"""GitHub API interactions for checking releases."""

import logging
import os
import itertools
import re
import subprocess
from typing import List, Optional, Tuple

from github import Github
from github.GithubException import GithubException

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
        logger.warning("Could not parse GitHub repo URL from: %s", support_url)
        return None

    owner = match.group(1)
    repo = match.group(2)

    if owner.lower() != ALLOWED_OWNER.lower():
        logger.warning(
            "Rejecting update source from disallowed owner '%s' (only '%s' is accepted): %s",
            owner,
            ALLOWED_OWNER,
            support_url,
        )
        return None

    logger.debug("Parsed GitHub repo: %s/%s from %s", owner, repo, support_url)
    return (owner, repo)


def _create_github_client():
    """
    Create and configure a GitHub client instance.
    
    Uses module-level GitHub token if available. Validates token by checking rate limit.
    Retries are disabled to prevent blocking signal handling when rate limited.
    
    Returns:
        Configured Github instance
    """
    github_token = get_github_token()
    # Disable retries to prevent blocking signal handling when rate limited
    # This allows the daemon to respond to SIGTERM/SIGINT immediately
    if github_token:
        g = Github(github_token, retry=0)
        logger.debug("Using GitHub token for authentication")
        # Verify token is valid by checking rate limit
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
        except Exception as e:
            logger.warning("Could not verify GitHub token: %s", e)
    else:
        g = Github(retry=0)
        logger.debug("Using unauthenticated GitHub API access")
    return g


def _handle_github_exception(e: GithubException, owner: str, repo: str) -> None:
    """
    Handle and log GitHub API exceptions consistently.
    
    Args:
        e: GithubException to handle
        owner: Repository owner
        repo: Repository name
    """
    if e.status == 404:
        if get_github_token():
            logger.error(
                "Repository not found or access denied: %s/%s; if it is a private repository, "
                "ensure your token has 'repo' scope permissions",
                owner,
                repo,
            )
        else:
            logger.error(
                "Repository not found: %s/%s; if it is a private repository, "
                "set the GH_TOKEN environment variable to a token that has 'repo' scope",
                owner,
                repo,
            )
    elif e.status == 403:
        if get_github_token():
            logger.error(
                "GitHub API rate limit exceeded or access forbidden; check that your token has the necessary "
                "permissions (e.g., 'repo' scope for private repos)"
            )
        else:
            logger.error("GitHub API rate limit exceeded or access forbidden")
    else:
        logger.error("GitHub API error: %s %s", e.status, e.data)


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
            logger.warning(
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
            logger.warning(
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


def resolve_github_release_asset(url: str) -> Optional[dict]:
    """
    Resolve a GitHub release URL to the actual asset, via the GitHub API.

    Takes a GitHub release URL and uses PyGithub to fetch the release and
    find the matching asset. Supports authentication for private
    repositories (reads GH_TOKEN from environment variable).

    Args:
        url: GitHub release URL (download or tag URL)

    Returns:
        {"url": <download url>, "size": <int or None>, "digest": <str or None>}
        or None on error. `size`/`digest` come from the GitHub API's asset
        metadata (whichever of those fields the API response for this asset
        happened to include) and are used by ensure_file() to verify a
        cached/resumed file before reusing it without re-downloading.
    """
    # Parse the URL
    parsed = parse_github_release_url(url)
    if not parsed:
        logger.error("Invalid GitHub release URL format: %s", url)
        return None

    owner, repo, tag, asset_name = parsed

    # If URL already contains asset name, we can construct the download URL directly
    # But we still need to verify it exists and get authenticated URL if needed
    if tag and asset_name:
        # We have both tag and asset name, but we should verify via API
        # and get authenticated URL for private repos
        try:
            # Create GitHub client
            g = _create_github_client()
            repository = g.get_repo(f"{owner}/{repo}")

            # Get release by tag
            try:
                release = repository.get_release(tag)
            except GithubException as e:
                if e.status == 404:
                    logger.error("Release tag '%s' not found in %s/%s", tag, owner, repo)
                else:
                    logger.error("Error fetching release: %s %s", e.status, e.data)
                return None

            # Find the asset by name
            assets = _get_assets_from_release(release)
            for asset_info in assets:
                if asset_info['name'] == asset_name:
                    metadata = {"size": asset_info.get("size"), "digest": asset_info.get("digest")}
                    # Prefer browser_download_url (works for public repos, simpler)
                    # Only use API endpoint if we have a token (for private repos)
                    github_token = get_github_token()
                    if github_token and 'id' in asset_info:
                        # For private repos with auth, use API endpoint
                        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_info['id']}"
                        logger.debug("Found asset: %s -> API endpoint (authenticated): %s", asset_name, api_url)
                        return {"url": api_url, **metadata}
                    else:
                        # For public repos or when no token, use browser_download_url
                        browser_url = asset_info.get('url', '')
                        if browser_url:
                            logger.debug("Found asset: %s -> browser_download_url: %s", asset_name, browser_url)
                            return {"url": browser_url, **metadata}
                        elif 'id' in asset_info:
                            # Fallback to API endpoint if browser_url not available
                            api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_info['id']}"
                            logger.debug("Found asset: %s -> API endpoint (fallback): %s", asset_name, api_url)
                            return {"url": api_url, **metadata}

            logger.error("Asset '%s' not found in release '%s'", asset_name, tag)
            return None

        except GithubException as e:
            _handle_github_exception(e, owner, repo)
            return None
        except Exception as e:
            logger.error("Unexpected error resolving GitHub release URL: %s", e)
            return None

    # If we only have tag (no asset name), we need to find the asset
    # This is less common but we'll handle it
    if tag and not asset_name:
        logger.error("Cannot resolve GitHub release URL: asset name required")
        logger.error("Use format: https://github.com/owner/repo/releases/download/tag/asset-name")
        return None

    return None


def fetch_releases(
    owner: str,
    repo: str,
    include_prereleases: bool = False,
    limit: Optional[int] = None,
) -> Optional[List]:
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
        List of Release objects from PyGithub, or None on error
    """
    try:
        # Create GitHub client
        g = _create_github_client()
        logger.debug("Fetching releases from: %s/%s", owner, repo)
        repository = g.get_repo(f"{owner}/{repo}")
        
        # Releases are sorted by date, latest first. get_releases() pages lazily, so stop
        # reading as soon as `limit` matching releases were found.
        candidates = repository.get_releases()
        if not include_prereleases:
            candidates = (r for r in candidates if not r.prerelease)
        releases = list(itertools.islice(candidates, limit))
        
        kind = "releases" if include_prereleases else "regular releases (excluding pre-releases)"
        logger.debug("Fetched %s %s%s", len(releases), kind, f" (limit {limit})" if limit is not None else "")
        return releases
                
    except GithubException as e:
        _handle_github_exception(e, owner, repo)
        return None
    except Exception as e:
        logger.error("Unexpected error fetching releases: %s", e)
        return None


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
        logger.warning("No current version provided")
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

