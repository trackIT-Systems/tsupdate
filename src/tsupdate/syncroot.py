"""Syncroot tool for syncing root partition to inactive partition."""

import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from tsupdate.status import get_inactive_partition
from tsupdate.tryboot import is_regular_boot, is_tryboot_persisted

logger = logging.getLogger(__name__)


# Mount points
ROOT_RO = Path("/media/root-ro")
ROOT_UP = Path("/media/root-up")


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


@contextmanager
def mount_context(device: str, mount_point: Path):
    """
    Context manager for mounting and automatically unmounting a partition.
    
    Args:
        device: Device path to mount (e.g., '/dev/mmcblk0p2')
        mount_point: Path where to mount the partition
        
    Yields:
        mount_point: The path where the partition is mounted
        
    Raises:
        RuntimeError: If mounting fails
    """
    if not mount_partition(device, mount_point):
        raise RuntimeError(f"Failed to mount {device} to {mount_point}")
    try:
        yield mount_point
    finally:
        if not unmount_partition(mount_point):
            logger.warning(f"Could not unmount {mount_point}")


def sync_root_partitions(source: Optional[Path] = None, destination: Optional[Path] = None) -> bool:
    """
    Sync root partitions using rsync.
    
    Syncs source to destination using rsync with archive mode
    and deletion of extra files.
    
    Args:
        source: Source path to sync from (defaults to ROOT_RO)
        destination: Destination path to sync to (defaults to ROOT_UP)
    
    Returns:
        True if successful, False otherwise
    """
    if source is None:
        source = ROOT_RO
    if destination is None:
        destination = ROOT_UP
    
    source_str = str(source) + "/"
    destination_str = str(destination) + "/"
    
    logger.debug(f"Syncing from {source_str} to {destination_str}")
    
    # Build rsync command
    rsync_cmd = ["rsync", "-a", "-h", "--stats", "--delete"]
    
    # Add --verbose if debug logging is enabled
    if logger.isEnabledFor(logging.DEBUG):
        rsync_cmd.append("--itemize-changes")
    
    rsync_cmd.extend([source_str, destination_str])
    
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
    
    # Mount inactive partition and sync
    try:
        with mount_context(device, ROOT_UP) as mount_point:
            logger.info(f"Mounted {device} to {mount_point}")
            
            logger.info(f"Syncing {ROOT_RO} to {mount_point}...")
            if not sync_root_partitions():
                return 1
            
            logger.info("Sync completed successfully")
            return 0
    except RuntimeError as e:
        logger.error(str(e))
        return 1


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

