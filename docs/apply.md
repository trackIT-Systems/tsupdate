# Apply Module

The `apply` module provides functionality for applying pidiff (incremental) updates to the inactive partition.

## Overview

This module applies rsync-based incremental updates to the inactive partition. Updates are packaged as tar archives containing an rsync batch file and metadata.

## Update Archive Format

Update archives contain:
- **`batch.sh`** - Shell script with metadata and rsync command
- **rsync batch file** - Binary rsync batch data for incremental update
- Batch file naming: `tsOS-{variant}-arm64-update-{from_version}-to-{to_version}.tar`

### Metadata in batch.sh

- `BASE_PRETTY_NAME` - Expected source OS version
- `UPDATED_PRETTY_NAME` - Target OS version after update
- `BASE_IMAGE` - Base image filename
- `UPDATED_IMAGE` - Updated image filename

## Key Components

### Archive Processing

**`extract_update_archive(archive_path)`**: Extract tar archive to temporary directory.

**`parse_batch_metadata(batch_sh_path)`**: Parse metadata variables from `batch.sh`.

**`extract_rsync_command_from_batch_sh(batch_sh_path)`**: Extract rsync options and filter rules.

**`find_rsync_batch_file(extracted_dir, update_filename)`**: Locate rsync batch file in extracted archive.

### Version Validation

**`check_version_compatibility(base_pretty_name, target_os_release_path)`**: Verify target partition matches expected base version.

### Update Application

**`apply_rsync_batch(batch_file, target_dir, rsync_options, filter_rules)`**: Apply rsync batch file to target directory.

**`handle_rsync_exit_code(exit_code)`**: Interpret rsync exit codes with user-friendly messages.

**`execute_apply(update_source, keep_download)`**: Complete apply process including download, extraction, validation, and application.

## Update Process Flow

1. **Validation** - Check boot state (regular or persisted tryboot)
2. **Download** - Fetch update archive from URL (or use local file)
3. **Extract** - Extract tar archive to temporary directory
4. **Parse Metadata** - Read version info from `batch.sh`
5. **Mount** - Mount inactive partition to `/media/root-up`
6. **Version Check** - Verify base version matches target partition
7. **Apply** - Execute rsync batch to apply incremental changes
8. **Unmount** - Clean up mounts and temporary files

## Rsync Batch Application

The batch file contains:
- File changes (additions, modifications, deletions)
- Metadata preservation (permissions, timestamps)
- Optional filter rules provided via stdin

Rsync options typically include:
- `--recursive` - Traverse directories
- `--filter=._-` - Read filter rules from stdin
- `--read-batch=<file>` - Apply batch file
- `--stats` - Show transfer statistics
- `--itemize-changes` - Show detailed changes (verbose mode)

## Usage Examples

### Apply Update from URL

```python
from tsupdate.apply import execute_apply

# Apply update from GitHub release
exit_code = execute_apply(
    "https://github.com/owner/repo/releases/download/v1.0/update.tar",
    keep_download=False
)
```

### Apply Local Update

```python
# Apply from local file
exit_code = execute_apply("/path/to/update.tar", keep_download=True)
```

## CLI Commands

```bash
# Apply update from URL
sudo tsupdate apply https://example.com/update.tar

# Apply from local file
sudo tsupdate apply /path/to/update.tar

# Apply and keep downloaded file
sudo tsupdate apply https://example.com/update.tar --keep-download
```

## Complete Update Workflow

```bash
# 1. Check for available updates
tsupdate check

# 2. Apply incremental update
sudo tsupdate apply https://github.com/.../update-2025.12.1-to-2025.12.2.tar

# 3. Configure tryboot to test new version
sudo tsupdate tryboot

# 4. System reboots to new version (automatically)

# 5. Test new version, then persist if working correctly
sudo tsupdate persist

# Or rollback if something went wrong
sudo tsupdate rollback
```

## Restrictions

- Can only run when booted regularly or with persisted tryboot
- Requires matching base version in target partition
- Requires root privileges for partition mounting and modification

## Error Handling

### Common Rsync Exit Codes

- **0** - Success
- **1** - Syntax or usage error
- **2** - Protocol incompatibility
- **11** - File I/O error
- **23** - Partial transfer due to error
- **24** - Partial transfer due to vanished source files

### Version Mismatch

If target partition version doesn't match expected base version:
```
Error: Version mismatch
  Expected base version: tsOS 2025.12.1
  Found target version: tsOS 2025.12.0
```

## Safety Features

- **Version validation**: Ensures update is compatible with target partition
- **Automatic cleanup**: Removes temporary files even on failure
- **Mount context**: Automatic unmount on errors
- **State checking**: Validates boot state before applying
- **Download caching**: Reuses downloaded files to save bandwidth

## Creating Update Files

Update files (rsync batch archives) can be created using [pimod's pidiff tool](https://github.com/nature40/pimod):

```bash
# Install pimod
git clone https://github.com/nature40/pimod.git
cd pimod

# Create incremental update from two images
./pidiff.sh --tar --output=update.batch base.img updated.img

# This creates update.batch.tar containing:
# - update.batch (rsync batch file)
# - update.batch.sh (batch script with metadata)

# The tar archive can be applied using tsupdate
sudo tsupdate apply update.batch.tar
```

### pidiff Options

- `--partition=NUM` - Specify rootfs partition number (default: 2)
- `--output=PATH` - Set output batch file path
- `--tar` - Create tar archive (recommended for distribution)
- Additional rsync options can be passed through (e.g., `--exclude="*.log"`)

### Update File Naming Convention

For automatic version detection, name update files following the pattern:

```
tsOS-{variant}-arm64-update-{from_version}-to-{to_version}.tar
```

Examples:
- `tsOS-base-arm64-update-2025.12.1-to-2025.12.2.tar`
- `tsOS-pro-arm64-update-2025.12.1-to-2025.12.2.tar`

This naming allows `tsupdate check` to find applicable updates automatically.

