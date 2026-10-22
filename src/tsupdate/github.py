"""GitHub API interactions for checking releases."""

import logging
import os
import itertools
import re
import subprocess
from typing import List, Optional, Tuple

from github import Github
from github.GithubException import GithubException, RateLimitExceededException

from tsupdate.errors import NETWORK_ERRORS, ConfigurationError, TransientError, UpdateError, network_error

logger = logging.getLogger(__name__)

# tsupdate runs as root and applies whatever update source it's pointed at, so the
# accepted source is pinned to this GitHub org in code (not configurable via
# tsupdate.yml/CLI/SUPPORT_URL) rather than trusted from any of those inputs.
# Compared case-insensitively everywhere below.
ALLOWED_OWNER = "trackIT-Systems"

# Module-level GitHub token (loaded once at program start)
_github_token: Optional[str] = None


def initialize_github_token() -> None:
    """
    Initialize the GitHub token from environment variable or GitHub CLI.
    
    First checks GH_TOKEN environment variable. If not set, tries to get
    the token from GitHub CLI ('gh auth token').
    
    This should be called once at program start to load the token.
    """
    global _github_token
    
    # First, check environment variable
    token = os.environ.get("GH_TOKEN")
    if token:
        _github_token = token.strip() if token.strip() else None
        if _github_token:
            logger.debug("Loaded GitHub token from GH_TOKEN environment variable")
            return
    
    # If not in environment, try GitHub CLI
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        token = result.stdout.strip()
        if token:
            _github_token = token
            logger.debug("Loaded GitHub token from 'gh auth token'")
            return
    except FileNotFoundError:
        logger.debug("GitHub CLI ('gh') not found")
    except subprocess.TimeoutExpired:
        logger.debug("'gh auth token' command timed out")
    except subprocess.CalledProcessError as e:
        logger.debug("'gh auth token' failed: %s", e.stderr.strip())
    except Exception as e:
        logger.debug("Unexpected error running 'gh auth token': %s", e)
    
    # No token available
    _github_token = None
    logger.debug("No GitHub token available (neither GH_TOKEN nor 'gh auth token')")


def get_github_token() -> Optional[str]:
    """
    Get the GitHub token (if set).
    
    Returns:
        GitHub token string or None if not set
    """
    return _github_token


def parse_github_repo_url(support_url: str) -> Optional[Tuple[str, str]]:
    """
    Parse GitHub owner/repo from SUPPORT_URL.
    
    Handles formats like:
    - https://github.com/owner/repo
    - https://github.com/owner/repo/issues
    - https://github.com/owner/repo/releases
    
    Args:
        support_url: SUPPORT_URL from os-release
        
    Returns:
        Tuple of (owner, repo) or None if parsing fails, or if the owner is
        not ALLOWED_OWNER.
    """
    if not support_url:
        return None

    # Fully anchored (not re.search) so this can't match a GitHub URL embedded
    # anywhere inside a longer, attacker-controlled string (e.g.
    # "https://evil.example/?u=https://github.com/trackIT-Systems/foo", or
    # "https://github.com.evil.example/..."). https only - no plain http.
    # A trailing path (e.g. /issues, /releases) is still allowed after
    # owner/repo, matching the documented formats this function accepts.
    pattern = r'^https://github\.com/([^/]+)/([^/?#]+)(?:/[^?#]*)?(?:[?#].*)?$'
    match = re.fullmatch(pattern, support_url.strip())

    if not match:
        logger.debug("Could not parse GitHub repo URL from: %s", support_url)
        return None

    owner = match.group(1)
    repo = match.group(2)

    if owner.lower() != ALLOWED_OWNER.lower():
        logger.debug(
            "Rejecting update source from disallowed owner '%s' (only '%s' is accepted): %s",
            owner,
            ALLOWED_OWNER,
            support_url,
        )
        return None

    logger.debug("Parsed GitHub repo: %s/%s from %s", owner, repo, support_url)
    return (owner, repo)


