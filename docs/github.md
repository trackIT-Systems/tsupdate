# GitHub Module

The `github` module provides integration with GitHub Releases API for checking available updates and downloading release assets.

## Overview

This module enables tsupdate to query GitHub releases, find applicable updates, and resolve download URLs for both public and private repositories.

## Authentication

GitHub authentication is obtained from two sources (in order of priority):

1. **`GH_TOKEN` environment variable** - Personal access token
2. **GitHub CLI** - Token from `gh auth token` command

For private repositories, the token must have `repo` scope permissions.

## Key Components

### Token Management

**`initialize_github_token()`**: Load token at program start (called once in `cli.py`).

**`get_github_token()`**: Retrieve the loaded token.

### URL Parsing

**`parse_github_repo_url(support_url)`**: Extract owner/repo from GitHub URLs.

**`parse_github_release_url(url)`**: Parse release URLs to extract owner, repo, tag, and asset name.

**`resolve_github_release_url(url)`**: Convert release URL to authenticated API endpoint.

### Release Fetching

**`fetch_releases(owner, repo, include_prereleases)`**: Get list of releases from repository.

**`find_latest_image(releases)`**: Find latest OS image in releases.

**`find_applicable_batch_update(releases, current_version, max_releases)`**: Find incremental update matching current version.

## Release File Patterns

### OS Images

Matches files with image extensions:
- `.img`
- `.gz` (gzip compressed)
- `.xz` (xz compressed)
- `.zip` (zip archive)

Selects the **largest file** among candidates.

### Batch Updates

Pattern: `tsOS-{variant}-arm64-update-{current_version}-to-{next_version}.tar`

Examples:
- `tsOS-base-arm64-update-2025.12.1-to-2025.12.2.tar`
- `tsOS-pro-arm64-update-2025.12.1-to-2025.12.2.tar.gz`

## Usage Examples

### Check for Updates

```python
from tsupdate.status import read_booted_os_release
from tsupdate.github import parse_github_repo_url, fetch_releases, find_latest_image, find_applicable_batch_update

# Get current version
os_release = read_booted_os_release()
version_id = os_release.version_id
support_url = os_release.get("SUPPORT_URL")

# Parse repository
owner, repo = parse_github_repo_url(support_url)

# Fetch releases
releases = fetch_releases(owner, repo, include_prereleases=False)

# Find latest image
image_url = find_latest_image(releases)
print(f"Latest image: {image_url}")

# Find applicable batch update
batch_url = find_applicable_batch_update(releases, version_id, max_releases=5)
print(f"Next update: {batch_url}")
```

### Resolve Private Repository URL

```python
from tsupdate.github import resolve_github_release_url

# Public URL from release page
url = "https://github.com/owner/repo/releases/download/v1.0/tsos.img.gz"

# Resolve to authenticated API endpoint (uses GH_TOKEN)
api_url = resolve_github_release_url(url)

# Use api_url with utils.download_file() for authenticated download
```

## CLI Integration

The `check` command uses this module:

```bash
# Check for updates using SUPPORT_URL from /etc/os-release
tsupdate check

# Include pre-releases
tsupdate check --pre

# Check more releases for batch updates (default: 5)
tsupdate check --max-releases 10

# Override repository URL
tsupdate check --github-url https://github.com/owner/repo
```

## Environment Variable

Set `GH_TOKEN` for GitHub authentication:

```bash
# Export token
export GH_TOKEN="ghp_..."

# Check for updates (uses token)
tsupdate check

# Apply update from private repository (uses token)
sudo tsupdate apply https://github.com/owner/private-repo/releases/download/v1.0/update.tar
```

Or use GitHub CLI:

```bash
# Authenticate with GitHub CLI
gh auth login

# Check for updates (uses gh token automatically)
tsupdate check
```

## Rate Limiting

### Unauthenticated Requests
- **60 requests per hour** per IP address
- Sufficient for occasional checks

### Authenticated Requests
- **5,000 requests per hour** per user
- Recommended for automated systems

The module logs rate limit information in verbose mode:

```bash
tsupdate -v check
```

## Error Handling

### Common Errors

**404 Not Found**:
- Repository doesn't exist or is private
- For private repos, ensure `GH_TOKEN` is set with `repo` scope

**403 Forbidden**:
- Rate limit exceeded
- Token lacks required permissions

**Network Errors**:
- Connection timeout
- DNS resolution failure

### Error Messages

The module provides helpful error messages:

```
Repository not found: owner/repo
If this is a private repository, set GH_TOKEN environment variable with a token that has 'repo' scope.
```

## PyGithub Integration

This module uses the PyGithub library for GitHub API access. It provides:
- Automatic pagination
- Rate limit handling
- Response caching
- Type safety

### Client Creation

The `_create_github_client()` function handles authentication and validation:
- Creates authenticated client if token available
- Verifies token by checking rate limit
- Logs authentication status in verbose mode

### Asset Access

The `_get_assets_from_release()` function optimizes API usage:
- Tries to use raw_data first (no API call)
- Falls back to API call if needed
- Returns consistent asset information format

## Private Repository Support

For private repositories:

1. **Create personal access token**:
   - Go to GitHub Settings → Developer settings → Personal access tokens
   - Generate token with `repo` scope
   - Save token securely

2. **Set environment variable**:
   ```bash
   export GH_TOKEN="ghp_your_token_here"
   ```

3. **Use tsupdate commands normally**:
   ```bash
   tsupdate check
   sudo tsupdate apply https://github.com/owner/private-repo/releases/download/v1.0/update.tar
   ```

## Security Considerations

- **Token storage**: Never commit tokens to version control
- **Token scope**: Use minimal required scope (`repo` for private repos)
- **Token rotation**: Regularly rotate tokens
- **Token expiration**: Use tokens with expiration dates
- **CLI integration**: GitHub CLI stores tokens securely

## Version Compatibility

The module works with VERSION_ID format: `YYYY.MM.N`

Examples:
- `2025.12.1` - First release in December 2025
- `2025.12.2` - Second release in December 2025

Batch update matching is strict - requires exact version match.

