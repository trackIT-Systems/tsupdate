"""Utility functions for file downloads and management."""

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests

from tsupdate.github import ALLOWED_OWNER, parse_github_release_url, resolve_github_release_asset, get_github_token

logger = logging.getLogger(__name__)


# Artifacts directory for downloaded files
ARTIFACTS_DIR = Path("/data/tsupdate")

# Hosts a download is ever allowed to actually fetch bytes from. github.com and
# api.github.com are where a request is first made; GitHub redirects asset
# downloads to one of the *.githubusercontent.com CDN hosts, which download_file()
# checks the final response URL against after following redirects.
_ALLOWED_API_HOSTS = frozenset({"github.com", "api.github.com"})
_ALLOWED_ASSET_HOSTS = frozenset({
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})


def is_allowed_source_url(url: str) -> bool:
    """Whether `url` is allowed as a tsupdate download/resolve source.

    This is the actual enforcement point for the trackIT-Systems org pin: it
    is checked in ensure_file() for *every* remote source, not only ones that
    happen to match the GitHub-release-URL shape that parse_github_release_url
    understands - a URL that doesn't match that shape (e.g. an arbitrary
    non-GitHub host, or a github.com URL to some other repo/org) would
    otherwise fall through to being downloaded as-is.

    Requires https, host github.com or api.github.com, and the owner segment
    of the path (github.com/<owner>/... or api.github.com/repos/<owner>/...)
    to match ALLOWED_OWNER case-insensitively.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_API_HOSTS:
        return False

    path_parts = [p for p in parsed.path.split("/") if p]
    if host == "github.com":
        owner = path_parts[0] if path_parts else ""
    else:  # api.github.com
        # Expected shape: /repos/<owner>/<repo>/...
        owner = path_parts[1] if len(path_parts) >= 2 and path_parts[0] == "repos" else ""

    return owner.lower() == ALLOWED_OWNER.lower()


def _is_allowed_asset_response_host(url: str) -> bool:
    """Whether a (possibly redirected-to) response URL is a legitimate GitHub
    asset host. Used after following redirects in download_file(), since a
    malicious or misbehaving server on an otherwise-allowed request URL could
    redirect the actual content fetch elsewhere.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _ALLOWED_ASSET_HOSTS

# Default wall-clock limit for rsync invocations (syncroot, apply read-batch)
DEFAULT_RSYNC_TIMEOUT_SEC = 3600


def resolve_rsync_timeout_seconds(rsync_timeout_sec: Optional[int]) -> Optional[float]:
    """
    Convert configured or CLI ``rsync_timeout`` (seconds) to a value for ``subprocess.run(timeout=...)``.

    Args:
        rsync_timeout_sec: ``None`` uses ``DEFAULT_RSYNC_TIMEOUT_SEC`` (3600).
            ``0`` means no limit (returns ``None``). Positive values are used as-is.

    Returns:
        Seconds as float for subprocess, or ``None`` for no timeout.
    """
    if rsync_timeout_sec is None:
        return float(DEFAULT_RSYNC_TIMEOUT_SEC)
    if rsync_timeout_sec == 0:
        return None
    if rsync_timeout_sec < 0:
        logger.warning(
            "Invalid rsync_timeout %s (must be >= 0), using default %s seconds",
            rsync_timeout_sec,
            DEFAULT_RSYNC_TIMEOUT_SEC,
        )
        return float(DEFAULT_RSYNC_TIMEOUT_SEC)
    return float(rsync_timeout_sec)


def safe_cleanup(path: Path) -> None:
    """
    Safely remove a file, ignoring errors.
    
    Useful for cleanup operations where failure to remove is not critical.
    
    Args:
        path: Path to file to remove
    """
    try:
        if path and path.exists():
            path.unlink()
            logger.debug(f"Removed file: {path}")
    except OSError:
        pass  # Ignore cleanup errors


def prune_artifacts(artifacts_path: Optional[Path] = None) -> int:
    """
    Delete everything left in the artifacts directory (downloads, partial downloads,
    extracted images) and return how many files were removed.

    Only entries directly inside the directory are removed, symlinks are unlinked
    rather than followed, and subdirectories are left alone.
    """
    if artifacts_path is None:
        artifacts_path = ARTIFACTS_DIR
    removed = 0
    try:
        entries = list(artifacts_path.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"Could not remove {entry}: {e}")
    if removed:
        logger.info(f"Removed {removed} file(s) from {artifacts_path}")
    return removed


