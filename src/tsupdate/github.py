"""GitHub API interactions for checking releases."""

import logging
import os
import re
import subprocess
from typing import List, Optional, Tuple

from github import Github
from github.GithubException import GithubException

logger = logging.getLogger(__name__)

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
        logger.debug(f"'gh auth token' failed: {e.stderr.strip()}")
    except Exception as e:
        logger.debug(f"Unexpected error running 'gh auth token': {e}")
    
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
        Tuple of (owner, repo) or None if parsing fails
    """
    if not support_url:
        return None
    
    # Pattern to match GitHub URLs
    # Matches: https://github.com/owner/repo or https://github.com/owner/repo/...
    pattern = r'https?://github\.com/([^/]+)/([^/?#]+)'
    match = re.search(pattern, support_url)
    
    if match:
        owner = match.group(1)
        repo = match.group(2)
        logger.debug(f"Parsed GitHub repo: {owner}/{repo} from {support_url}")
        return (owner, repo)
    
    logger.warning(f"Could not parse GitHub repo URL from: {support_url}")
    return None


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
                logger.debug(f"Rate limit remaining: {rate_limit.resources.core.remaining}/{rate_limit.resources.core.limit}")
            elif hasattr(rate_limit, 'rate'):
                logger.debug(f"Rate limit remaining: {rate_limit.rate.remaining}/{rate_limit.rate.limit}")
            else:
                logger.debug("Rate limit checked (structure unknown)")
        except Exception as e:
            logger.warning(f"Could not verify GitHub token: {e}")
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
            logger.error(f"Repository not found or access denied: {owner}/{repo}")
            logger.error("This may be a private repository. Ensure your token has 'repo' scope permissions.")
        else:
            logger.error(f"Repository not found: {owner}/{repo}")
            logger.error("If this is a private repository, set GH_TOKEN environment variable with a token that has 'repo' scope.")
    elif e.status == 403:
        logger.error("GitHub API rate limit exceeded or access forbidden")
        if get_github_token():
            logger.error("Check that your token has the necessary permissions (e.g., 'repo' scope for private repos)")
    else:
        logger.error(f"GitHub API error: {e.status} {e.data}")


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
                    'url': asset_data.get('browser_download_url', '')
                }
                # Include asset_id if available
                if 'id' in asset_data:
                    asset_info['id'] = asset_data['id']
                assets.append(asset_info)
            logger.debug(f"Found {len(assets)} assets from raw_data")
            return assets
    except (AttributeError, KeyError, TypeError) as e:
        logger.debug(f"Could not access raw_data, falling back to API call: {e}")
    
    # Fallback: use API call
    try:
        for asset in release.get_assets():
            asset_info = {
                'name': asset.name,
                'size': asset.size,
                'url': asset.browser_download_url,
                'id': asset.id
            }
            assets.append(asset_info)
        logger.debug(f"Found {len(assets)} assets from API call")
    except Exception as e:
        logger.debug(f"Error fetching assets via API: {e}")
    
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
        Tuple of (owner, repo, tag, asset_name) or None if parsing fails
        tag and asset_name may be None if not found in URL
    """
    if not url:
        return None
    
    # Pattern for release download URL: /releases/download/tag/asset-name
    download_pattern = r'https?://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)'
    match = re.match(download_pattern, url)
    if match:
        owner = match.group(1)
        repo = match.group(2)
        tag = match.group(3)
        asset_name = match.group(4)
        logger.debug(f"Parsed GitHub release download URL: {owner}/{repo} tag={tag} asset={asset_name}")
        return (owner, repo, tag, asset_name)
    
    # Pattern for release page URL: /releases/tag/tag-name
    tag_pattern = r'https?://github\.com/([^/]+)/([^/]+)/releases/tag/([^/?#]+)'
    match = re.match(tag_pattern, url)
    if match:
        owner = match.group(1)
        repo = match.group(2)
        tag = match.group(3)
        logger.debug(f"Parsed GitHub release tag URL: {owner}/{repo} tag={tag}")
        return (owner, repo, tag, None)
    
    logger.debug(f"Could not parse GitHub release URL: {url}")
    return None


