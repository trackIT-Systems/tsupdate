"""tsupdate - Update daemon for tsOS-based devices."""

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

