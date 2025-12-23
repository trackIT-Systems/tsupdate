# tsupdate

Update daemon for tsOS-based Raspberry Pi devices with A/B partition management and safe rollback.

## Overview

`tsupdate` manages OS updates on Raspberry Pi devices using an A/B partition scheme with the tryboot mechanism for safe, atomic updates with automatic rollback.

## Features

- **System status monitoring** - View OS version, partitions, and boot method
- **A/B partition management** - Dual root partitions (rootfs/clonefs) for safe updates
- **Tryboot integration** - Hardware-backed boot failsafe with automatic rollback
- **Incremental updates** - Apply rsync-based differential updates (pidiff)
- **Image restoration** - Restore complete OS images from various formats
- **GitHub integration** - Check for available updates from GitHub releases
- **Partition operations** - Mount, unmount, and sync partitions
- **Boot management** - Configure, persist, and rollback boot configurations

## Installation

```bash
# Install from source
git clone https://github.com/trackit-systems/tsupdate.git
cd tsupdate
pip install -e .
```

## Commands

### System Information

**`status`** - Display current system status (OS version, partitions, boot method)

```bash
tsupdate status          # Human-readable output
tsupdate status --json   # JSON output
```

### Boot Management

**`tryboot`** - Configure tryboot to switch to alternate partition

```bash
sudo tsupdate tryboot              # Configure and reboot automatically
sudo tsupdate tryboot --no-reboot  # Configure without reboot
```

**`persist`** - Make tryboot configuration permanent

```bash
sudo tsupdate persist           # Persist configuration
sudo tsupdate persist --reboot  # Persist and reboot
```

**`rollback`** - Revert from tryboot to previous partition

```bash
sudo tsupdate rollback              # Rollback and reboot automatically
sudo tsupdate rollback --no-reboot  # Rollback without reboot
```

### Updates

**`check`** - Check GitHub releases for available updates

```bash
tsupdate check                                    # Check for updates
tsupdate check --pre                              # Include pre-releases
tsupdate check --max-releases 10                  # Check more releases
tsupdate check --github-url https://github.com/...  # Custom repository
```

**`apply`** - Apply incremental update to inactive partition

```bash
sudo tsupdate apply <url>          # Apply from URL
sudo tsupdate apply <file>         # Apply from local file
sudo tsupdate apply <url> -k       # Keep downloaded file
```

**`restore`** - Restore OS image to inactive partition

```bash
sudo tsupdate restore <url>        # Restore from URL
sudo tsupdate restore <file>       # Restore from local file
sudo tsupdate restore <url> -p 3   # Use partition 3 from image
sudo tsupdate restore <url> -k     # Keep downloaded image
```

### Partition Operations

**`syncroot`** - Sync active partition to inactive partition

```bash
sudo tsupdate syncroot   # Clone current system to inactive partition
```

**`mount`** - Mount inactive partition to `/media/root-up`

```bash
sudo tsupdate mount   # Mount for manual access
```

**`unmount`** - Unmount inactive partition

```bash
sudo tsupdate unmount   # Unmount after manual access
```

## Typical Workflows

### Incremental Update

```bash
# 1. Check for updates
tsupdate check

# 2. Apply incremental update
sudo tsupdate apply https://github.com/.../update-2025.12.1-to-2025.12.2.tar

# 3. Reboot to test (automatic)

# 4. Persist if working correctly
sudo tsupdate persist

# Or rollback if issues found
sudo tsupdate rollback
```

### Full Image Restore

```bash
# 1. Check for latest release
tsupdate check

# 2. Restore OS image
sudo tsupdate restore https://github.com/.../tsos-2025.12.2.img.gz

# 3. Reboot to test (automatic)

# 4. Persist or rollback
sudo tsupdate persist
```

### Manual Partition Inspection

```bash
# Mount inactive partition
sudo tsupdate mount

# Inspect or modify files
ls /media/root-up
sudo nano /media/root-up/etc/config

# Unmount when done
sudo tsupdate unmount
```

## Creating Updates

Incremental update files (rsync batch files) can be created using [pimod's pidiff tool](https://github.com/nature40/pimod):

```bash
# Compare two images and create an incremental update
pidiff --tar base.img updated.img

# This creates a tar archive containing the rsync batch file
# which can be applied using: tsupdate apply update.tar
```

## GitHub Authentication

For private repositories, set `GH_TOKEN` environment variable or use GitHub CLI:

```bash
# Using environment variable
export GH_TOKEN="ghp_..."

# Or authenticate with GitHub CLI
gh auth login

# Commands now work with private repositories
tsupdate check
sudo tsupdate apply https://github.com/private-org/repo/releases/download/...
```

## Requirements

- Python 3.11+
- Raspberry Pi with tryboot support (Pi 4, 400, CM4, Pi 5)
- A/B partition layout - mmcblk0p2 (rootfs) and mmcblk0p3 (clonefs)
- Root privileges for most operations

## Documentation

Detailed documentation for each module is available in the `docs/` directory:

- **[status.md](docs/status.md)** - System status and partition detection
- **[tryboot.md](docs/tryboot.md)** - Tryboot configuration and management
- **[syncroot.md](docs/syncroot.md)** - Partition mounting and syncing
- **[apply.md](docs/apply.md)** - Incremental update application
- **[restore.md](docs/restore.md)** - OS image restoration
- **[github.md](docs/github.md)** - GitHub API integration
- **[utils.md](docs/utils.md)** - File download and caching utilities

## Version Scheme

tsupdate uses time-based versioning: `YEAR.MONTH.COUNT`

Examples: `2025.12.1`, `2025.12.2`, `2026.01.1`

## Authors

Jonas Höchst <hoechst@trackit.systems>

## Links

- [Raspberry Pi Tryboot Documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#fail-safe-os-updates-tryboot)
- [pimod - Tool for creating Raspberry Pi images and incremental updates](https://github.com/nature40/pimod)
