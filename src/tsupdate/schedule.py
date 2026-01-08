"""Schedule parsing and maintenance window checking."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

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
        logger.debug(f"Schedule file not found: {schedule_path}")
        return None
    
    try:
        with open(schedule_path, "r", encoding="utf-8") as f:
            schedule = yaml.safe_load(f)
        
        if schedule is None:
            logger.warning(f"Schedule file is empty: {schedule_path}")
            return None
        
        if not isinstance(schedule, dict):
            logger.warning(f"Invalid schedule format in {schedule_path}: expected dictionary")
            return None
        
        logger.debug(f"Loaded schedule from {schedule_path}")
        return schedule
        
    except yaml.YAMLError as e:
        logger.warning(f"Failed to parse schedule YAML: {e}")
        return None
    except (OSError, IOError) as e:
        logger.warning(f"Failed to read schedule file: {e}")
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


def is_in_maintenance_window(maintenance_entry: dict) -> bool:
    """
    Check if current time (system timezone) is within maintenance window.
    
    Uses scheduleparse library to parse start/stop times including sunrise/sunset calculations.
    
    Args:
        maintenance_entry: Maintenance schedule entry dictionary with 'start' and 'stop' fields
        
    Returns:
        True if current time is within maintenance window, False otherwise
    """
    if ScheduleEntry is None:
        logger.error("scheduleparse library not available - cannot check maintenance window")
        return False
    
    if not isinstance(maintenance_entry, dict):
        logger.warning("Invalid maintenance entry format")
        return False
    
    start_str = maintenance_entry.get("start")
    stop_str = maintenance_entry.get("stop")
    name = maintenance_entry.get("name", "maintenance")
    
    if not start_str or not stop_str:
        logger.warning("Maintenance entry missing 'start' or 'stop' field")
        return False
    
    try:
        # Create ScheduleEntry from the maintenance entry
        # ScheduleEntry handles parsing start/stop times including sunrise/sunset calculations
        schedule_entry = ScheduleEntry(
            name=name,
            start=start_str,
            stop=stop_str,
        )
        
        # Check if current time is within the maintenance window
        # Passing None uses current local time automatically
        in_window = schedule_entry.active()
        
        if in_window:
            logger.info(f"Current time is within maintenance window ({start_str} - {stop_str})")
        else:
            logger.info(f"Current time is outside maintenance window ({start_str} - {stop_str})")
        
        return in_window
        
    except Exception as e:
        logger.error(f"Error checking maintenance window: {e}", exc_info=True)
        return False


def get_next_maintenance_window_start(maintenance_entry: dict) -> Optional[datetime]:
    """
    Get the datetime when the next maintenance window starts.
    
    Args:
        maintenance_entry: Maintenance schedule entry dictionary with 'start' and 'stop' fields
        
    Returns:
        Datetime when next maintenance window starts, or None on error
    """
    if ScheduleEntry is None:
        logger.error("scheduleparse library not available - cannot get next maintenance window")
        return None
    
    if not isinstance(maintenance_entry, dict):
        logger.warning("Invalid maintenance entry format")
        return None
    
    start_str = maintenance_entry.get("start")
    stop_str = maintenance_entry.get("stop")
    name = maintenance_entry.get("name", "maintenance")
    
    if not start_str or not stop_str:
        logger.warning("Maintenance entry missing 'start' or 'stop' field")
        return None
    
    try:
        # Create ScheduleEntry from the maintenance entry
        schedule_entry = ScheduleEntry(
            name=name,
            start=start_str,
            stop=stop_str,
        )
        
        # Get next start time
        next_start = schedule_entry.next_start()
        
        if next_start:
            logger.debug(f"Next maintenance window starts at: {next_start}")
            return next_start
        else:
            logger.warning("Could not determine next maintenance window start time")
            return None
        
    except Exception as e:
        logger.error(f"Error getting next maintenance window start: {e}", exc_info=True)
        return None

