# Tryboot Module

The `tryboot` module provides functionality for configuring Raspberry Pi tryboot, persisting boot configurations, and rolling back failed updates.

## Overview

Raspberry Pi's tryboot mechanism provides hardware-level boot failsafe for safe A/B partition updates. If a new partition fails to boot properly, the system automatically reverts to the previous working partition.

## Key Components

### Boot Configuration Files

Located in `/boot/firmware/`:
- **`cmdline.txt`** - Current boot command line (specifies root partition)
- **`tryline.txt`** - Alternate boot command line for tryboot
- **`config.txt`** - Current boot configuration
- **`tryboot.txt`** - Boot configuration for tryboot attempts

### Core Functions

**`is_regular_boot()`**: Check if system is booted regularly (not via tryboot).

**`get_current_root_partition()`**: Get current root partition number (2 or 3).

**`get_target_partition()`**: Determine target partition for tryboot (switches between 2 and 3).

**`modify_cmdline_partition(cmdline, target_partition)`**: Modify cmdline string to use target partition.

**`is_tryboot_persisted()`**: Check if tryboot config matches current cmdline (indicates persistence).

### Tryboot Configuration

**`copy_and_modify_cmdline(target_partition, target_label)`**: Create `tryline.txt` with switched partition.

**`copy_config_to_tryboot()`**: Copy `config.txt` to `tryboot.txt`.

**`add_cmdline_entry()`**: Add `cmdline=tryline.txt` to `tryboot.txt`.

**`execute_tryboot(reboot)`**: Complete tryboot configuration process and optionally reboot.

### Persistence

**`persist_boot_configuration()`**: Copy `tryline.txt` to `cmdline.txt` to make tryboot permanent.

**`execute_persist(reboot)`**: Execute persistence process with validation and optional reboot.

### Rollback

**`rollback_tryboot(reboot=True)`**: Restore previous partition configuration and remove tryboot files. By default, automatically reboots the system after rollback.

## Tryboot Workflow

### Initial Setup (Regular Boot)
1. System boots from partition A (rootfs)
2. User runs `tsupdate tryboot`
3. Creates `tryline.txt` pointing to partition B (clonefs)
4. Creates `tryboot.txt` with `cmdline=tryline.txt`
5. System reboots with `reboot "0 tryboot"`

### Tryboot Attempt
1. Firmware loads `tryboot.txt` and `tryline.txt`
2. System boots from partition B
3. Boot counter tracks boot attempts
4. If boot succeeds, user runs `tsupdate persist`

### Persistence
1. User runs `tsupdate persist` (when booted via tryboot)
2. Copies `tryline.txt` to `cmdline.txt`
3. Partition B becomes the default boot partition
4. Normal reboot now boots from partition B

### Automatic Rollback (Hardware)
If the system fails to boot from partition B:
- Boot counter reaches limit
- Firmware automatically boots from partition A
- No user intervention required

### Manual Rollback
If booted via tryboot but want to revert:
1. User runs `tsupdate rollback`
2. Restores `cmdline.txt` to point to previous partition
3. Removes `tryboot.txt` and `tryline.txt`
4. System reboots to previous partition

## Usage Examples

### Configure Tryboot

```python
from tsupdate.tryboot import execute_tryboot

# Configure tryboot and reboot
exit_code = execute_tryboot(reboot=True)
```

### Persist Configuration

```python
from tsupdate.tryboot import execute_persist

# Persist and reboot
exit_code = execute_persist(reboot=True)
```

### Rollback

```python
from tsupdate.tryboot import rollback_tryboot

# Rollback and reboot (default behavior)
exit_code = rollback_tryboot(reboot=True)

# Rollback without reboot
exit_code = rollback_tryboot(reboot=False)
```

## CLI Commands

```bash
# Configure tryboot (auto-reboots by default)
tsupdate tryboot

# Configure without auto-reboot
tsupdate tryboot --no-reboot

# Persist configuration (when booted via tryboot)
tsupdate persist

# Persist and reboot
tsupdate persist --reboot

# Rollback from tryboot (auto-reboots by default)
tsupdate rollback

# Rollback without auto-reboot
tsupdate rollback --no-reboot
```

## Safety Features

- **Validation**: Checks boot state before allowing operations
- **Persistence check**: Prevents double-tryboot without persistence
- **Automatic hardware rollback**: Firmware-level failsafe for boot failures
- **Manual rollback**: Software rollback when booted via tryboot



