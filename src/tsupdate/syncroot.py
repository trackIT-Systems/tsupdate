"""Syncroot tool for syncing root partition to inactive partition."""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

from tsupdate.status import get_inactive_partition
from tsupdate.tryboot import is_regular_boot, BOOT_DIR, CMDLINE_FILE, TRYLINE_FILE

logger = logging.getLogger(__name__)


# Mount points
ROOT_RO = Path("/media/root-ro")
ROOT_UP = Path("/media/root-up")


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


def can_run_syncroot() -> bool:
    """
    Check if syncroot can be run.
    
    Returns:
        True if system is booted regularly OR tryboot is persisted
    """
    return is_regular_boot() or is_tryboot_persisted()


def get_inactive_partition_device() -> Optional[str]:
    """
    Get inactive partition device path.
    
    Returns:
        Device path (e.g., '/dev/mmcblk0p2') or None
    """
    inactive = get_inactive_partition()
    if not inactive:
        return None
    
    device, _, _ = inactive
    return device


def mount_partition(device: str, mount_point: Path) -> bool:
    """
    Mount partition to specified mount point.
    
    Args:
        device: Device path to mount (e.g., '/dev/mmcblk0p2')
        mount_point: Path where to mount the partition
        
    Returns:
        True if successful, False otherwise
    """
    # Create mount point directory if it doesn't exist
    try:
        mount_point.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Created mount point directory: {mount_point}")
    except OSError as e:
        logger.error(f"Could not create mount point {mount_point}: {e}")
        return False
    
    # Mount the partition
    try:
        result = subprocess.run(
            ["mount", device, str(mount_point)],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.debug(f"Mounted {device} to {mount_point}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Could not mount {device} to {mount_point}: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error("mount command not found")
        return False


def unmount_partition(mount_point: Path) -> bool:
    """
    Unmount partition from mount point.
    
    Args:
        mount_point: Path where partition is mounted
        
    Returns:
        True if successful, False otherwise
    """
    try:
        result = subprocess.run(
            ["umount", str(mount_point)],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.debug(f"Unmounted {mount_point}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Could not unmount {mount_point}: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error("umount command not found")
        return False


def sync_root_partitions() -> bool:
    """
    Sync root partitions using rsync.
    
    Syncs /media/root-ro to /media/root-up using rsync with archive mode
    and deletion of extra files.
    
    Returns:
        True if successful, False otherwise
    """
    source = str(ROOT_RO) + "/"
    destination = str(ROOT_UP) + "/"
    
    logger.debug(f"Syncing from {source} to {destination}")
    
    # Build rsync command
    rsync_cmd = ["rsync", "-a", "-h", "--stats", "--delete"]
    
    # Add --verbose if debug logging is enabled
    if logger.isEnabledFor(logging.DEBUG):
        rsync_cmd.append("--itemize-changes")
    
    rsync_cmd.extend([source, destination])
    
    try:
        result = subprocess.run(
            rsync_cmd,
            check=True,
        )
        logger.debug("Rsync completed successfully")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"rsync failed: {e}")
        return False
    except FileNotFoundError:
        logger.error("rsync command not found")
        return False


def execute_syncroot() -> int:
    """
    Execute the syncroot process.
    
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check if syncroot can be run
    if not can_run_syncroot():
        logger.error("syncroot can only be run when booted regularly or when tryboot is persisted.")
        return 1
    
    # Get inactive partition device
    device = get_inactive_partition_device()
    if not device:
        logger.error("Could not determine inactive partition")
        return 1
    
    # Get partition info for user feedback
    inactive = get_inactive_partition()
    if inactive:
        _, partition_num, label = inactive
        logger.info(f"Inactive partition: {label} (p{partition_num})")
    
    # Mount inactive partition
    if not mount_partition(device, ROOT_UP):
        return 1
    
    logger.info(f"Mounted {device} to {ROOT_UP}")
    
    # Sync partitions
    sync_success = False
    try:
        logger.info(f"Syncing {ROOT_RO} to {ROOT_UP}...")
        if not sync_root_partitions():
            sync_success = False
        else:
            logger.info("Sync completed successfully")
            sync_success = True
    finally:
        # Always unmount after syncing
        unmount_success = unmount_partition(ROOT_UP)
        if not unmount_success:
            logger.warning(f"Could not unmount {ROOT_UP}")
    
    return 0 if sync_success else 1


def execute_mount() -> int:
    """
    Mount the inactive partition to /media/root-up.
    
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Get inactive partition device
    device = get_inactive_partition_device()
    if not device:
        logger.error("Could not determine inactive partition")
        return 1
    
    # Get partition info for user feedback
    inactive = get_inactive_partition()
    if inactive:
        _, partition_num, label = inactive
        logger.info(f"Inactive partition: {label} (p{partition_num})")
    
    # Check if already mounted
    try:
        result = subprocess.run(
            ["mountpoint", "-q", str(ROOT_UP)],
            capture_output=True,
        )
        if result.returncode == 0:
            logger.error(f"{ROOT_UP} is already mounted")
            return 1
    except FileNotFoundError:
        # mountpoint command not available, continue
        logger.debug("mountpoint command not available, skipping mount check")
        pass
    
    # Mount inactive partition
    if not mount_partition(device, ROOT_UP):
        return 1
    
    logger.info(f"Mounted {device} to {ROOT_UP}")
    return 0


def execute_unmount() -> int:
    """
    Unmount the inactive partition from /media/root-up.
    
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check if mounted
    try:
        result = subprocess.run(
            ["mountpoint", "-q", str(ROOT_UP)],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.error(f"{ROOT_UP} is not mounted")
            return 1
    except FileNotFoundError:
        # mountpoint command not available, continue
        logger.debug("mountpoint command not available, skipping mount check")
        pass
    
    # Unmount partition
    if not unmount_partition(ROOT_UP):
        return 1
    
    logger.info(f"Unmounted {ROOT_UP}")
    return 0

