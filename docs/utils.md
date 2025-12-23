# Utils Module

The `utils` module provides utility functions for file downloads, caching, and cleanup operations.

## Overview

This module handles downloading files from URLs, caching them locally, and managing temporary files. It integrates with the GitHub module for authenticated downloads from private repositories.

## Key Components

### Artifacts Directory

**`ARTIFACTS_DIR`** - `/data/tsupdate/` - Local cache for downloaded files

### Core Functions

**`safe_cleanup(path)`**: Safely remove a file, ignoring errors (useful for cleanup operations).

**`download_file(url, output_path)`**: Download file from URL with support for GitHub authentication.

**`ensure_file(source, artifacts_path)`**: Ensure file is available locally, downloading if needed.

## Download Behavior

### Standard Downloads

For regular URLs:
- Uses `urllib.request.urlretrieve`
- Downloads to temporary file first
- Renames to final path on success (atomic operation)
- Progress logging every 100 blocks

### GitHub Downloads

For GitHub URLs, automatically uses authentication if `GH_TOKEN` is set:
- Detects GitHub URLs (github.com and api.github.com)
- Adds `Authorization: Bearer <token>` header
- Uses authenticated request for private repositories
- Progress logging every 100 chunks (800KB)

## Caching System

The `ensure_file()` function implements smart caching:

1. **Local files**: Returns path directly if file exists
2. **Cached downloads**: Reuses existing download if file exists in artifacts directory
3. **New downloads**: Downloads to artifacts directory and returns path
4. **Size validation**: Checks file size > 0 before reusing

### Cache Benefits

- Saves bandwidth by reusing downloads
- Speeds up repeated operations
- Handles interrupted downloads (removes invalid files)

## Usage Examples

### Download File

```python
from tsupdate.utils import download_file
from pathlib import Path

url = "https://example.com/file.tar.gz"
output_path = Path("/tmp/file.tar.gz")

success = download_file(url, output_path)
if success:
    print(f"Downloaded to {output_path}")
```

### Ensure File Availability

```python
from tsupdate.utils import ensure_file

# URL or local path
source = "https://github.com/owner/repo/releases/download/v1.0/update.tar"

# Ensure file is available (downloads if needed, reuses if cached)
file_path, is_local = ensure_file(source)

if file_path:
    print(f"File available at: {file_path}")
    print(f"Was local: {is_local}")
```

### Safe Cleanup

```python
from tsupdate.utils import safe_cleanup
from pathlib import Path

temp_file = Path("/tmp/temp-file.tmp")

# Remove file, ignore errors if it doesn't exist
safe_cleanup(temp_file)
```

## GitHub URL Resolution

For GitHub release URLs, the module:

1. Detects GitHub release URL format
2. Extracts asset name from URL
3. Calls `github.resolve_github_release_url()` to get API endpoint
4. Downloads using authenticated request

### Supported GitHub URL Formats

- `https://github.com/owner/repo/releases/download/tag/asset-name`
- `https://api.github.com/repos/owner/repo/releases/assets/12345`

### Example

```python
# GitHub release URL
url = "https://github.com/owner/repo/releases/download/v1.0/tsos.img.gz"

# Automatically resolves to API endpoint and downloads with authentication
file_path, is_local = ensure_file(url)
```

## Atomic Operations

Downloads use atomic file operations to prevent corruption:

1. Download to `.tmp` file (e.g., `file.tar.tmp`)
2. Verify download completed successfully
3. Rename to final name (atomic operation)
4. On failure, remove `.tmp` file

This ensures:
- No partial/corrupt files in artifacts directory
- Safe interruption handling
- Cache integrity

## Error Handling

### Download Failures

Common download errors:
- Network connection failure
- 404 Not Found
- 403 Forbidden (authentication required)
- Timeout
- Disk space issues

On error:
- Logs error message
- Removes temporary file
- Returns `False` or `(None, False)`

### Cleanup Failures

`safe_cleanup()` ignores all errors:
- File doesn't exist
- Permission denied
- File in use

This is intentional for cleanup operations where failure is acceptable.

## Integration with Other Modules

### Apply Module

```python
from tsupdate.apply import execute_apply

# ensure_file is called internally
execute_apply("https://example.com/update.tar", keep_download=False)
```

### Restore Module

```python
from tsupdate.restore import execute_restore

# ensure_file is called internally
execute_restore("https://example.com/image.img.gz", partition=2, keep_image=False)
```

## File Naming

For downloads, the filename is determined by:

1. **GitHub releases**: Use asset name from URL
2. **Other URLs**: Use filename from URL path
3. **Fallback**: Use "downloaded-file" if no filename available

## Storage Management

### Artifacts Directory

Default location: `/data/tsupdate/`

Create if it doesn't exist:
```python
artifacts_path.mkdir(parents=True, exist_ok=True)
```

### Cleanup Options

When using `apply` or `restore` commands:

- **Default**: Remove downloaded files after operation
- **`--keep-download` / `--keep-image`**: Preserve files in artifacts directory
- **Local files**: Never deleted (source file is unchanged)

### Manual Cleanup

```bash
# List cached files
ls -lh /data/tsupdate/

# Remove old downloads
sudo rm /data/tsupdate/old-file.tar

# Clear entire cache
sudo rm -rf /data/tsupdate/*
```

## Progress Logging

Download progress is logged in verbose mode:

```bash
# Enable verbose logging
sudo tsupdate -v apply https://example.com/large-file.tar
```

Output includes:
- Download start message
- Progress percentage (every 100 blocks/chunks)
- Completion message with file path

## Security Considerations

### URL Validation

- No validation of URL safety (user responsibility)
- Downloads to controlled directory (`/data/tsupdate/`)
- Atomic operations prevent partial file corruption

### Authentication

- GitHub tokens used only for GitHub URLs
- Token obtained from environment or GitHub CLI
- Token never logged or exposed

### File Permissions

- Downloads inherit umask permissions
- Artifacts directory requires write access
- Root permissions required for `/data/tsupdate/` on typical systems

## Performance

### Streaming Downloads

- Uses chunked reading (8KB chunks for standard, 8MB for extraction)
- Memory efficient for large files (GB+)
- No loading entire file into memory

### Caching

- Avoids redundant downloads
- Checks file existence before downloading
- Validates file size to ensure completeness

### Temporary Files

- Uses `.tmp` suffix during download
- Cleaned up automatically on success or failure
- Atomic rename prevents race conditions

