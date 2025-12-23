"""tsupdate - Update daemon for tsOS-based devices."""

import logging
import os
import sys

from tsupdate.status import read_booted_os_release

__version__ = "2025.12.1"

# Read booted system OS release information at module import
_booted_os_release = read_booted_os_release()

# Module-level variables for easy access to booted system info
BOOTED_OS_NAME = _booted_os_release.name if _booted_os_release else None
BOOTED_OS_PRETTY_NAME = _booted_os_release.pretty_name if _booted_os_release else None
BOOTED_OS_VERSION = _booted_os_release.version if _booted_os_release else None
BOOTED_OS_VERSION_ID = _booted_os_release.version_id if _booted_os_release else None
BOOTED_OS_VERSION_COMMIT = _booted_os_release.version_commit if _booted_os_release else None
BOOTED_OS_ID = _booted_os_release.id if _booted_os_release else None


def configure_logging(verbose: bool = False) -> None:
    """
    Configure logging for the tsupdate tool.
    
    Args:
        verbose: If True, set log level to DEBUG. Otherwise, use INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # Create handler for stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    
    # Set format based on level
    if verbose:
        # More detailed format for DEBUG
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    else:
        # Simple format for INFO and above
        formatter = logging.Formatter('%(levelname)s: %(message)s')
    
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


def is_root() -> bool:
    """
    Check if the current process is running as root.
    
    Returns:
        True if running as root (effective UID is 0), False otherwise
    """
    return os.geteuid() == 0
