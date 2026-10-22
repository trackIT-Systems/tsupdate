"""Restore tool for restoring OS image to inactive partition."""

import gzip
import logging
import lzma
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

from tsupdate.syncroot import (
    can_run_syncroot,
    get_inactive_partition_device,
    mount_context,
    ROOT_UP,
    sync_root_partitions,
)
from tsupdate.utils import ARTIFACTS_DIR, ensure_file, safe_cleanup

logger = logging.getLogger(__name__)


# Mount points and directories
IMAGE_MOUNT = Path("/media/image-rootfs")
LOOP_DEVICE_PREFIX = "/dev/loop"


def find_largest_img_in_zip(zip_path: Path) -> Optional[str]:
    """
    Find the largest .img file in a zip archive.
    
    Args:
        zip_path: Path to zip file
        
    Returns:
        Name of largest .img file, or None if not found
    """
    try:
        logger.debug("Scanning zip archive for .img files: %s", zip_path)
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            img_files = []
            for info in zip_file.infolist():
                filename = info.filename
                if filename.lower().endswith(".img"):
                    img_files.append((filename, info.file_size))
                    logger.debug("Found .img file: %s (%s bytes)", filename, info.file_size)
            
            if not img_files:
                logger.error("No .img files found in zip archive")
                return None
            
            # Find largest .img file
            largest_img = max(img_files, key=lambda x: x[1])
            img_name, img_size = largest_img
            logger.info("Using largest .img file: %s (%s bytes)", img_name, img_size)
            return img_name
    except zipfile.BadZipFile as e:
        logger.error("Invalid zip file: %s", e)
        return None
    except Exception as e:
        logger.error("Failed to scan zip archive: %s", e)
        return None


def extract_image(compressed_path: Path, output_path: Path) -> bool:
    """
    Extract compressed image file using streaming to avoid memory issues.
    
    Extracts to a temporary file first, then renames to the final path
    on success to avoid corrupt files if interrupted.
    
    Supports .gz, .xz, and .zip compression.
    For .zip files, extracts the largest .img file.
    
    Args:
        compressed_path: Path to compressed image file
        output_path: Path where to save extracted image
        
    Returns:
        True if successful, False otherwise
    """
    logger.info("Extracting image from %s", compressed_path)
    
    # Chunk size for streaming (8MB chunks)
    CHUNK_SIZE = 8 * 1024 * 1024
    
    # Use temporary file for extraction
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    
    try:
        if compressed_path.suffix == ".zip" or compressed_path.suffixes[-1] == ".zip":
            # Handle zip files
            logger.debug("Detected zip compression")
            
            # Find largest .img file in zip
            img_name = find_largest_img_in_zip(compressed_path)
            if not img_name:
                return False
            
            # Extract the largest .img file using streaming to temporary file
            with zipfile.ZipFile(compressed_path, "r") as zip_file:
                logger.debug("Extracting %s from zip archive (streaming)", img_name)
                with zip_file.open(img_name) as source:
                    with open(temp_path, "wb") as target:
                        while True:
                            chunk = source.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            target.write(chunk)
            
            # Rename temporary file to final path (atomic operation)
            temp_path.rename(output_path)
            logger.info("Extracted %s from zip to %s", img_name, output_path)
            return True
        elif compressed_path.suffix == ".gz" or compressed_path.suffixes[-1] == ".gz":
            logger.debug("Extracting gzip image (streaming)")
            with gzip.open(compressed_path, "rb") as f_in:
                with open(temp_path, "wb") as f_out:
                    while True:
                        chunk = f_in.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f_out.write(chunk)
            
            # Rename temporary file to final path (atomic operation)
            temp_path.rename(output_path)
            logger.info("Extracted gzip image to %s", output_path)
            return True
        elif compressed_path.suffix == ".xz" or compressed_path.suffixes[-1] == ".xz":
            logger.debug("Extracting xz image (streaming)")
            with lzma.open(compressed_path, "rb") as f_in:
                with open(temp_path, "wb") as f_out:
                    while True:
                        chunk = f_in.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f_out.write(chunk)
            
            # Rename temporary file to final path (atomic operation)
            temp_path.rename(output_path)
            logger.info("Extracted xz image to %s", output_path)
            return True
        else:
            logger.warning("Unknown compression format, assuming uncompressed")
            # Copy file as-is using shutil.copyfileobj for streaming to temporary file
            with open(compressed_path, "rb") as f_in:
                with open(temp_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out, length=CHUNK_SIZE)
            
            # Rename temporary file to final path (atomic operation)
            temp_path.rename(output_path)
            logger.info("Copied image to %s", output_path)
            return True
    except Exception as e:
        logger.error("Failed to extract image: %s", e)
        # Clean up temporary file on failure
        safe_cleanup(temp_path)
        return False


