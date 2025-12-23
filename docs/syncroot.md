# Syncroot Module

The `syncroot` module provides partition mounting and syncing operations for A/B partition management.

## Overview

This module handles mounting/unmounting partitions and syncing filesystems between the active and inactive partitions. It's used for cloning the current system to the inactive partition before updates.

## Key Components

### Mount Points

- **`ROOT_RO`** (`/media/root-ro`) - Read-only root mount point
- **`ROOT_UP`** (`/media/root-up`) - Inactive partition mount point

### Core Functions

**`can_run_syncroot()`**: Check if operations can be run (requires regular boot or persisted tryboot).

**`get_inactive_partition_device()`**: Get device path of inactive partition.

**`mount_partition(device, mount_point)`**: Mount partition to specified mount point.

**`unmount_partition(mount_point)`**: Unmount partition from mount point.

**`mount_context(device, mount_point)`**: Context manager for automatic mount/unmount.

**`sync_root_partitions(source, destination)`**: Sync filesystems using rsync with archive mode and deletion.

### Command Functions

**`execute_syncroot()`**: Mount inactive partition, sync from `ROOT_RO`, and unmount.

**`execute_mount()`**: Mount inactive partition to `ROOT_UP` for manual access.

**`execute_unmount()`**: Unmount inactive partition from `ROOT_UP`.

## Sync Behavior

The sync operation uses rsync with the following options:
- **`-a`** (archive) - Preserves permissions, timestamps, ownership, and symlinks
- **`-h`** (human-readable) - Human-readable progress
- **`--stats`** - Show transfer statistics
- **`--delete`** - Delete files from destination that don't exist in source
- **`--itemize-changes`** - Show detailed changes (when verbose mode is enabled)

## Restrictions

Operations can only run when:
- System is booted regularly (not via tryboot), OR
- System is booted via tryboot AND configuration is persisted

This prevents accidental modifications to the wrong partition.

## Usage Examples

### Sync Root Partitions

```python
from tsupdate.syncroot import execute_syncroot

# Sync active partition to inactive partition
exit_code = execute_syncroot()
```

### Manual Mount/Unmount

```python
from tsupdate.syncroot import execute_mount, execute_unmount

# Mount inactive partition
execute_mount()

# ... perform operations on /media/root-up ...

# Unmount
execute_unmount()
```

### Using Context Manager

```python
from tsupdate.syncroot import mount_context, ROOT_UP

device = "/dev/mmcblk0p3"

with mount_context(device, ROOT_UP) as mount_point:
    # Partition is mounted here
    print(f"Mounted at: {mount_point}")
    # ... perform operations ...
# Partition is automatically unmounted
```

## CLI Commands

```bash
# Sync active partition to inactive partition
tsupdate syncroot

# Mount inactive partition for manual access
tsupdate mount

# Unmount inactive partition
tsupdate unmount
```

## Common Use Cases

### Cloning Current System

```bash
# Clone current system to inactive partition
sudo tsupdate syncroot
```

### Manual Partition Access

```bash
# Mount for inspection or modification
sudo tsupdate mount

# Inspect files
ls /media/root-up

# Unmount when done
sudo tsupdate unmount
```

### Pre-Update Sync

Before applying updates, sync the current system to ensure the inactive partition is a clean clone:

```bash
sudo tsupdate syncroot
sudo tsupdate apply update.tar
sudo tsupdate tryboot
```

## Safety Features

- **State validation**: Checks boot state before allowing operations
- **Mount point checks**: Verifies mount/unmount state before operations
- **Context managers**: Automatic cleanup even on errors
- **Rsync with delete**: Ensures inactive partition is exact mirror

