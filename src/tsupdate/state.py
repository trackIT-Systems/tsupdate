"""Persistent record of update attempts, to stop a failing update from boot-looping.

If a new partition fails to boot, the Pi falls back to the old one. The daemon
would then find the very same update again and re-apply it, forever. Before
rebooting into an update the daemon therefore records the attempt here; on later
checks an update that was already tried MAX_UPDATE_ATTEMPTS times from the
currently running version is not applied again.

The file lives on /data because that is persistent across reboots (the root
filesystem is not). It can only be used to suppress updates, never to trigger one.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = Path("/data/tsupdate-state.json")

# Total tries per (update, running version): the first, plus one retry in case
# the first reboot was interrupted for a reason unrelated to the update (power cut).
MAX_UPDATE_ATTEMPTS = 2


def _load(path: Optional[Path] = None) -> dict:
    path = path or STATE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning(f"Ignoring unreadable state file {path}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict, path: Optional[Path] = None) -> None:
    path = path or STATE_FILE
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f"Could not write state file {path}: {e}")


def record_attempt(update_url: str, from_version: Optional[str]) -> int:
    """
    Record that the daemon is about to reboot into `update_url`.

    Returns:
        How many times this update has now been attempted from `from_version`
    """
    data = _load()
    attempts = 1
    if data.get("update_url") == update_url and data.get("from_version") == from_version:
        attempts = int(data.get("attempts", 0)) + 1
    _save({"update_url": update_url, "from_version": from_version, "attempts": attempts})
    return attempts


def block(update_url: str, from_version: Optional[str]) -> None:
    """Mark an update as not to be applied again from `from_version` (e.g. cancelled by the operator)."""
    _save({"update_url": update_url, "from_version": from_version, "attempts": MAX_UPDATE_ATTEMPTS})


def discard_attempt() -> None:
    """Forget the recorded attempt (e.g. the reboot was never actually initiated)."""
    try:
        STATE_FILE.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Could not remove state file {STATE_FILE}: {e}")


def is_blocked(update_url: str, current_version: Optional[str]) -> bool:
    """Whether `update_url` was already tried too often from the running version."""
    data = _load()
    return (
        data.get("update_url") == update_url
        and data.get("from_version") == current_version
        and int(data.get("attempts", 0)) >= MAX_UPDATE_ATTEMPTS
    )


def forget_if_version_changed(current_version: Optional[str]) -> bool:
    """
    Drop a stale record once the running version differs from the one the update started from.

    Returns:
        True if a record was dropped, i.e. a recorded update has now been installed
    """
    if current_version is None:
        return False
    data = _load()
    if data and data.get("from_version") != current_version:
        logger.info(f"Running version changed ({data.get('from_version')} -> {current_version}); clearing update state")
        discard_attempt()
        return True
    return False