def update_source(os_release, github_url: str | None = None) -> tuple[str, str, str]:
    """
    The repository to look for updates in, and the version to update from.

    Args:
        os_release: OSRelease of the booted system (or None if it could not be read)
        github_url: Repository URL that overrides SUPPORT_URL from os-release

    Returns:
        (owner, repo, version_id)

    Raises:
        UpdateError: /etc/os-release cannot be read.
        ConfigurationError: There is no repository URL, it is not an allowed GitHub URL, or
            VERSION_ID is missing.
    """
    if not os_release:
        raise UpdateError("Could not read /etc/os-release")

    if github_url:
        support_url = github_url
        logger.debug("Using GitHub URL from configuration: %s", support_url)
    else:
        support_url = os_release.get("SUPPORT_URL")
        if not support_url:
            raise ConfigurationError("SUPPORT_URL not found in /etc/os-release and no github_url configured")
        logger.debug("Using SUPPORT_URL from os-release: %s", support_url)

    version_id = os_release.version_id
    if not version_id:
        raise ConfigurationError("VERSION_ID not found in /etc/os-release")
    logger.debug("Current VERSION_ID: %s", version_id)

    repo_info = parse_github_repo_url(support_url)
    if not repo_info:
        raise ConfigurationError(
            f"Not an allowed update source: {support_url}; expected https://github.com/{ALLOWED_OWNER}/<repo>"
        )
    owner, repo = repo_info
    logger.debug("GitHub repository: %s/%s", owner, repo)
    return owner, repo, version_id


def _create_github_client():
    """
    Create and configure a GitHub client instance.
    
    Uses module-level GitHub token if available. Retries are disabled to prevent blocking
    signal handling when rate limited. An invalid token is reported by the first request
    (401, a ConfigurationError), so the token is not verified separately.
    
    Returns:
        Configured Github instance
    """
    github_token = get_github_token()
    # Disable retries to prevent blocking signal handling when rate limited
    # This allows the daemon to respond to SIGTERM/SIGINT immediately
    if github_token:
        g = Github(github_token, retry=0)
        logger.debug("Using GitHub token for authentication")
        if logger.isEnabledFor(logging.DEBUG):
            _log_rate_limit(g)
    else:
        g = Github(retry=0)
        logger.debug("Using unauthenticated GitHub API access")
    return g


def _log_rate_limit(g) -> None:
    """Log the remaining API rate limit at DEBUG. Costs one request, so only done when debugging."""
    try:
        rate_limit = g.get_rate_limit()
        if hasattr(rate_limit, 'resources') and hasattr(rate_limit.resources, 'core'):
            logger.debug(
                "Rate limit remaining: %s/%s",
                rate_limit.resources.core.remaining,
                rate_limit.resources.core.limit,
            )
        elif hasattr(rate_limit, 'rate'):
            logger.debug("Rate limit remaining: %s/%s", rate_limit.rate.remaining, rate_limit.rate.limit)
        else:
            logger.debug("Rate limit checked (structure unknown)")
    except Exception as e:  # noqa: BLE001 - only a debugging aid; the real request reports errors
        logger.debug("Could not read the GitHub rate limit: %s", e)


def _is_rate_limit(e: GithubException) -> bool:
    """Whether a 403/429 from GitHub is the rate limit rather than missing permissions."""
    if isinstance(e, RateLimitExceededException) or e.status == 429:
        return True
    headers = {k.lower(): v for k, v in (e.headers or {}).items()}
    if headers.get("x-ratelimit-remaining") == "0":
        return True
    message = e.data.get("message", "") if isinstance(e.data, dict) else str(e.data or "")
    return "rate limit" in message.lower()


def github_error(e: Exception, owner: str, repo: str) -> UpdateError:
    """
    Map an error from a GitHub API request to an UpdateError.

    Rate limit, 5xx and network errors are transient; 401, 403 and 404 need a human.
    Anything else is returned unchanged by the caller (an unexpected error).

    Args:
        e: GithubException or network error raised by PyGithub
        owner: Repository owner
        repo: Repository name
    """
    if not isinstance(e, GithubException):
        return network_error(e, "GitHub")

    token = get_github_token()
    if e.status == 404:
        if token:
            return ConfigurationError(
                f"Repository not found or access denied: {owner}/{repo}; if it is a private repository, "
                "ensure your token has 'repo' scope permissions"
            )
        return ConfigurationError(
            f"Repository not found: {owner}/{repo}; if it is a private repository, "
            "set the GH_TOKEN environment variable to a token that has 'repo' scope"
        )
    if e.status in (403, 429) and _is_rate_limit(e):
        hint = "" if token else "; set GH_TOKEN for a higher limit"
        return TransientError(f"GitHub API rate limit exceeded{hint}")
    if e.status == 401:
        return ConfigurationError("GitHub rejected the token (401 Bad credentials); check GH_TOKEN or 'gh auth token'")
    if e.status == 403:
        if token:
            return ConfigurationError(
                f"GitHub denied access to {owner}/{repo}; check that your token has the necessary "
                "permissions (e.g., 'repo' scope for private repos)"
            )
        return ConfigurationError(f"GitHub denied access to {owner}/{repo}")
    if isinstance(e.status, int) and e.status >= 500:
        return TransientError(f"GitHub API error {e.status}: {_github_message(e)}")
    return UpdateError(f"GitHub API error {e.status}: {_github_message(e)}")


