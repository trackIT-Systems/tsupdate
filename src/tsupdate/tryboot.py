"""Tryboot tool for switching boot partitions."""

import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from tsupdate.status import is_tryboot_active, get_booted_partition, get_inactive_partition

logger = logging.getLogger(__name__)


# Boot files directory
BOOT_DIR = Path("/boot/firmware")
CMDLINE_FILE = BOOT_DIR / "cmdline.txt"
TRYLINE_FILE = BOOT_DIR / "tryline.txt"
CONFIG_FILE = BOOT_DIR / "config.txt"
TRYBOOT_CONFIG_FILE = BOOT_DIR / "tryboot.txt"


def write_file_atomic(path: Path, content: str, preserve_times: bool = False) -> None:
    """
    Replace the contents of a file atomically.

    The new content is written to a temporary file in the same directory,
    flushed to disk and then renamed over the target, so a power cut leaves
    either the complete old or the complete new file - never a truncated one.
    This matters for cmdline.txt: a truncated file leaves the device unbootable.

    Args:
        path: File to write
        content: New file content
        preserve_times: Keep the atime/mtime of the existing file (if there is one)

    Raises:
        OSError: If the file could not be written (the original is left untouched)
    """
    old_times = None
    if preserve_times:
        try:
            st = path.stat()
            old_times = (st.st_atime, st.st_mtime)
        except FileNotFoundError:
            pass

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if old_times is not None:
            os.utime(tmp_path, old_times)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    # Persist the rename itself
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def is_regular_boot() -> bool:
    """
    Check if system is booted regularly (not via tryboot).
    
    Returns:
        True if regular boot, False if trybooted
    """
    return not is_tryboot_active()


def get_current_root_partition() -> Optional[int]:
    """
    Get current root partition number from booted system.
    
    Returns:
        Partition number (2 for rootfs, 3 for clonefs) or None
    """
    booted = get_booted_partition()
    if not booted:
        return None
    
    _, partition_num, _ = booted
    return partition_num


def get_target_partition() -> Optional[int]:
    """
    Determine target partition for tryboot.
    
    Returns:
        Target partition number (2 for rootfs, 3 for clonefs) or None
    """
    current = get_current_root_partition()
    if current is None:
        return None
    
    # If current is rootfs (p2), target is clonefs (p3)
    if current == 2:
        return 3
    
    # If current is clonefs (p3), target is rootfs (p2)
    if current == 3:
        return 2
    
    return None


