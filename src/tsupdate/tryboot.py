"""Tryboot tool for switching boot partitions."""

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from tsupdate.status import is_tryboot_active, get_booted_partition


# Boot files directory
BOOT_DIR = Path("/boot/firmware")
CMDLINE_FILE = BOOT_DIR / "cmdline.txt"
TRYLINE_FILE = BOOT_DIR / "tryline.txt"
CONFIG_FILE = BOOT_DIR / "config.txt"
TRYBOOT_CONFIG_FILE = BOOT_DIR / "tryboot.txt"


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


def modify_cmdline_partition(cmdline: str, target_partition: int) -> str:
    """
    Modify cmdline string to use target partition.
    
    Args:
        cmdline: Original cmdline string
        target_partition: Target partition number (2 or 3)
        
    Returns:
        Modified cmdline string with root parameter updated
    """
    target_device = f"/dev/mmcblk0p{target_partition}"
    
    # Replace root= parameter
    # Pattern matches: root=/dev/mmcblk0p2, root=mmcblk0p2, root=/dev/mmcblk0p3, etc.
    pattern = r"root=(/dev/)?mmcblk0p[23]"
    replacement = f"root={target_device}"
    
    modified = re.sub(pattern, replacement, cmdline)
    return modified


def copy_and_modify_cmdline(target_partition: int, target_label: str) -> bool:
    """
    Copy cmdline.txt to tryline.txt and modify partition.
    
    Args:
        target_partition: Target partition number (2 or 3)
        target_label: Target partition label (e.g., "clonefs" or "rootfs")
        
    Returns:
        True if successful, False otherwise
    """
    cmdline_content = read_cmdline_file()
    if cmdline_content is None:
        print(f"Error: Could not read {CMDLINE_FILE}", file=sys.stderr)
        return False
    
    # Modify the cmdline to use target partition
    modified_cmdline = modify_cmdline_partition(cmdline_content, target_partition)
    
    # Write to tryline.txt
    try:
        with open(TRYLINE_FILE, "w", encoding="utf-8") as f:
            f.write(modified_cmdline)
            f.write("\n")
        print(f"Writing tryline.txt to boot partition {target_label} (p{target_partition})")
        return True
    except (OSError, IOError) as e:
        print(f"Error: Could not write {TRYLINE_FILE}: {e}", file=sys.stderr)
        return False


def copy_config_to_tryboot() -> bool:
    """
    Copy config.txt to tryboot.txt.
    
    Returns:
        True if successful, False otherwise
    """
    if not CONFIG_FILE.exists():
        print(f"Error: {CONFIG_FILE} does not exist", file=sys.stderr)
        return False
    
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config_content = f.read()
        
        with open(TRYBOOT_CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(config_content)
        return True
    except (OSError, IOError) as e:
        print(f"Error: Could not copy {CONFIG_FILE} to {TRYBOOT_CONFIG_FILE}: {e}", file=sys.stderr)
        return False


def add_cmdline_entry() -> bool:
    """
    Add cmdline=tryline.txt entry to tryboot.txt.
    
    Returns:
        True if successful, False otherwise
    """
    if not TRYBOOT_CONFIG_FILE.exists():
        print(f"Error: {TRYBOOT_CONFIG_FILE} does not exist", file=sys.stderr)
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
            
            # Write back
            with open(TRYBOOT_CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(content)
        
        return True
    except (OSError, IOError) as e:
        print(f"Error: Could not add cmdline entry to {TRYBOOT_CONFIG_FILE}: {e}", file=sys.stderr)
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


def persist_boot_configuration() -> bool:
    """
    Persist the current boot configuration by copying tryline.txt to cmdline.txt.
    
    Returns:
        True if successful, False otherwise
    """
    tryline_content = read_tryline_file()
    if tryline_content is None:
        print(f"Error: {TRYLINE_FILE} does not exist", file=sys.stderr)
        return False
    
    try:
        with open(CMDLINE_FILE, "w", encoding="utf-8") as f:
            f.write(tryline_content)
            f.write("\n")
        return True
    except (OSError, IOError) as e:
        print(f"Error: Could not write {CMDLINE_FILE}: {e}", file=sys.stderr)
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
        print(
            "Error: System is not booted via tryboot.\n"
            "Persist can only be used when booted via tryboot.",
            file=sys.stderr
        )
        return 1
    
    # Get current partition info
    booted = get_booted_partition()
    if not booted:
        print("Error: Could not determine current boot partition", file=sys.stderr)
        return 1
    
    _, partition_num, label = booted
    
    print(f"Current partition: {label} (p{partition_num})")
    
    # Persist configuration
    if not persist_boot_configuration():
        return 1
    
    print(f"Writing cmdline.txt from tryline.txt to persist boot configuration")
    print("\nBoot configuration persisted successfully.")
    
    if reboot:
        print("Rebooting system...")
        try:
            subprocess.run(["reboot"], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"Error: Could not reboot system: {e}", file=sys.stderr)
            print("Please reboot manually", file=sys.stderr)
            return 1
    else:
        print("Reboot to complete the persistence process.")
    
    return 0


def execute_tryboot(reboot: bool = False) -> int:
    """
    Execute the tryboot process.
    
    Args:
        reboot: If True, automatically reboot the system after configuration
        
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check if already trybooted
    if not is_regular_boot():
        print(
            "Error: System is already booted via tryboot.\n"
            "The config needs to be persisted before doing another tryboot.",
            file=sys.stderr
        )
        return 1
    
    # Get current and target partitions
    current_partition = get_current_root_partition()
    if current_partition is None:
        print("Error: Could not determine current boot partition", file=sys.stderr)
        return 1
    
    target_partition = get_target_partition()
    if target_partition is None:
        print("Error: Could not determine target partition", file=sys.stderr)
        return 1
    
    # Get partition labels for user feedback
    booted = get_booted_partition()
    current_label = booted[2] if booted else f"p{current_partition}"
    target_label = "clonefs" if target_partition == 3 else "rootfs"
    
    print(f"Current partition: {current_label} (p{current_partition})")
    print(f"Target partition: {target_label} (p{target_partition})")
    
    # Copy and modify cmdline
    if not copy_and_modify_cmdline(target_partition, target_label):
        return 1
    
    # Copy config
    if not copy_config_to_tryboot():
        return 1
    
    # Add cmdline entry
    if not add_cmdline_entry():
        return 1
    
    print("Writing tryboot.txt configuration with cmdline reference")
    print("\nTryboot configuration complete.")
    
    if reboot:
        print("Rebooting system to activate tryboot...")
        try:
            subprocess.run(["reboot", "0 tryboot"], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"Error: Could not reboot system: {e}", file=sys.stderr)
            print("Please reboot manually using: reboot \"0 tryboot\"", file=sys.stderr)
            return 1
    else:
        print("To activate tryboot, reboot using: sudo reboot \"0 tryboot\"")
    
    return 0


