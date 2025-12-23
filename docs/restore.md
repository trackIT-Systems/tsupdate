# Restore Module

The `restore` module provides functionality for restoring complete OS images to the inactive partition.

## Overview

This module handles downloading, extracting, and restoring full OS images to the inactive partition. Unlike incremental updates (apply), restore replaces the entire partition contents.

## Supported Image Formats

- **Uncompressed**: `.img`
- **Gzip**: `.gz`, `.img.gz`
- **XZ**: `.xz`, `.img.xz`
- **Zip**: `.zip` (extracts largest .img file)

## Key Components

### Image Processing

**`find_largest_img_in_zip(zip_path)`**: Find and select the largest .img file in a zip archive.

**`extract_image(compressed_path, output_path)`**: Extract compressed image using streaming to avoid memory issues.

### Loopback Device Management

**`setup_loopback(image_path)`**: Create loopback device for image file and scan for partitions.

**`remove_loopback(loop_device)`**: Detach loopback device.

**`get_partition_device(loop_device, partition)`**: Get partition device path from loopback device.

### Restore Process

**`execute_restore(image_source, partition, keep_image)`**: Complete restore process including download, extraction, loopback setup, and filesystem sync.

## Restore Process Flow

1. **Download** - Fetch image from URL (or use local file)
2. **Extract** - Decompress image if needed (streaming for large files)
3. **Loopback Setup** - Create loopback device and expose partitions
4. **Mount Image** - Mount specified partition from image to `/media/image-rootfs`
5. **Mount Target** - Mount inactive partition to `/media/root-up`
6. **Sync** - Copy all files from image to inactive partition using rsync
7. **Cleanup** - Unmount partitions, detach loopback, remove temporary files

## Streaming Extraction

To handle large image files (2GB+) without excessive memory usage, extraction is performed in chunks:
- **Chunk size**: 8MB
- **Streaming**: Reads and writes in chunks
- **Temporary files**: Uses `.tmp` suffix during extraction for atomicity

## Usage Examples

### Restore from URL

```python
from tsupdate.restore import execute_restore

# Restore OS image from GitHub release
exit_code = execute_restore(
    image_source="https://github.com/owner/repo/releases/download/v1.0/tsos.img.gz",
    partition=2,
    keep_image=False
)
```

### Restore from Local File

```python
# Restore from local image
exit_code = execute_restore(
    image_source="/path/to/tsos.img.gz",
    partition=2,
    keep_image=True
)
```

### Custom Partition Selection

```python
# Use partition 3 from image (if image has non-standard layout)
exit_code = execute_restore(
    image_source="https://example.com/custom.img.xz",
    partition=3,
    keep_image=False
)
```

## CLI Commands

```bash
# Restore from URL (uses partition 2 by default)
sudo tsupdate restore https://example.com/tsos.img.gz

# Restore from local file
sudo tsupdate restore /path/to/tsos.img.gz

# Use different partition from image
sudo tsupdate restore https://example.com/image.img.xz --partition 3

# Keep downloaded image after restore
sudo tsupdate restore https://example.com/tsos.img.gz --keep-image
```

## Complete Restore Workflow

```bash
# 1. Check available releases
tsupdate check

# 2. Restore OS image to inactive partition
sudo tsupdate restore https://github.com/.../tsos-2025.12.2.img.gz

# 3. Configure tryboot to test restored image
sudo tsupdate tryboot

# 4. System reboots to restored image (automatically)

# 5. Test restored image, then persist if working correctly
sudo tsupdate persist

# Or rollback if something went wrong
sudo tsupdate rollback
```

## Loopback Device Management

The module uses Linux loopback devices to access partitions within image files:

```bash
# Loopback device example
/dev/loop0     -> entire image file
/dev/loop0p1   -> boot partition
/dev/loop0p2   -> root partition
/dev/loop0p3   -> alternate root partition
```

The `partprobe` command scans for partitions after loopback setup. If not available, partition devices should still be accessible.

## Partition Selection

Standard Raspberry Pi images typically have:
- **Partition 1**: Boot partition (FAT32)
- **Partition 2**: Root filesystem (ext4)

The `--partition` flag selects which partition from the image to restore to the inactive partition (default: 2).

## Restrictions

- Can only run when booted regularly or with persisted tryboot
- Requires root privileges for loopback management and partition mounting
- Target partition must not be the currently booted partition

## Caching and Cleanup

### Download Caching

Downloaded and extracted images are cached in `/data/tsupdate/`:
- Reuses existing downloads if file already exists
- Skips re-extraction if .img already exists
- Saves bandwidth and time for repeated operations

### Cleanup Behavior

- **Default**: Removes downloaded and extracted files after restore
- **With `--keep-image`**: Preserves files in `/data/tsupdate/`
- **Local files**: Never deleted (source is kept)
- **Temporary files**: Always cleaned up (even on errors)

## Safety Features

- **Streaming extraction**: Handles large files without memory issues
- **Atomic operations**: Uses temporary files with rename
- **Context managers**: Automatic cleanup of mounts
- **Loopback cleanup**: Detaches devices even on errors
- **State validation**: Checks boot state before restoring
- **Download validation**: Verifies file size before proceeding