def read_cmdline_file() -> Optional[str]:
    """
    Read cmdline.txt file.
    
    Returns:
        Contents of cmdline.txt as string, or None if file doesn't exist
    """
    if not CMDLINE_FILE.exists():
        return None
    
    try:
        with open(CMDLINE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def modify_cmdline_root(cmdline: str, target_device: str) -> str:
    """
    Modify cmdline string to use target root device.

    Args:
        cmdline: Original cmdline string
        target_device: Target root device (e.g., '/dev/mmcblk0p3')

    Returns:
        Modified cmdline string with the root parameter replaced. Unchanged if
        there is no root= parameter.
    """
    return re.sub(r"(?<!\S)root=\S+", lambda _: f"root={target_device}", cmdline)


def copy_and_modify_cmdline(target_device: str, target_partition: int, target_label: str) -> bool:
    """
    Copy cmdline.txt to tryline.txt and modify partition.
    
    Args:
        target_device: Target root device (e.g., '/dev/mmcblk0p3')
        target_partition: Target partition number (2 or 3)
        target_label: Target partition label (e.g., "clonefs" or "rootfs")
        
    Returns:
        True if successful, False otherwise
    """
    cmdline_content = read_cmdline_file()
    if cmdline_content is None:
        logger.error("Could not read %s", CMDLINE_FILE)
        return False
    
    # Modify the cmdline to use target partition
    modified_cmdline = modify_cmdline_root(cmdline_content, target_device)
    logger.debug("Modified cmdline for partition %s: %s", target_partition, modified_cmdline)
    if f"root={target_device}" not in modified_cmdline.split():
        logger.error("No root= parameter in %s; cannot point tryboot at %s", CMDLINE_FILE, target_device)
        return False
    
    # Write to tryline.txt
    try:
        write_file_atomic(TRYLINE_FILE, modified_cmdline + "\n")
        logger.info("Writing tryline.txt to boot partition %s (p%s)", target_label, target_partition)
        return True
    except (OSError, IOError) as e:
        logger.error("Could not write %s: %s", TRYLINE_FILE, e)
        return False


def copy_config_to_tryboot() -> bool:
    """
    Copy config.txt to tryboot.txt.
    
    Returns:
        True if successful, False otherwise
    """
    if not CONFIG_FILE.exists():
        logger.error("%s does not exist", CONFIG_FILE)
        return False
    
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config_content = f.read()
        
        write_file_atomic(TRYBOOT_CONFIG_FILE, config_content)
        logger.debug("Copied %s to %s", CONFIG_FILE, TRYBOOT_CONFIG_FILE)
        return True
    except (OSError, IOError) as e:
        logger.error("Could not copy %s to %s: %s", CONFIG_FILE, TRYBOOT_CONFIG_FILE, e)
        return False


def add_cmdline_entry() -> bool:
    """
    Add cmdline=tryline.txt entry to tryboot.txt.
    
    Returns:
        True if successful, False otherwise
    """
    if not TRYBOOT_CONFIG_FILE.exists():
        logger.error("%s does not exist", TRYBOOT_CONFIG_FILE)
        return False
    
    try:
        # Read existing content
        with open(TRYBOOT_CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Append cmdline entry if not already present
        entry = "cmdline=tryline.txt"
        if entry not in content:
            # Ensure content ends with newline before appending
            if content and not content.endswith("\n"):
                content += "\n"
            content += entry + "\n"
            
            write_file_atomic(TRYBOOT_CONFIG_FILE, content)
            logger.debug("Added cmdline entry to %s", TRYBOOT_CONFIG_FILE)
        else:
            logger.debug("Cmdline entry already present in %s", TRYBOOT_CONFIG_FILE)
        
        return True
    except (OSError, IOError) as e:
        logger.error("Could not add cmdline entry to %s: %s", TRYBOOT_CONFIG_FILE, e)
        return False


def read_tryline_file() -> Optional[str]:
    """
    Read tryline.txt file.
    
    Returns:
        Contents of tryline.txt as string, or None if file doesn't exist
    """
    if not TRYLINE_FILE.exists():
        return None
    
    try:
        with open(TRYLINE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def is_tryboot_persisted() -> bool:
    """
    Check if tryboot configuration is persisted by comparing tryline.txt and cmdline.txt.
    
    Returns:
        True if tryboot is persisted (files match), False otherwise
    """
    if not TRYLINE_FILE.exists() or not CMDLINE_FILE.exists():
        return False
    
    try:
        with open(TRYLINE_FILE, "r", encoding="utf-8") as f:
            tryline_content = f.read().strip()
        
        with open(CMDLINE_FILE, "r", encoding="utf-8") as f:
            cmdline_content = f.read().strip()
        
        return tryline_content == cmdline_content
    except (OSError, IOError):
        return False


def persist_boot_configuration() -> bool:
    """
    Persist the current boot configuration by copying tryline.txt to cmdline.txt.
    
    Returns:
        True if successful, False otherwise
    """
    tryline_content = read_tryline_file()
    if tryline_content is None:
        logger.error("%s does not exist", TRYLINE_FILE)
        return False
    
    try:
        write_file_atomic(CMDLINE_FILE, tryline_content + "\n", preserve_times=True)
        logger.debug("Copied %s to %s", TRYLINE_FILE, CMDLINE_FILE)
        return True
    except (OSError, IOError) as e:
        logger.error("Could not write %s: %s", CMDLINE_FILE, e)
        return False


def execute_persist(reboot: bool = False) -> int:
    """
    Execute the persist process to save current boot configuration.
    
    Args:
        reboot: If True, automatically reboot the system after persisting
        
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check if tryboot is active
    if is_regular_boot():
        logger.error("System is not booted via tryboot; persist can only be used when booted via tryboot")
        return 1
    
    # Get current partition info
    booted = get_booted_partition()
    if not booted:
        logger.error("Could not determine current boot partition")
        return 1
    
    _, partition_num, label = booted
    
    logger.info("Current partition: %s (p%s)", label, partition_num)
    
    # Persist configuration
    if not persist_boot_configuration():
        return 1
    
    logger.info("Boot configuration persisted successfully by writing cmdline.txt from tryline.txt")
    
    if reboot:
        logger.info("Rebooting system")
        try:
            subprocess.run(["reboot"], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error("Could not reboot system, please reboot manually: %s", e)
            return 1
    else:
        logger.info("Reboot to complete the persistence process")
    
    return 0


def rollback_tryboot(reboot: bool = True) -> int:
    """
    Rollback from tryboot to the previous partition.
    
    This command restores the boot configuration to the previous partition
    and removes tryboot configuration files. Used when booted via tryboot
    but something went wrong and you want to go back.
    
    Args:
        reboot: If True, automatically reboot the system after rollback (default: True)
        
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check if tryboot is active
    if is_regular_boot():
        logger.error("System is not booted via tryboot. Rollback can only be used when booted via tryboot")
        return 1
    
    # Get current partition (the one we're trying to rollback from)
    booted = get_booted_partition()
    if not booted:
        logger.error("Could not determine current boot partition")
        return 1
    
    _, current_partition_num, current_label = booted
    
    # Get the previous partition (the one we want to go back to)
    previous = get_inactive_partition()
    if not previous:
        logger.error("Could not determine previous partition")
        return 1
    
    previous_device, previous_partition_num, previous_label = previous
    
    logger.info("Current partition (tryboot): %s (p%s)", current_label, current_partition_num)
    logger.info("Rolling back to: %s (p%s)", previous_label, previous_partition_num)
    
    # Read current cmdline.txt to get base content
    cmdline_content = read_cmdline_file()
    if cmdline_content is None:
        logger.error("Could not read %s", CMDLINE_FILE)
        return 1
    
    # Modify cmdline to point to previous partition
    modified_cmdline = modify_cmdline_root(cmdline_content, previous_device)
    if f"root={previous_device}" not in modified_cmdline.split():
        logger.error("No root= parameter in %s; cannot point it at %s", CMDLINE_FILE, previous_device)
        return 1
    
    # Write restored cmdline.txt
    try:
        write_file_atomic(CMDLINE_FILE, modified_cmdline + "\n", preserve_times=True)
        logger.info("Restored %s to point to %s (p%s)", CMDLINE_FILE, previous_label, previous_partition_num)
    except (OSError, IOError) as e:
        logger.error("Could not write %s: %s", CMDLINE_FILE, e)
        return 1
    
    # Remove tryboot configuration files
    removed_files = []
    
    if TRYBOOT_CONFIG_FILE.exists():
        try:
            TRYBOOT_CONFIG_FILE.unlink()
            removed_files.append(TRYBOOT_CONFIG_FILE.name)
            logger.debug("Removed %s", TRYBOOT_CONFIG_FILE)
        except OSError as e:
            logger.warning("Could not remove %s: %s", TRYBOOT_CONFIG_FILE, e)
    
    if TRYLINE_FILE.exists():
        try:
            TRYLINE_FILE.unlink()
            removed_files.append(TRYLINE_FILE.name)
            logger.debug("Removed %s", TRYLINE_FILE)
        except OSError as e:
            logger.warning("Could not remove %s: %s", TRYLINE_FILE, e)
    
    if removed_files:
        logger.info("Removed tryboot configuration files: %s", ', '.join(removed_files))
    
    logger.info("Rollback complete")
    
    if reboot:
        logger.info("Rebooting system to complete rollback")
        try:
            subprocess.run(["reboot"], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error("Could not reboot system, please reboot manually: %s", e)
            return 1
    else:
        logger.info("Reboot to complete the rollback process")
    
    return 0


def execute_tryboot(reboot: bool = True) -> int:
    """
    Execute the tryboot process.
    
    Args:
        reboot: If True, automatically reboot the system after configuration (default: True)
        
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check if already trybooted
    if not is_regular_boot():
        # Allow another tryboot if config has been persisted
        if is_tryboot_persisted():
            logger.info("System is booted via tryboot, but config is persisted; allowing another tryboot")
        else:
            logger.error(
                "System is already booted via tryboot; the config needs to be persisted before doing another tryboot"
            )
            return 1
    
    # Get current and target partitions
    current_partition = get_current_root_partition()
    if current_partition is None:
        logger.error("Could not determine current boot partition")
        return 1
    
    target_partition = get_target_partition()
    target = get_inactive_partition()
    if target_partition is None or target is None:
        logger.error("Could not determine target partition")
        return 1
    target_device = target[0]
    
    # Get partition labels for user feedback
    booted = get_booted_partition()
    current_label = booted[2] if booted else f"p{current_partition}"
    target_label = "clonefs" if target_partition == 3 else "rootfs"
    
    logger.info("Current partition: %s (p%s)", current_label, current_partition)
    logger.info("Target partition: %s (p%s)", target_label, target_partition)
    
    # Copy and modify cmdline
    if not copy_and_modify_cmdline(target_device, target_partition, target_label):
        return 1
    
    # Copy config
    if not copy_config_to_tryboot():
        return 1
    
    # Add cmdline entry
    if not add_cmdline_entry():
        return 1
    
    logger.info("Tryboot configuration complete, tryboot.txt written with cmdline reference")
    
    if reboot:
        logger.info("Rebooting system to activate tryboot")
        try:
            subprocess.run(["reboot", "0 tryboot"], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error("Could not reboot system, please reboot manually using reboot \"0 tryboot\": %s", e)
            return 1
    else:
        logger.info("To activate tryboot, reboot using: sudo reboot \"0 tryboot\"")
    
    return 0