def _github_message(e: GithubException) -> str:
    if isinstance(e.data, dict) and e.data.get("message"):
        return str(e.data["message"])
    return str(e.data) if e.data else "no message"


def _get_assets_from_release(release) -> List[dict]:
    """
    Extract asset information from a release object.
    
    Tries to use raw_data first to avoid API calls, falls back to API call if needed.
    
    Args:
        release: Release object from PyGithub
        
    Returns:
        List of asset dictionaries with 'name', 'size', 'url', and optionally 'id'
    """
    assets = []
    
    # Try to get assets from raw_data (avoids API call)
    try:
        if hasattr(release, 'raw_data') and 'assets' in release.raw_data:
            for asset_data in release.raw_data['assets']:
                asset_info = {
                    'name': asset_data.get('name', ''),
                    'size': asset_data.get('size', 0),
                    'url': asset_data.get('browser_download_url', ''),
                    # GitHub's release-assets API started returning a content
                    # digest (e.g. "sha256:<hex>") on newer API versions; not
                    # guaranteed present depending on API version/asset age.
                    'digest': asset_data.get('digest'),
                }
                # Include asset_id if available
                if 'id' in asset_data:
                    asset_info['id'] = asset_data['id']
                assets.append(asset_info)
            logger.debug("Found %s assets from raw_data", len(assets))
            return assets
    except (AttributeError, KeyError, TypeError) as e:
        logger.debug("Could not access raw_data, falling back to API call: %s", e)

    # Fallback: use API call
    try:
        for asset in release.get_assets():
            asset_info = {
                'name': asset.name,
                'size': asset.size,
                'url': asset.browser_download_url,
                'id': asset.id,
                'digest': getattr(asset, 'digest', None),
            }
            assets.append(asset_info)
        logger.debug("Found %s assets from API call", len(assets))
    except Exception as e:
        logger.debug("Error fetching assets via API: %s", e)
    
    return assets


def parse_github_release_url(url: str) -> Optional[Tuple[str, str, Optional[str], Optional[str]]]:
    """
    Parse GitHub release URL to extract owner, repo, tag, and asset name.
    
    Handles formats like:
    - https://github.com/owner/repo/releases/download/tag/asset-name
    - https://github.com/owner/repo/releases/tag/tag-name
    
    Args:
        url: GitHub release URL
        
    Returns:
        Tuple of (owner, repo, tag, asset_name) or None if parsing fails,
        or if the owner is not ALLOWED_OWNER.
        tag and asset_name may be None if not found in URL
    """
    if not url:
        return None

    url = url.strip()

    # Both patterns are fully anchored (re.fullmatch) and https-only, for the
    # same reason as parse_github_repo_url: an unanchored/http-tolerant match
    # could be tricked by a URL that merely contains a GitHub-looking segment.
    # Pattern for release download URL: /releases/download/tag/asset-name
    download_pattern = r'^https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/?#]+)$'
    match = re.fullmatch(download_pattern, url)
    if match:
        owner, repo, tag, asset_name = match.groups()
        if owner.lower() != ALLOWED_OWNER.lower():
            logger.debug(
                "Rejecting release URL from disallowed owner '%s' (only '%s' is accepted): %s",
                owner,
                ALLOWED_OWNER,
                url,
            )
            return None
        logger.debug("Parsed GitHub release download URL: %s/%s tag=%s asset=%s", owner, repo, tag, asset_name)
        return (owner, repo, tag, asset_name)

    # Pattern for release page URL: /releases/tag/tag-name
    tag_pattern = r'^https://github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)$'
    match = re.fullmatch(tag_pattern, url)
    if match:
        owner, repo, tag = match.groups()
        if owner.lower() != ALLOWED_OWNER.lower():
            logger.debug(
                "Rejecting release URL from disallowed owner '%s' (only '%s' is accepted): %s",
                owner,
                ALLOWED_OWNER,
                url,
            )
            return None
        logger.debug("Parsed GitHub release tag URL: %s/%s tag=%s", owner, repo, tag)
        return (owner, repo, tag, None)

    logger.debug("Could not parse GitHub release URL: %s", url)
    return None