def resolve_github_release_url(url: str) -> Optional[str]:
    """
    Resolve GitHub release URL to direct download URL using GitHub API.
    
    Takes a GitHub release URL and uses PyGithub to fetch the release and
    return the API endpoint URL for the asset. Supports authentication
    for private repositories (reads GH_TOKEN from environment variable).
    
    Args:
        url: GitHub release URL (download or tag URL)
        
    Returns:
        API endpoint URL for authenticated download or None on error
    """
    # Parse the URL
    parsed = parse_github_release_url(url)
    if not parsed:
        logger.error(f"Invalid GitHub release URL format: {url}")
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
                    logger.error(f"Release tag '{tag}' not found in {owner}/{repo}")
                else:
                    logger.error(f"Error fetching release: {e.status} {e.data}")
                return None
            
            # Find the asset by name
            assets = _get_assets_from_release(release)
            for asset_info in assets:
                if asset_info['name'] == asset_name:
                    # Prefer browser_download_url (works for public repos, simpler)
                    # Only use API endpoint if we have a token (for private repos)
                    github_token = get_github_token()
                    if github_token and 'id' in asset_info:
                        # For private repos with auth, use API endpoint
                        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_info['id']}"
                        logger.debug(f"Found asset: {asset_name} -> API endpoint (authenticated): {api_url}")
                        return api_url
                    else:
                        # For public repos or when no token, use browser_download_url
                        browser_url = asset_info.get('url', '')
                        if browser_url:
                            logger.debug(f"Found asset: {asset_name} -> browser_download_url: {browser_url}")
                            return browser_url
                        elif 'id' in asset_info:
                            # Fallback to API endpoint if browser_url not available
                            api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_info['id']}"
                            logger.debug(f"Found asset: {asset_name} -> API endpoint (fallback): {api_url}")
                            return api_url
            
            logger.error(f"Asset '{asset_name}' not found in release '{tag}'")
            return None
            
        except GithubException as e:
            _handle_github_exception(e, owner, repo)
            return None
        except Exception as e:
            logger.error(f"Unexpected error resolving GitHub release URL: {e}")
            return None
    
    # If we only have tag (no asset name), we need to find the asset
    # This is less common but we'll handle it
    if tag and not asset_name:
        logger.error("Cannot resolve GitHub release URL: asset name required")
        logger.error("Use format: https://github.com/owner/repo/releases/download/tag/asset-name")
        return None
    
    return None


def fetch_releases(owner: str, repo: str, include_prereleases: bool = False) -> Optional[List]:
    """
    Fetch GitHub releases using PyGithub.
    
    Reads GH_TOKEN from environment variable for authentication.
    
    Args:
        owner: GitHub repository owner
        repo: GitHub repository name
        include_prereleases: If True, include pre-releases. If False, filter them out.
        
    Returns:
        List of Release objects from PyGithub, or None on error
    """
    try:
        # Create GitHub client
        g = _create_github_client()
        logger.debug(f"Fetching releases from: {owner}/{repo}")
        repository = g.get_repo(f"{owner}/{repo}")
        
        # Get all releases (sorted by date, latest first)
        all_releases = list(repository.get_releases())
        
        if include_prereleases:
            releases = all_releases
            logger.debug(f"Fetched {len(releases)} releases (including pre-releases)")
        else:
            # Filter out pre-releases
            releases = [r for r in all_releases if not r.prerelease]
            logger.debug(f"Fetched {len(all_releases)} total releases, {len(releases)} regular releases (excluding pre-releases)")
        
        return releases
                
    except GithubException as e:
        _handle_github_exception(e, owner, repo)
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching releases: {e}")
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
    
    logger.debug(f"Searching for image in latest release: {latest_release.tag_name}")
    
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
            logger.debug(f"Found candidate image file: {asset_info['name']} ({size} bytes)")
            
            if size > largest_size:
                largest_size = size
                largest_asset = asset_info
    
    if largest_asset:
        logger.debug(f"Selected largest image file: {largest_asset['name']} ({largest_size} bytes)")
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
    
    logger.debug(f"Searching for batch update matching pattern: tsOS-*-arm64-update-{current_version}-to-*.tar")
    
    # Limit search to most recent releases to reduce API calls
    # The "next" update should logically be in recent releases
    releases_to_check = releases[:max_releases]
    logger.debug(f"Checking {len(releases_to_check)} most recent releases (out of {len(releases)} total)")
    
    # Search through recent releases (they should be sorted by date, latest first)
    for release in releases_to_check:
        # Get assets from release
        assets = _get_assets_from_release(release)
        if not assets:
            logger.debug(f"No assets found for release {release.tag_name}")
            continue
        
        # Check asset names against pattern
        for asset_info in assets:
            name = asset_info['name']
            match = re.match(pattern, name)
            if match:
                variant = match.group(1)
                next_version = match.group(2)
                logger.debug(f"Found applicable batch update: {name} (variant: {variant}, updates to: {next_version})")
                return asset_info['url']
    
    logger.debug(f"No applicable batch update found for version {current_version} in {len(releases_to_check)} recent releases")
    return None

