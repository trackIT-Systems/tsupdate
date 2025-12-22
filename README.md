# tsupdate

A tool for monitoring system status on tsOS-based Raspberry Pi devices, including OS version, partition information, and tryboot boot method detection.

## Overview

`tsupdate` is a Python-based tool for monitoring system status on tsOS-based Raspberry Pi devices. It provides information about:

- **OS version**: Reads and displays version information from `/etc/os-release`
- **A/B partition scheme**: Detects active and inactive partitions (rootfs/clonefs)
- **Raspberry Pi tryboot**: Identifies whether the system was booted via tryboot or regular boot

## Current Status

### Implemented Features

- **System status monitoring**: Query OS version, partition information, and boot method
- **Partition detection**: Automatically detects active and inactive partitions
- **Tryboot detection**: Identifies whether the system was booted via tryboot or regular boot
- **Tryboot configuration**: Configure tryboot to switch to alternate partition
- **Boot configuration persistence**: Persist tryboot configuration to make it permanent
- **OS release parsing**: Reads and parses `/etc/os-release` information

## A/B Partition Layout

tsOS devices use a dual root partition layout:

- **mmcblk0p2 (rootfs)**: Primary root partition (A)
- **mmcblk0p3 (clonefs)**: Backup/alternate root partition (B)

At any time, one partition is active (currently booted) and the other is inactive.

## Tryboot Mechanism

The Raspberry Pi's tryboot feature provides hardware-level boot selection:

- On first boot attempt, tryboot loads the new partition
- A boot counter tracks boot attempts
- If the system fails to boot properly (counter reaches limit), tryboot automatically reverts to the previous working partition

This ensures that a bad update can never brick the device.

## Version Scheme

tsupdate uses a time-based versioning scheme:

```
<YEAR>.<MONTH>.<COUNT>

Examples:
  2025.12.1  → First release in December 2025
  2025.12.2  → Second release in December 2025
  2026.01.1  → First release in January 2026
```

Version information is extracted from `/etc/os-release` (specifically the `VERSION_ID` and `VERSION_COMMIT` fields).

## CLI Commands

### Available Commands

```bash
# Show current system status
tsupdate status

# Show status in JSON format
tsupdate status --json

# Configure tryboot to switch to alternate partition
tsupdate tryboot

# Configure tryboot and automatically reboot
tsupdate tryboot --reboot

# Persist current boot configuration (when booted via tryboot)
tsupdate persist

# Persist configuration and automatically reboot
tsupdate persist --reboot

# Show version
tsupdate --version
```

### Command Details

#### `status`

Display system status (OS version, partitions, boot method).

**Options:**
- `--json`: JSON output

#### `tryboot`

Configure tryboot to switch to alternate partition. Requires regular boot.

**Options:**
- `--reboot`, `-r`: Reboot after configuration

After configuration, reboot using: `reboot "0 tryboot"`

#### `persist`

Persist current tryboot configuration. Requires tryboot boot.

**Options:**
- `--reboot`, `-r`: Reboot after persistence

## Project Structure

```
tsupdate/
├── README.md                   # This file
├── pyproject.toml              # Project metadata and dependencies
└── src/
    └── tsupdate/
        ├── __init__.py         # Package initialization
        ├── __main__.py         # CLI entry point
        ├── cli.py              # Command-line interface
        ├── status.py           # Status tracking and system information
        └── tryboot.py          # Tryboot configuration and persistence
```

## Installation

```bash
# Install from source
git clone https://github.com/trackit-systems/tsupdate.git
cd tsupdate
pip install -e .

# Or install from PyPI (when published)
pip install tsupdate
```

After installation, you can run `tsupdate` directly:

```bash
tsupdate status
```

## Development

```bash
# Clone repository
git clone https://github.com/trackit-systems/tsupdate.git
cd tsupdate

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Code formatting
black src/
ruff check src/

# Type checking
mypy src/
```

## Requirements

- Python 3.11+
- Raspberry Pi with tryboot support (Raspberry Pi 4, 400, CM4, Pi 5)
- Two root partitions (A/B layout) - mmcblk0p2 (rootfs) and mmcblk0p3 (clonefs)

## Dependencies

Currently, tsupdate uses only Python standard library modules:
- `argparse` - CLI argument parsing
- `json` - JSON output formatting
- `pathlib` - Path handling
- `subprocess` - System command execution
- `re` - Regular expressions
- `typing` - Type hints

No external dependencies are required.

## Architecture Notes

### Partition Detection

The tool detects the currently active partition by parsing `/proc/cmdline`:

```python
# Example: root=/dev/mmcblk0p2 → active partition is p2
# Inactive partition will be p3
```

Partition labels are read using `lsblk` or `blkid` to identify rootfs vs clonefs.

### Tryboot Detection

Tryboot status is detected by reading the device tree node:

```
/sys/firmware/devicetree/base/chosen/bootloader/tryboot
```

A non-zero value indicates the system was booted via tryboot.

### Tryboot Configuration

The `tryboot` command configures the system to boot from the alternate partition on the next reboot:

1. Reads the current `cmdline.txt` from `/boot/firmware/`
2. Creates `tryline.txt` with the partition switched (rootfs ↔ clonefs)
3. Copies `config.txt` to `tryboot.txt`
4. Adds `cmdline=tryline.txt` entry to `tryboot.txt`

On reboot with `reboot "0 tryboot"`, the system will boot from the alternate partition.

### Boot Configuration Persistence

The `persist` command makes the current tryboot configuration permanent:

1. Reads `tryline.txt` from `/boot/firmware/`
2. Writes it back to `cmdline.txt`

This ensures that after a successful tryboot, the new partition becomes the default boot partition.

### OS Release Parsing

The tool reads `/etc/os-release` from the booted system and extracts:
- OS name and pretty name
- Version information (VERSION_ID, VERSION_COMMIT)
- Support URLs and other metadata

## References

- [Raspberry Pi Tryboot](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#fail-safe-os-updates-tryboot) - Hardware boot failsafe mechanism

## Authors

Jonas Höchst <hoechst@trackit.systems>
