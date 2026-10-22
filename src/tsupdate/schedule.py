"""Schedule parsing and maintenance window checking."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from tsupdate.errors import ConfigurationError

try:
    from scheduleparse import ScheduleEntry
except ImportError:
    ScheduleEntry = None  # type: ignore

logger = logging.getLogger(__name__)


def load_schedule(schedule_path: Path) -> Optional[dict]:
    """
    Load and parse schedule.yml file.
    
    Args:
        schedule_path: Path to schedule YAML file
        
    Returns:
        Schedule dictionary if file exists and is valid, None otherwise
    """
    if not schedule_path.exists():
        logger.debug("Schedule file not found: %s", schedule_path)
        return None
    
    try:
        with open(schedule_path, "r", encoding="utf-8") as f:
            schedule = yaml.safe_load(f)
        
        if schedule is None:
            logger.warning("Schedule file is empty: %s", schedule_path)
            return None
        
        if not isinstance(schedule, dict):
            logger.warning("Invalid schedule format in %s: expected dictionary", schedule_path)
            return None
        
        logger.debug("Loaded schedule from %s", schedule_path)
        return schedule
        
    except yaml.YAMLError as e:
        logger.warning("Failed to parse schedule YAML: %s", e)
        return None
    except (OSError, IOError) as e:
        logger.warning("Failed to read schedule file: %s", e)
        return None


def find_maintenance_entry(schedule: dict) -> Optional[dict]:
    """
    Find maintenance entry in schedule list.
    
    Args:
        schedule: Schedule dictionary
        
    Returns:
        Maintenance entry dictionary if found, None otherwise
    """
    if not isinstance(schedule, dict):
        return None
    
    schedule_list = schedule.get("schedule")
    if not schedule_list:
        logger.debug("No schedule list found in schedule file")
        return None
    
    if not isinstance(schedule_list, list):
        logger.warning("Schedule 'schedule' field is not a list")
        return None
    
    for entry in schedule_list:
        if isinstance(entry, dict) and entry.get("name") == "maintenance":
            logger.debug("Found maintenance entry in schedule")
            return entry
    
    logger.debug("No maintenance entry found in schedule")
    return None


def _schedule_entry(maintenance_entry: dict):
    """
    The scheduleparse ScheduleEntry for a maintenance entry.

    Raises:
        ConfigurationError: scheduleparse is not installed, or the entry is not a dictionary
            with 'start' and 'stop'.
    """
    if ScheduleEntry is None:
        raise ConfigurationError("The scheduleparse library is not available, cannot evaluate the maintenance window")

    if not isinstance(maintenance_entry, dict):
        raise ConfigurationError("Invalid maintenance entry: expected a dictionary with 'start' and 'stop'")

    start_str = maintenance_entry.get("start")
    stop_str = maintenance_entry.get("stop")
    if not start_str or not stop_str:
        raise ConfigurationError("Maintenance entry is missing 'start' or 'stop'")

    try:
        return ScheduleEntry(name=maintenance_entry.get("name", "maintenance"), start=start_str, stop=stop_str)
    except Exception as e:  # scheduleparse raises ValueError and others for a bad time
        raise ConfigurationError(f"Cannot evaluate the maintenance window ({_window(maintenance_entry)}): {e}") from e


def _window(maintenance_entry: dict) -> str:
    return f"{maintenance_entry.get('start')} - {maintenance_entry.get('stop')}"


def is_in_maintenance_window(maintenance_entry: dict) -> bool:
    """
    Check if current time (system timezone) is within maintenance window.
    
    Uses scheduleparse library to parse start/stop times including sunrise/sunset calculations.
    
    Args:
        maintenance_entry: Maintenance schedule entry dictionary with 'start' and 'stop' fields
        
    Returns:
        True if current time is within maintenance window, False otherwise

    Raises:
        ConfigurationError: The window cannot be evaluated (see _schedule_entry), or
            scheduleparse cannot parse its start or stop time.
    """
    schedule_entry = _schedule_entry(maintenance_entry)
    try:
        # Passing None uses current local time automatically
        in_window = schedule_entry.active()
    except Exception as e:  # scheduleparse raises ValueError and others for a bad time
        raise ConfigurationError(f"Cannot evaluate the maintenance window ({_window(maintenance_entry)}): {e}") from e

    where = "within" if in_window else "outside"
    logger.debug("Current time is %s maintenance window (%s)", where, _window(maintenance_entry))
    return in_window


def get_next_maintenance_window_start(maintenance_entry: dict) -> datetime:
    """
    Get the datetime when the next maintenance window starts.
    
    Args:
        maintenance_entry: Maintenance schedule entry dictionary with 'start' and 'stop' fields
        
    Returns:
        Datetime when next maintenance window starts

    Raises:
        ConfigurationError: The window cannot be evaluated (see _schedule_entry), scheduleparse
            cannot parse its start or stop time, or it has no next start.
    """
    schedule_entry = _schedule_entry(maintenance_entry)
    try:
        next_start = schedule_entry.next_start()
        # next_start() only evaluates `start`. Evaluate `stop` too, so that a typo in it
        # (scheduleparse raises ValueError) rejects the whole window up front instead of
        # being noticed only once the window should have opened.
        schedule_entry.next_stop()
    except Exception as e:  # scheduleparse raises ValueError and others for a bad time
        raise ConfigurationError(f"Cannot evaluate the maintenance window ({_window(maintenance_entry)}): {e}") from e

    if not next_start:
        raise ConfigurationError(f"Cannot determine when the maintenance window ({_window(maintenance_entry)}) starts")
    logger.debug("Next maintenance window starts at: %s", next_start)
    return next_start