def resolve_github_release_asset(url: str) -> dict:
    """
    Resolve a GitHub release URL to the actual asset, via the GitHub API.

    Takes a GitHub release URL and uses PyGithub to fetch the release and
    find the matching asset. Supports authentication for private
    repositories (reads GH_TOKEN from environment variable).

    Args:
        url: GitHub release URL (download URL with tag and asset name)

    Returns:
        {"url": <download url>, "size": <int or None>, "digest": <str or None>}.
        `size`/`digest` come from the GitHub API's asset metadata (whichever of
        those fields the API response for this asset happened to include) and
        are used by ensure_file() to verify a cached/resumed file before reusing
        it without re-downloading.

    Raises:
        ConfigurationError: The URL is not a release download URL of ALLOWED_OWNER, or the
            repository, release or asset does not exist.
        TransientError: GitHub is unreachable or rate limited.
        UpdateError: Any other GitHub API error.
    """
    parsed = parse_github_release_url(url)
    if not parsed:
        raise ConfigurationError(
            f"Invalid GitHub release URL: {url}; expected https://github.com/{ALLOWED_OWNER}/<repo>/releases/"
            "download/<tag>/<asset>"
        )

    owner, repo, tag, asset_name = parsed

    if not asset_name:
        raise ConfigurationError(
            f"Cannot resolve GitHub release URL {url}: asset name required; "
            "use https://github.com/owner/repo/releases/download/tag/asset-name"
        )

    # Verify the asset via the API and get an authenticated URL for private repos
    try:
        g = _create_github_client()
        repository = g.get_repo(f"{owner}/{repo}")
        try:
            release = repository.get_release(tag)
        except GithubException as e:
            if e.status == 404:
                raise ConfigurationError(f"Release tag '{tag}' not found in {owner}/{repo}") from e
            raise
        assets = _get_assets_from_release(release)
    except (GithubException, *NETWORK_ERRORS) as e:
        raise github_error(e, owner, repo) from e

    for asset_info in assets:
        if asset_info['name'] != asset_name:
            continue
        metadata = {"size": asset_info.get("size"), "digest": asset_info.get("digest")}
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_info.get('id')}"
        # Prefer browser_download_url (works for public repos, simpler)
        # Only use API endpoint if we have a token (for private repos)
        if get_github_token() and 'id' in asset_info:
            logger.debug("Found asset: %s -> API endpoint (authenticated): %s", asset_name, api_url)
            return {"url": api_url, **metadata}
        browser_url = asset_info.get('url', '')
        if browser_url:
            logger.debug("Found asset: %s -> browser_download_url: %s", asset_name, browser_url)
            return {"url": browser_url, **metadata}
        if 'id' in asset_info:
            # Fallback to API endpoint if browser_url not available
            logger.debug("Found asset: %s -> API endpoint (fallback): %s", asset_name, api_url)
            return {"url": api_url, **metadata}

    raise ConfigurationError(f"Asset '{asset_name}' not found in release '{tag}' of {owner}/{repo}")


def fetch_releases(
    owner: str,
    repo: str,
    include_prereleases: bool = False,
    limit: Optional[int] = None,
) -> List:
    """
    Fetch GitHub releases using PyGithub.
    
    Reads GH_TOKEN from environment variable for authentication.
    
    Args:
        owner: GitHub repository owner
        repo: GitHub repository name
        include_prereleases: If True, include pre-releases. If False, filter them out.
        limit: Return at most this many releases (the newest, after filtering). The API is
            paginated and read lazily, so a small limit fetches only the first page or two
            instead of every release the repository ever had. None returns all releases.
        
    Returns:
        List of Release objects from PyGithub; empty if the repository has no (matching) releases

    Raises:
        TransientError: GitHub is unreachable, returned 5xx or is rate limited.
        ConfigurationError: The repository does not exist or access was denied.
        UpdateError: Any other GitHub API error.
    """
    try:
        g = _create_github_client()
        logger.debug("Fetching releases from: %s/%s", owner, repo)
        repository = g.get_repo(f"{owner}/{repo}")
        
        # Releases are sorted by date, latest first. get_releases() pages lazily, so stop
        # reading as soon as `limit` matching releases were found.
        candidates = repository.get_releases()
        if not include_prereleases:
            candidates = (r for r in candidates if not r.prerelease)
        releases = list(itertools.islice(candidates, limit))
    except (GithubException, *NETWORK_ERRORS) as e:
        raise github_error(e, owner, repo) from e

    kind = "releases" if include_prereleases else "regular releases (excluding pre-releases)"
    logger.debug("Fetched %s %s%s", len(releases), kind, f" (limit {limit})" if limit is not None else "")
    return releases


