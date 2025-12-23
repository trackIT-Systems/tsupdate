# Status Module

The `status` module provides system status tracking, partition detection, OS release parsing, and tryboot detection for tsOS devices.

## Overview

This module is responsible for gathering information about the current system state, including which partition is active, OS version information, and boot method detection.

## Key Components

### OS Release Information

**`OSRelease` class**: Represents parsed `/etc/os-release` information with convenient properties.

**`read_os_release(path)`**: Parse an os-release file from any path.

**`read_booted_os_release()`**: Read os-release from the currently booted system.

### Partition Detection

**`parse_cmdline()`**: Parse `/proc/cmdline` into a dictionary of kernel parameters.

**`get_root_partition_from_cmdline()`**: Extract the root partition device from cmdline.

**`get_partition_number(device)`**: Extract partition number from device path (e.g., `/dev/mmcblk0p2` → `2`).

**`get_partition_label(device)`**: Get partition label using `lsblk` or `blkid`.

**`get_booted_partition()`**: Returns tuple of `(device, partition_num, label)` for the currently active partition.

**`get_inactive_partition()`**: Returns tuple of `(device, partition_num, label)` for the inactive partition.

**`get_inactive_partition_info()`**: Returns inactive partition info as a dictionary.

### Tryboot Detection

**`is_tryboot_active()`**: Check if the system was booted via tryboot mechanism by reading the device tree node at `/sys/firmware/devicetree/base/chosen/bootloader/tryboot`.

### System Status

**`get_system_status()`**: Gather complete system status including OS info, partitions, and boot method.

**`format_status_text(status)`**: Format status as human-readable text.

**`format_status_json(status)`**: Format status as JSON.

## Partition Layout

tsOS uses an A/B partition scheme with two root partitions:
- **mmcblk0p2** - Primary root partition (rootfs)
- **mmcblk0p3** - Alternate root partition (clonefs)

At any time, one partition is active (booted) and the other is inactive (standby for updates).

## Usage Example

```python
from tsupdate.status import get_system_status, format_status_text

# Get complete system status
status = get_system_status()

# Display as text
print(format_status_text(status))

# Access specific fields
print(f"Active partition: {status['active_partition']}")
print(f"Boot method: {'tryboot' if status['booted_via_tryboot'] == 'True' else 'regular'}")
```

## CLI Integration

The `status` command uses this module to display system information:

```bash
# Human-readable output
tsupdate status

# JSON output
tsupdate status --json
```