def setup_loopback(image_path: Path) -> Optional[str]:
    """
    Set up loopback device for image file.
    
    Args:
        image_path: Path to image file
        
    Returns:
        Loopback device path (e.g., '/dev/loop0') or None on error
    """
    try:
        logger.debug("Setting up loopback device for %s", image_path)
        # --partscan makes the kernel create the partition devices (/dev/loopNpM) itself;
        # relying on partprobe alone fails where it is not installed.
        result = subprocess.run(
            ["losetup", "--find", "--show", "--partscan", str(image_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        loop_device = result.stdout.strip()
        logger.info("Created loopback device: %s", loop_device)
        
        # Re-scan as a fallback for losetup versions that ignore --partscan
        try:
            subprocess.run(
                ["partprobe", loop_device],
                capture_output=True,
                text=True,
                check=False,  # Don't fail if partprobe is not available
            )
            logger.debug("Scanned partitions on %s", loop_device)
        except FileNotFoundError:
            logger.debug("partprobe not available, partitions should still be accessible")
        
        return loop_device
    except subprocess.CalledProcessError as e:
        logger.error("Failed to setup loopback device: %s", e.stderr)
        return None
    except FileNotFoundError:
        logger.error("Command not found: losetup")
        return None


def remove_loopback(loop_device: str) -> bool:
    """
    Remove loopback device.
    
    Args:
        loop_device: Loopback device path (e.g., '/dev/loop0')
        
    Returns:
        True if successful, False otherwise
    """
    try:
        logger.debug("Removing loopback device: %s", loop_device)
        subprocess.run(
            ["losetup", "--detach", loop_device],
            capture_output=True,
            text=True,
            check=True,
        )
        logger.debug("Removed loopback device: %s", loop_device)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to remove loopback device %s: %s", loop_device, e.stderr)
        return False
    except FileNotFoundError:
        logger.warning("Command not found: losetup")
        return False


def get_partition_device(loop_device: str, partition: int) -> str:
    """
    Get partition device path from loopback device.
    
    Args:
        loop_device: Loopback device path (e.g., '/dev/loop0')
        partition: Partition number (e.g., 2)
        
    Returns:
        Partition device path (e.g., '/dev/loop0p2')
    """
    # Check if partition device exists with p prefix
    partition_device = f"{loop_device}p{partition}"
    
    # Verify the partition device exists
    if Path(partition_device).exists():
        return partition_device
    
    # Fallback: try without p prefix (some systems use /dev/loop0p2, others /dev/loop0p2)
    # Actually, standard is /dev/loop0p2, so we'll use that
    return partition_device


def execute_restore(
    image_source: str,
    partition: int = 2,
    keep_image: bool = False,
) -> int:
    """
    Execute the restore process.
    
    Reads GH_TOKEN from environment variable for GitHub authentication.
    
    Args:
        image_source: URL or local file path to OS image
        partition: Partition number in image to use as rootfs (default: 2)
        keep_image: If True, keep downloaded image file after restore
        
    Returns:
        Exit code (0 for success, non-zero for failure)

    Raises:
        UpdateError: The image cannot be downloaded (see ensure_file()), or mounting or
            syncing failed.
    """
    # Check if restore can be run
    if not can_run_syncroot():
        logger.error("The restore command can only be run when booted regularly or when tryboot is persisted")
        return 1
    
    # Get inactive partition device
    device = get_inactive_partition_device()
    if not device:
        logger.error("Could not determine inactive partition")
        return 1
    
    # Get partition info for user feedback
    from tsupdate.status import get_inactive_partition
    inactive = get_inactive_partition()
    if inactive:
        _, partition_num, label = inactive
        logger.info("Inactive partition: %s (p%s)", label, partition_num)
    
    # Use artifacts directory for downloaded/extracted images
    artifacts_path = ARTIFACTS_DIR
    image_path = None
    loop_device = None
    image_mounted = False
    target_mounted = False
    is_local_file = False
    downloaded_path = None
    extracted_path = None
    success = False
    
    try:
        # Ensure file is available (download from URL if needed)
        downloaded_path, is_local_file = ensure_file(image_source, artifacts_path)
        
        # Check if image needs extraction
        image_path = downloaded_path
        needs_extraction = False
        if downloaded_path.suffix in (".gz", ".xz", ".zip"):
            needs_extraction = True
        elif downloaded_path.suffixes and downloaded_path.suffixes[-1] in (".gz", ".xz", ".zip"):
            needs_extraction = True
        
        if needs_extraction:
            # Determine extracted filename based on downloaded file name
            # Remove compression extensions (.gz, .xz, .zip)
            extracted_filename = downloaded_path.name
            for ext in [".zip", ".xz", ".gz"]:
                if extracted_filename.endswith(ext):
                    extracted_filename = extracted_filename[:-len(ext)]
                    break
            
            # For zip files, we extract a .img file, so append .img if not present
            if downloaded_path.suffix == ".zip" or downloaded_path.suffixes[-1] == ".zip":
                if not extracted_filename.endswith(".img"):
                    # Use the downloaded filename without .zip, but ensure .img extension
                    extracted_filename = extracted_filename.rsplit(".", 1)[0] + ".img"
            elif not extracted_filename.endswith(".img"):
                # For other compressed formats, ensure .img extension
                extracted_filename = extracted_filename.rsplit(".", 1)[0] + ".img"
            
            extracted_path = artifacts_path / extracted_filename
            
            # Always extract afresh and never reuse an earlier .img: /data is world-writable, so
            # an existing file there can't be trusted (the compressed download, in contrast, was
            # verified against the GitHub asset digest by ensure_file()). Re-extracting is cheap,
            # re-downloading is not, so only the former is repeated.
            safe_cleanup(extracted_path)
            safe_cleanup(extracted_path.with_suffix(extracted_path.suffix + ".tmp"))
            if not extract_image(downloaded_path, extracted_path):
                return 1
            image_path = extracted_path
            logger.debug("Using extracted image: %s", image_path)
        
        # Setup loopback device
        loop_device = setup_loopback(image_path)
        if not loop_device:
            return 1
        
        # Get partition device
        partition_device = get_partition_device(loop_device, partition)
        
        # Wait a moment for partition devices to become available
        max_attempts = 5
        for attempt in range(max_attempts):
            if Path(partition_device).exists():
                break
            if attempt < max_attempts - 1:
                logger.debug("Waiting for partition device %s to become available", partition_device)
                time.sleep(0.5)
        
        if not Path(partition_device).exists():
            logger.error(
                "Partition device %s does not exist; the image may not have the requested partition, "
                "or partition scanning failed",
                partition_device,
            )
            return 1
        
        logger.info("Using partition %s from image: %s", partition, partition_device)
        
        # Mount image partition and inactive partition, then sync (failures raise UpdateError)
        with mount_context(partition_device, IMAGE_MOUNT) as image_mount:
            logger.info("Mounted %s to %s", partition_device, image_mount)
            
            with mount_context(device, ROOT_UP) as target_mount:
                logger.info("Mounted %s to %s", device, target_mount)
                
                # Sync from image to inactive partition
                logger.info("Syncing %s to %s", image_mount, target_mount)
                sync_root_partitions(source=image_mount, destination=target_mount)
                logger.info("Sync completed successfully")
                success = True
                return 0
        
    finally:
        
        if loop_device:
            remove_loopback(loop_device)
        
        # The extracted image is never reused (see above), so it only takes up space.
        if extracted_path is not None:
            safe_cleanup(extracted_path)
            safe_cleanup(extracted_path.with_suffix(extracted_path.suffix + ".tmp"))
        
        # Keep the download after a failure so a retry doesn't download it again; a file the
        # user passed in is never deleted.
        if success and downloaded_path is not None and not is_local_file and not keep_image:
            logger.debug("Removing downloaded image %s", downloaded_path)
            safe_cleanup(downloaded_path)