def _sha256_file(path: Path) -> str:
    """Compute the sha256 hex digest of a file, streaming it in chunks."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _verify_file(path: Path, expected_size: Optional[int], expected_digest: Optional[str]) -> Tuple[bool, str]:
    """Verify a downloaded/cached file against whatever metadata the GitHub
    API gave us for the asset it's supposed to be.

    Digest is the strong check when available (GitHub's release-assets API
    returns one as "sha256:<hex>" on newer API versions, but not always -
    older assets and some API responses omit it). When there's no digest,
    fall back to a size comparison, which is weaker (doesn't rule out a
    same-size tampered file) but still catches a truncated/wrong file. When
    neither is available at all, we cannot verify - and given this exists
    specifically to stop a pre-placed or tampered file in the world-writable
    /data/tsupdate cache from being silently trusted, "cannot verify" must
    mean "reject", not "assume it's fine".

    Returns (ok, reason) - reason is a human-readable explanation either way,
    used for logging.
    """
    actual_size = path.stat().st_size

    if expected_digest:
        algo, _, expected_hex = expected_digest.partition(":")
        algo = algo.lower()
        if algo == "sha256":
            actual_hex = _sha256_file(path)
            if actual_hex.lower() == expected_hex.lower():
                return True, "sha256 digest matches"
            return False, f"sha256 digest mismatch (expected {expected_hex}, got {actual_hex})"
        logger.debug(f"Unsupported digest algorithm '{algo}', falling back to size check")

    if expected_size is not None:
        if actual_size == expected_size:
            return True, "size matches (no digest available to check)"
        return False, f"size mismatch (expected {expected_size} bytes, got {actual_size} bytes)"

    return False, "no digest or size available from the API to verify against"


def download_file(
    url: str,
    output_path: Path,
    expected_size: Optional[int] = None,
    expected_digest: Optional[str] = None,
) -> bool:
    """
    Download file from URL with resumable download support.
    
    Downloads to a temporary file first, then renames to the final path
    on success to avoid corrupt files if interrupted. Automatically resumes
    from existing .tmp files if present.
    
    For GitHub release URLs, uses authenticated requests if GH_TOKEN is set.
    
    Args:
        url: URL to download from
        output_path: Path where to save the downloaded file
        expected_size: If known (from the GitHub API), the asset's size in
            bytes - the completed download is checked against this (and,
            if available, expected_digest) before being kept.
        expected_digest: If known, the asset's content digest as returned by
            the GitHub API (e.g. "sha256:<hex>").

    Returns:
        True if successful, False otherwise
    """
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    
    # Check if we can resume from existing partial download
    resume_from = 0
    file_mode = "wb"
    if temp_path.exists() and temp_path.is_file():
        resume_from = temp_path.stat().st_size
        if resume_from > 0:
            logger.info(f"Resuming download from {resume_from} bytes")
            file_mode = "ab"  # Append mode for resume
        else:
            # Empty file, start fresh
            logger.debug("Existing temp file is empty, starting fresh download")
            temp_path.unlink()
            resume_from = 0
    
    try:
        if resume_from == 0:
            logger.info(f"Downloading file from {url}...")
        logger.debug(f"Downloading to temporary file: {temp_path}")
        
        # Prepare headers
        headers = {
            "User-Agent": "tsupdate/1.0",
        }
        
        # Check if this is a GitHub API URL (requires authentication) or GitHub URL
        is_github_api_url = url.startswith("https://api.github.com/repos/") and "/releases/assets/" in url
        is_github_url = url.startswith("https://github.com/") or url.startswith("https://api.github.com/")
        github_token = get_github_token()
        use_auth = (is_github_api_url or is_github_url) and github_token
        
        if use_auth:
            logger.debug("Using authenticated download for GitHub URL")
            headers["Authorization"] = f"Bearer {github_token}"
            headers["Accept"] = "application/octet-stream"
        
        # Add Range header if resuming
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
        
        # Make request with streaming
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        
        # Handle Range request errors
        if resume_from > 0:
            if response.status_code == 416:  # Range Not Satisfiable
                logger.warning("Server does not support Range requests, restarting download")
                response.close()
                safe_cleanup(temp_path)
                # Retry without Range header
                headers.pop("Range", None)
                file_mode = "wb"
                resume_from = 0
                response = requests.get(url, headers=headers, stream=True, timeout=30)
            elif response.status_code == 206:  # Partial Content
                logger.debug("Server supports Range requests, resuming download")
            elif response.status_code == 200:  # Full content (some servers ignore Range)
                logger.warning("Server returned full content despite Range header, restarting download")
                response.close()
                safe_cleanup(temp_path)
                headers.pop("Range", None)
                file_mode = "wb"
                resume_from = 0
                response = requests.get(url, headers=headers, stream=True, timeout=30)
        
        # Check for errors
        response.raise_for_status()

        # requests follows redirects by default, so the actual bytes could come
        # from somewhere other than the URL we requested (that's expected for
        # GitHub asset downloads, which redirect to a *.githubusercontent.com
        # CDN host) - but verify the final host is still one we trust, so a
        # request that started at github.com/api.github.com can't be redirected
        # to an arbitrary attacker-controlled host.
        if not _is_allowed_asset_response_host(response.url):
            logger.error(f"Refusing download: response URL is not a trusted GitHub asset host: {response.url}")
            response.close()
            return False

        # Get total size
        total_size = None
        if 'Content-Length' in response.headers:
            content_length = int(response.headers['Content-Length'])
            if resume_from > 0:
                # For Range requests, Content-Length is the remaining bytes
                total_size = resume_from + content_length
            else:
                total_size = content_length
        elif resume_from > 0:
            # If we're resuming but don't know total size, log warning
            logger.warning("Resuming download but server didn't provide Content-Length")
        
        # Download in chunks
        downloaded = resume_from
        chunk_size = 8192  # 8KB chunks
        
        with open(temp_path, file_mode) as f_out:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:  # Filter out keep-alive chunks
                    f_out.write(chunk)
                    downloaded += len(chunk)
                    
                    # Log progress every 100 chunks (about every 800KB)
                    if total_size and downloaded % (chunk_size * 100) == 0:
                        percent = min(100, (downloaded * 100) / total_size)
                        logger.debug(f"Download progress: {percent:.1f}% ({downloaded}/{total_size} bytes)")
        
        # Verify download completed
        if total_size and downloaded < total_size:
            logger.warning(f"Download incomplete: {downloaded}/{total_size} bytes")
            # Keep temp file for resume on next attempt
            return False

        # Verify the completed file against known-good metadata before trusting
        # it, whether this was a fresh download or a resumed one - a resume
        # picks up appending to whatever was already in the .tmp file, so this
        # also catches a tampered/corrupt partial file that "completes" to the
        # wrong content.
        if expected_size is not None or expected_digest is not None:
            ok, reason = _verify_file(temp_path, expected_size, expected_digest)
            if not ok:
                logger.error(f"Downloaded file failed verification, discarding: {reason}")
                safe_cleanup(temp_path)
                return False
            logger.debug(f"Downloaded file verified: {reason}")

        # Rename temporary file to final path (atomic operation)
        temp_path.rename(output_path)
        if resume_from > 0:
            logger.info(f"Resumed and completed download to {output_path}")
        else:
            logger.info(f"Downloaded file to {output_path}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download file: {e}")
        # Keep temporary file for resume on next attempt
        return False
    except Exception as e:
        logger.error(f"Failed to download file: {e}")
        # Keep temporary file for resume on next attempt
        return False


def ensure_file(source: str, artifacts_path: Optional[Path] = None) -> Tuple[Optional[Path], bool]:
    """
    Ensure a file is available locally, downloading from URL if needed.
    
    Handles both URLs and local file paths:
    - For URLs: checks cache in artifacts directory, downloads if needed
    - For local files: validates existence and returns path
    - For GitHub release URLs: uses GitHub API to resolve authenticated download URL
      (reads GH_TOKEN from environment variable)
    
    Args:
        source: URL or local file path
        artifacts_path: Directory for cached downloads (defaults to ARTIFACTS_DIR)
        
    Returns:
        Tuple of (file_path, is_local) where:
        - file_path: Path to the file (or None on error)
        - is_local: True if source was a local file, False if downloaded from URL
    """
    if artifacts_path is None:
        artifacts_path = ARTIFACTS_DIR
    
    # Check if source is a URL or local file path
    source_path = Path(source)
    if source_path.exists() and source_path.is_file():
        # Local file path
        logger.info(f"Using local file: {source}")
        return (source_path, True)

    # Reject any remote source that isn't a trackIT-Systems GitHub URL, before
    # doing anything else with it (network access, GitHub API resolution, or
    # treating it as a download URL). This is the actual enforcement point for
    # the org pin: parse_github_repo_url/parse_github_release_url reject bad
    # owners for the paths that call them, but a source that doesn't even match
    # the GitHub-release-URL shape below would otherwise fall through to the
    # generic "download whatever URL we were given" path untouched.
    if not is_allowed_source_url(source):
        logger.error(
            f"Refusing update source (not a github.com/api.github.com URL for "
            f"'{ALLOWED_OWNER}'): {source}"
        )
        return (None, False)

    # Check if this is a GitHub release URL
    download_url = source
    asset_name = None
    asset_size: Optional[int] = None
    asset_digest: Optional[str] = None
    if source.startswith("https://github.com/") and "/releases/" in source:
        logger.debug(f"Detected GitHub release URL: {source}")
        # Extract asset name from original URL before resolving
        # Format: https://github.com/owner/repo/releases/download/tag/asset-name
        if "/releases/download/" in source:
            # basename(): the URL segment after tag/ is meant to be a bare
            # filename, but nothing stops it containing "/" or ".." - without
            # this it could be used to escape artifacts_path below.
            asset_name = os.path.basename(source.split("/releases/download/")[1].split("/", 1)[-1])

        # Always resolve via the GitHub API, even without a token (when the
        # direct browser_download_url would work fine on its own). This is
        # deliberate, not just to get the right endpoint for private repos:
        # it's the only way to get the asset's size/digest, which the cache
        # check below uses to verify a cached or resumed file before reusing
        # it without a fresh download. Skipping the API call whenever
        # possible would mean that check silently never runs for the common
        # "public repo, no GH_TOKEN" case - the one most deployments are in.
        asset = resolve_github_release_asset(source)
        if not asset:
            logger.error(f"Failed to resolve GitHub release URL: {source}")
            return (None, False)
        download_url = asset["url"]
        asset_size = asset.get("size")
        asset_digest = asset.get("digest")
        logger.debug(f"Resolved GitHub release URL to: {download_url} (size={asset_size}, digest={asset_digest})")
    
    # URL - check if already downloaded, otherwise download
    # Determine file name - prefer asset name from GitHub URL, then from download URL
    if asset_name:
        filename = asset_name
        logger.debug(f"Using asset name from GitHub URL: {filename}")
    else:
        filename = Path(download_url).name
        if not filename or filename == download_url or filename.isdigit():
            # Fallback: try to get filename from original source URL
            filename = Path(source).name
            if not filename or filename == source:
                # Fallback to generic name
                filename = "downloaded-file"

    # However filename was derived above, reduce it to a bare basename before
    # using it in a path join below - Path(...).name already strips directory
    # components for a normal URL, but doesn't special-case ".."/"." as the
    # final component, and asset_name (from parse_github_release_url's capture
    # group) is now already basename()'d at its source, but this is the single
    # choke point every branch above funnels through, so enforce it here too.
    filename = os.path.basename(filename)
    if filename in ("", ".", ".."):
        filename = "downloaded-file"

    downloaded_path = artifacts_path / filename
    
    # Ensure artifacts directory exists
    try:
        artifacts_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Using artifacts directory: {artifacts_path}")
    except OSError as e:
        logger.error(f"Could not create artifacts directory {artifacts_path}: {e}")
        return (None, False)
    
    # Check if file already exists (preserve .tmp files for resumable downloads).
    # /data is a world-writable filesystem, so this cache is not trustworthy on
    # its own - a file could have been placed or altered by anything with
    # local access. Only reuse it if it verifies against the asset metadata
    # from the GitHub API; otherwise re-download rather than silently trusting
    # whatever is already there.
    if downloaded_path.exists() and downloaded_path.is_file():
        file_size = downloaded_path.stat().st_size
        if file_size > 0:
            if asset_size is not None or asset_digest is not None:
                ok, reason = _verify_file(downloaded_path, asset_size, asset_digest)
                if ok:
                    logger.info(f"File already exists and verified ({reason}): {downloaded_path}")
                    logger.info("Skipping download, using existing file")
                    return (downloaded_path, False)
                logger.warning(f"Cached file failed verification ({reason}), re-downloading: {downloaded_path}")
                downloaded_path.unlink()
            else:
                logger.warning(
                    f"Cached file exists but no size/digest available to verify it against "
                    f"({downloaded_path}); re-downloading rather than trusting it unverified"
                )
                downloaded_path.unlink()
        else:
            logger.warning(f"Existing file is empty, re-downloading")
            downloaded_path.unlink()

    # Download file
    logger.info(f"Downloading file from URL: {download_url}")
    if not download_file(download_url, downloaded_path, expected_size=asset_size, expected_digest=asset_digest):
        return (None, False)

    return (downloaded_path, False)