def find_latest_image(releases: List) -> Optional[str]:
    """
    Find image file in latest release assets.
    
    Matches files with .img, .zip, .gz, or .xz extension and returns the largest one.
    
    Args:
        releases: List of Release objects from PyGithub
        
    Returns:
        browser_download_url of the largest image file, or None if not found
    """
    if not releases:
        logger.debug("No releases available")
        return None
    
    # Get latest release (first in list, sorted by date)
    latest_release = releases[0]
    
    logger.debug("Searching for image in latest release: %s", latest_release.tag_name)
    
    # Get assets from release
    asset_candidates = _get_assets_from_release(latest_release)
    if not asset_candidates:
        logger.debug("No assets found in latest release")
        return None
    
    # Allowed image file extensions
    allowed_extensions = (".img", ".zip", ".gz", ".xz")
    
    # Find all matching assets and track the largest one
    largest_asset = None
    largest_size = 0
    
    for asset_info in asset_candidates:
        name = asset_info['name'].lower()
        
        # Check if file has an allowed extension
        if name.endswith(allowed_extensions):
            size = asset_info['size']
            logger.debug("Found candidate image file: %s (%s bytes)", asset_info['name'], size)
            
            if size > largest_size:
                largest_size = size
                largest_asset = asset_info
    
    if largest_asset:
        logger.debug("Selected largest image file: %s (%s bytes)", largest_asset['name'], largest_size)
        return largest_asset['url']
    
    logger.debug("No image file found in latest release")
    return None


def find_applicable_batch_update(releases: List, current_version: str, max_releases: int = 5) -> Optional[str]:
    """
    Find batch update tar file matching current version.
    
    Searches for files matching pattern: tsOS-{variant}-arm64-update-{current_version}-to-{next_version}.tar
    
    Args:
        releases: List of Release objects from PyGithub
        current_version: Current version ID (e.g., "2025.12.4")
        max_releases: Maximum number of recent releases to check (default: 5)
        
    Returns:
        browser_download_url of the applicable batch update file, or None if not found
    """
    if not releases:
        logger.debug("No releases available")
        return None
    
    if not current_version:
        logger.debug("No current version provided")
        return None
    
    # Escape dots in version for regex
    escaped_version = re.escape(current_version)
    
    # Pattern: tsOS-{variant}-arm64-update-{current_version}-to-{next_version}.tar(.gz)?
    # Variant can be any string (e.g., "base", "pro")
    pattern = rf'tsOS-([^-]+)-arm64-update-{escaped_version}-to-([0-9.]+)\.tar(\.gz)?$'
    
    logger.debug("Searching for batch update matching pattern: tsOS-*-arm64-update-%s-to-*.tar", current_version)
    
    # Limit search to most recent releases to reduce API calls
    # The "next" update should logically be in recent releases
    releases_to_check = releases[:max_releases]
    logger.debug("Checking %s most recent releases (out of %s total)", len(releases_to_check), len(releases))
    
    # Search through recent releases (they should be sorted by date, latest first)
    for release in releases_to_check:
        # Get assets from release
        assets = _get_assets_from_release(release)
        if not assets:
            logger.debug("No assets found for release %s", release.tag_name)
            continue
        
        # Check asset names against pattern
        for asset_info in assets:
            name = asset_info['name']
            match = re.match(pattern, name)
            if match:
                variant = match.group(1)
                next_version = match.group(2)
                logger.debug(
                    "Found applicable batch update: %s (variant: %s, updates to: %s)",
                    name,
                    variant,
                    next_version,
                )
                return asset_info['url']
    
    logger.debug(
        "No applicable batch update found for version %s in %s recent releases",
        current_version,
        len(releases_to_check),
    )
    return None

