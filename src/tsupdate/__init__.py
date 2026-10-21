"""tsupdate - Update daemon for tsOS-based devices."""

import logging
import os

from tsupdate.journal import log_handler
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


def configure_logging(identifier: str, verbose: bool = False) -> None:
    """
    Configure logging for the tsupdate tool.
    
    Under systemd, records go to the journal with their syslog priority;
    otherwise they go to stderr.
    
    Args:
        identifier: Program name, used as SYSLOG_IDENTIFIER in the journal
        verbose: If True, set log level to DEBUG. Otherwise, use INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # Set the stderr format based on level
    if verbose:
        # More detailed format for DEBUG
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    else:
        # Simple format for INFO and above
        formatter = logging.Formatter('%(levelname)s: %(message)s')
    
    # Journal handler under systemd, stderr handler otherwise
    handler = log_handler(identifier, formatter)
    handler.setLevel(level)
    root_logger.addHandler(handler)


def is_root() -> bool:
    """
    Check if the current process is running as root.
    
    Returns:
        True if running as root (effective UID is 0), False otherwise
    """
    return os.geteuid() == 0
