"""Status tracking and persistence."""

import json
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple


# ============================================================================
# OS Release Functions
# ============================================================================

class OSRelease:
    """Represents OS release information from /etc/os-release."""
    
    def __init__(self, data: Dict[str, str]):
        """Initialize with parsed os-release data."""
        self.data = data
    
    @property
    def name(self) -> Optional[str]:
        """OS name (NAME)."""
        return self.data.get("NAME")
    
    @property
    def pretty_name(self) -> Optional[str]:
        """Pretty OS name (PRETTY_NAME)."""
        return self.data.get("PRETTY_NAME")
    
    @property
    def version(self) -> Optional[str]:
        """OS version (VERSION)."""
        return self.data.get("VERSION")
    
    @property
    def version_id(self) -> Optional[str]:
        """OS version ID (VERSION_ID)."""
        return self.data.get("VERSION_ID")
    
    @property
    def version_commit(self) -> Optional[str]:
        """Version commit hash (VERSION_COMMIT)."""
        return self.data.get("VERSION_COMMIT")
    
    @property
    def id(self) -> Optional[str]:
        """OS ID (ID)."""
        return self.data.get("ID")
    
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a value by key."""
        return self.data.get(key, default)
    
    def __repr__(self) -> str:
        """String representation."""
        return f"OSRelease(name={self.name}, version_id={self.version_id})"


def read_os_release(path: Path) -> Optional[OSRelease]:
    """
    Read and parse an /etc/os-release file.
    
    Args:
        path: Path to the os-release file
        
    Returns:
        OSRelease object if file exists and is readable, None otherwise
    """
    if not path.exists():
        return None
    
    try:
        data = {}
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                
                # Parse KEY="VALUE" or KEY=VALUE format
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    
                    data[key] = value
        
        return OSRelease(data)
    except (OSError, IOError, UnicodeDecodeError):
        return None


def read_booted_os_release() -> Optional[OSRelease]:
    """
    Read /etc/os-release from the currently booted system.
    
    Returns:
        OSRelease object for the booted system
    """
    return read_os_release(Path("/etc/os-release"))


# ============================================================================
# Partition Detection Functions
# ============================================================================

def parse_cmdline() -> dict[str, str]:
    """
    Parse /proc/cmdline and return as dictionary.
    
    Returns:
        Dictionary of kernel command line parameters
    """
    cmdline_path = Path("/proc/cmdline")
    if not cmdline_path.exists():
        return {}
    
    try:
        with open(cmdline_path, "r") as f:
            cmdline = f.read().strip()
        
        params = {}
        for item in cmdline.split():
            if "=" in item:
                key, value = item.split("=", 1)
                params[key] = value
            else:
                params[item] = True
        
        return params
    except (OSError, IOError):
        return {}


def get_root_partition_from_cmdline() -> Optional[str]:
    """
    Get root partition device from /proc/cmdline.
    
    Returns:
        Root partition device (e.g., '/dev/mmcblk0p2') or None
    """
    cmdline = parse_cmdline()
    root = cmdline.get("root")
    
    if root:
        # Normalize to full path if it's a relative device name
        if not root.startswith("/"):
            root = f"/dev/{root}"
        return root
    
    return None


def get_partition_number(device: str) -> Optional[int]:
    """
    Extract partition number from device path.
    
    Args:
        device: Device path (e.g., '/dev/mmcblk0p2')
        
    Returns:
        Partition number (e.g., 2) or None
    """
    if not device:
        return None
    
    # Match patterns like mmcblk0p2, sda1, etc.
    match = re.search(r"p(\d+)$", device)
    if match:
        return int(match.group(1))
    
    # Try alternative pattern for devices like sda1
    match = re.search(r"(\d+)$", device)
    if match:
        return int(match.group(1))
    
    return None


def get_partition_label(device: str) -> Optional[str]:
    """
    Get partition label using lsblk or blkid.
    
    Args:
        device: Device path (e.g., '/dev/mmcblk0p2')
        
    Returns:
        Partition label (e.g., 'rootfs', 'clonefs') or None
    """
    if not device:
        return None
    
    # Try lsblk first (more reliable)
    try:
        result = subprocess.run(
            ["lsblk", "-n", "-o", "LABEL", device],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            label = result.stdout.strip()
            if label:
                return label
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    
    # Fallback to blkid
    try:
        result = subprocess.run(
            ["blkid", "-s", "LABEL", "-o", "value", device],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            label = result.stdout.strip()
            if label:
                return label
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    
    return None


def get_booted_partition() -> Optional[Tuple[str, int, str]]:
    """
    Detect which partition is currently booted.
    
    Returns:
        Tuple of (device_path, partition_number, label) or None
        Example: ('/dev/mmcblk0p2', 2, 'rootfs')
    """
    device = get_root_partition_from_cmdline()
    if not device:
        return None
    
    partition_num = get_partition_number(device)
    label = get_partition_label(device)
    
    return (device, partition_num, label)


def get_inactive_partition() -> Optional[Tuple[str, int, str]]:
    """
    Get the inactive (non-booted) partition.
    
    Returns:
        Tuple of (device_path, partition_number, label) or None
    """
    booted = get_booted_partition()
    if not booted:
        return None
    
    _, booted_num, _ = booted
    
    # If rootfs (p2) is booted, inactive is clonefs (p3)
    if booted_num == 2:
        device = "/dev/mmcblk0p3"
        partition_num = 3
        label = get_partition_label(device) or "clonefs"
        return (device, partition_num, label)
    
    # If clonefs (p3) is booted, inactive is rootfs (p2)
    if booted_num == 3:
        device = "/dev/mmcblk0p2"
        partition_num = 2
        label = get_partition_label(device) or "rootfs"
        return (device, partition_num, label)
    
    return None


def get_inactive_partition_info() -> Optional[dict]:
    """
    Get the inactive partition information as a dictionary.
    
    Returns:
        Dictionary with keys 'device', 'partition_num', and 'label', or None
    """
    inactive = get_inactive_partition()
    if not inactive:
        return None
    
    device, partition_num, label = inactive
    return {
        'device': device,
        'partition_num': partition_num,
        'label': label
    }


# ============================================================================
# Tryboot Detection Functions
# ============================================================================

def is_tryboot_active() -> bool:
    """
    Check if the system was booted via tryboot mechanism.
    
    Returns:
        True if booted via tryboot, False if regular boot
    """
    # Check device tree node - this is set by firmware when tryboot is active
    tryboot_dt_path = Path("/sys/firmware/devicetree/base/chosen/bootloader/tryboot")
    if tryboot_dt_path.exists():
        try:
            # Read the value (4 bytes, little-endian integer)
            with open(tryboot_dt_path, "rb") as f:
                data = f.read(4)
                if len(data) == 4:
                    # Convert from little-endian bytes to integer
                    value = int.from_bytes(data, byteorder="little")
                    # Non-zero value indicates tryboot is active
                    if value != 0:
                        return True
        except (OSError, IOError):
            pass
    
    return False


# ============================================================================
# Status Functions
# ============================================================================

def get_system_status() -> Dict[str, Optional[str]]:
    """
    Get current system status information.
    
    Returns:
        Dictionary containing system status information
    """
    os_release = read_booted_os_release()
    
    status = {
        "os_name": os_release.name if os_release else None,
        "os_pretty_name": os_release.pretty_name if os_release else None,
        "os_version": os_release.version if os_release else None,
        "os_version_id": os_release.version_id if os_release else None,
        "os_version_commit": os_release.version_commit if os_release else None,
        "os_id": os_release.id if os_release else None,
    }
    
    # Add any additional fields from os-release if available
    if os_release:
        # Add other common fields that might be useful
        for key in ["BUILD_ID", "VERSION_CODENAME", "HOME_URL", "SUPPORT_URL", "BUG_REPORT_URL"]:
            value = os_release.get(key)
            if value:
                status[key.lower()] = value
    
    # Add partition information
    booted = get_booted_partition()
    if booted:
        device, partition_num, label = booted
        status["active_partition"] = device
        status["active_partition_number"] = str(partition_num)
        status["active_partition_label"] = label
    
    inactive = get_inactive_partition()
    if inactive:
        device, partition_num, label = inactive
        status["inactive_partition"] = device
        status["inactive_partition_number"] = str(partition_num)
        status["inactive_partition_label"] = label
    
    # Add tryboot information
    status["booted_via_tryboot"] = str(is_tryboot_active())
    
    return status


def format_status_text(status: Dict[str, Optional[str]]) -> str:
    """
    Format status information as human-readable text.
    
    Args:
        status: Status dictionary from get_system_status()
        
    Returns:
        Formatted status string
    """
    lines = []
    
    if status.get("os_pretty_name"):
        lines.append(f"OS: {status['os_pretty_name']}")
    elif status.get("os_name"):
        lines.append(f"OS: {status['os_name']}")
    
    if status.get("os_version_commit"):
        lines.append(f"Version Commit: {status['os_version_commit']}")
    
    if status.get("support_url"):
        lines.append(f"Support: {status['support_url']}")
    
    # Add partition information
    lines.append("")
    lines.append("Partitions:")
    if status.get("active_partition"):
        active_label = status.get("active_partition_label", "unknown")
        active_device = status.get("active_partition", "unknown")
        lines.append(f"  Active: {active_device} ({active_label})")
    else:
        lines.append("  Active: (unknown)")
    
    if status.get("inactive_partition"):
        inactive_label = status.get("inactive_partition_label", "unknown")
        inactive_device = status.get("inactive_partition", "unknown")
        lines.append(f"  Inactive: {inactive_device} ({inactive_label})")
    else:
        lines.append("  Inactive: (unknown)")
    
    # Add boot method information
    lines.append("")
    lines.append("Boot:")
    booted_via_tryboot = status.get("booted_via_tryboot", "False")
    if booted_via_tryboot.lower() == "true":
        lines.append("  Method: tryboot")
    else:
        lines.append("  Method: regular")
    
    return "\n".join(lines)


def format_status_json(status: Dict[str, Optional[str]]) -> str:
    """
    Format status information as JSON.
    
    Args:
        status: Status dictionary from get_system_status()
        
    Returns:
        JSON-formatted status string
    """
    return json.dumps(status, indent=2, ensure_ascii=False)
