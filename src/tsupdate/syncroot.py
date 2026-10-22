"""Syncroot tool for syncing root partition to inactive partition."""

import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from tsupdate.errors import UpdateError, timeout_error
from tsupdate.status import get_inactive_partition
from tsupdate.tryboot import is_regular_boot, is_tryboot_persisted
from tsupdate.utils import resolve_rsync_timeout_seconds

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


def mount_partition(device: str, mount_point: Path) -> None:
    """
    Mount partition to specified mount point.
    
    Args:
        device: Device path to mount (e.g., '/dev/mmcblk0p2')
        mount_point: Path where to mount the partition
        
    Raises:
        UpdateError: The mount point cannot be created or mount failed.
    """
    # Create mount point directory if it doesn't exist
    try:
        mount_point.mkdir(parents=True, exist_ok=True)
        logger.debug("Created mount point directory: %s", mount_point)
    except OSError as e:
        raise UpdateError(f"Could not create mount point {mount_point}: {e}") from e
    
    # Mount the partition
    try:
        subprocess.run(
            ["mount", device, str(mount_point)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise UpdateError(f"Could not mount {device} to {mount_point}: {(e.stderr or '').strip()}") from e
    except FileNotFoundError as e:
        raise UpdateError("Command not found: mount") from e
    logger.debug("Mounted %s to %s", device, mount_point)


def unmount_partition(mount_point: Path) -> None:
    """
    Unmount partition from mount point.
    
    Args:
        mount_point: Path where partition is mounted
        
    Raises:
        UpdateError: umount failed.
    """
    try:
        subprocess.run(
            ["umount", str(mount_point)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise UpdateError(f"Could not unmount {mount_point}: {(e.stderr or '').strip()}") from e
    except FileNotFoundError as e:
        raise UpdateError("Command not found: umount") from e
    logger.debug("Unmounted %s", mount_point)


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
        UpdateError: If mounting fails. A failure to unmount is logged as a warning.
    """
    mount_partition(device, mount_point)
    try:
        yield mount_point
    finally:
        try:
            unmount_partition(mount_point)
        except UpdateError as e:
            logger.warning("%s", e)


def sync_root_partitions(
    source: Optional[Path] = None,
    destination: Optional[Path] = None,
    rsync_timeout_sec: Optional[int] = None,
) -> None:
    """
    Sync root partitions using rsync.
    
    Syncs source to destination using rsync with archive mode,
    content checksum comparison, and deletion of extra files.
    
    Args:
        source: Source path to sync from (defaults to ROOT_RO)
        destination: Destination path to sync to (defaults to ROOT_UP)
        rsync_timeout_sec: Wall-clock limit in seconds (None = default 3600; 0 = unlimited)
    
    Raises:
        TransientError: rsync ran into its timeout.
        UpdateError: rsync failed or is not installed.
    """
    if source is None:
        source = ROOT_RO
    if destination is None:
        destination = ROOT_UP
    
    source_str = str(source) + "/"
    destination_str = str(destination) + "/"
    
    logger.debug("Syncing from %s to %s", source_str, destination_str)
    
    # Build rsync command (--checksum: compare by content, not only size+mtime)
    rsync_cmd = ["rsync", "-a", "-h", "--stats", "--delete", "--checksum"]
    
    # Add --verbose if debug logging is enabled
    if logger.isEnabledFor(logging.DEBUG):
        rsync_cmd.append("--itemize-changes")
    
    rsync_cmd.extend([source_str, destination_str])
    
    timeout = resolve_rsync_timeout_seconds(rsync_timeout_sec)
    try:
        subprocess.run(
            rsync_cmd,
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise timeout_error(
            e, "Rsync", "increase rsync_timeout in tsupdate.yml or pass --rsync-timeout (0 = no limit)"
        ) from e
    except subprocess.CalledProcessError as e:
        raise UpdateError(f"Rsync failed: {e}") from e
    except FileNotFoundError as e:
        raise UpdateError("Command not found: rsync") from e
    logger.debug("Rsync completed successfully")


def _inactive_device() -> str:
    """
    The inactive partition's device, logging which partition that is.

    Raises:
        UpdateError: The inactive partition cannot be determined.
    """
    inactive = get_inactive_partition()
    if not inactive:
        raise UpdateError("Could not determine inactive partition")
    device, partition_num, label = inactive
    logger.info("Inactive partition: %s (p%s)", label, partition_num)
    return device


def execute_syncroot(rsync_timeout_sec: Optional[int] = None) -> int:
    """
    Execute the syncroot process.
    
    Args:
        rsync_timeout_sec: Wall-clock limit for rsync in seconds (None = default 3600; 0 = unlimited)
    
    Returns:
        0 (success)

    Raises:
        UpdateError: The partition cannot be synced now, or mounting or rsync failed.
    """
    if not can_run_syncroot():
        raise UpdateError("The syncroot command can only be run when booted regularly or when tryboot is persisted")
    
    device = _inactive_device()
    
    with mount_context(device, ROOT_UP) as mount_point:
        logger.info("Mounted %s to %s", device, mount_point)
        logger.info("Syncing %s to %s", ROOT_RO, mount_point)
        sync_root_partitions(rsync_timeout_sec=rsync_timeout_sec)
    
    logger.info("Sync completed successfully")
    return 0


def execute_mount() -> int:
    """
    Mount the inactive partition to /media/root-up.
    
    Returns:
        0 (success)

    Raises:
        UpdateError: The partition is already mounted, or mounting failed.
    """
    device = _inactive_device()
    
    # Check if already mounted
    try:
        result = subprocess.run(
            ["mountpoint", "-q", str(ROOT_UP)],
            capture_output=True,
        )
        if result.returncode == 0:
            raise UpdateError(f"{ROOT_UP} is already mounted")
    except FileNotFoundError:
        # mountpoint command not available, continue
        logger.debug("mountpoint command not available, skipping mount check")
    
    mount_partition(device, ROOT_UP)
    logger.info("Mounted %s to %s", device, ROOT_UP)
    return 0


def execute_unmount() -> int:
    """
    Unmount the inactive partition from /media/root-up.
    
    Returns:
        0 (success)

    Raises:
        UpdateError: The partition is not mounted, or unmounting failed.
    """
    # Check if mounted
    try:
        result = subprocess.run(
            ["mountpoint", "-q", str(ROOT_UP)],
            capture_output=True,
        )
        if result.returncode != 0:
            raise UpdateError(f"{ROOT_UP} is not mounted")
    except FileNotFoundError:
        # mountpoint command not available, continue
        logger.debug("mountpoint command not available, skipping mount check")
    
    unmount_partition(ROOT_UP)
    logger.info("Unmounted %s", ROOT_UP)
    return 0
