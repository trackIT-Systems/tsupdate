"""Utility functions for file downloads and management."""

import logging
from pathlib import Path
from typing import Optional, Tuple

import requests

from tsupdate.github import parse_github_release_url, resolve_github_release_url, get_github_token

logger = logging.getLogger(__name__)


# Artifacts directory for downloaded files
ARTIFACTS_DIR = Path("/data/tsupdate")


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


def download_file(url: str, output_path: Path) -> bool:
    """
    Download file from URL with resumable download support.
    
    Downloads to a temporary file first, then renames to the final path
    on success to avoid corrupt files if interrupted. Automatically resumes
    from existing .tmp files if present.
    
    For GitHub release URLs, uses authenticated requests if GH_TOKEN is set.
    
    Args:
        url: URL to download from
        output_path: Path where to save the downloaded file
        
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
    
    # Check if this is a GitHub release URL
    download_url = source
    asset_name = None
    if source.startswith("https://github.com/") and "/releases/" in source:
        logger.debug(f"Detected GitHub release URL: {source}")
        # Extract asset name from original URL before resolving
        # Format: https://github.com/owner/repo/releases/download/tag/asset-name
        if "/releases/download/" in source:
            # Direct download URL - use it directly without resolving
            # This works for public repos and avoids API calls
            asset_name = source.split("/releases/download/")[1].split("/", 1)[-1]
            logger.debug(f"Using direct GitHub download URL: {source}")
            logger.debug(f"Extracted asset name from URL: {asset_name}")
            download_url = source  # Use original URL directly
        else:
            # Tag URL or other format - resolve via API
            # Use GitHub API to resolve the URL (supports private repos with token from env)
            resolved_url = resolve_github_release_url(source)
            if resolved_url:
                download_url = resolved_url
                logger.debug(f"Resolved GitHub release URL to: {download_url}")
            else:
                logger.error(f"Failed to resolve GitHub release URL: {source}")
                return (None, False)
    
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
    
    downloaded_path = artifacts_path / filename
    
    # Ensure artifacts directory exists
    try:
        artifacts_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Using artifacts directory: {artifacts_path}")
    except OSError as e:
        logger.error(f"Could not create artifacts directory {artifacts_path}: {e}")
        return (None, False)
    
    # Check if file already exists (preserve .tmp files for resumable downloads)
    if downloaded_path.exists() and downloaded_path.is_file():
        file_size = downloaded_path.stat().st_size
        if file_size > 0:
            logger.info(f"File already exists: {downloaded_path} ({file_size} bytes)")
            logger.info("Skipping download, using existing file")
            return (downloaded_path, False)
        else:
            logger.warning(f"Existing file is empty, re-downloading")
            downloaded_path.unlink()
    
    # Download file
    logger.info(f"Downloading file from URL: {download_url}")
    if not download_file(download_url, downloaded_path):
        return (None, False)
    
    return (downloaded_path, False)




