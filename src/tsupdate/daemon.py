"""Background daemon for automatic system updates."""

import logging
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import yaml

from tsupdate import configure_logging, is_root
from tsupdate.apply import execute_apply
from tsupdate.github import (
    fetch_releases,
    find_applicable_batch_update,
    initialize_github_token,
    parse_github_repo_url,
)
from tsupdate.schedule import (
    find_maintenance_entry,
    get_next_maintenance_window_start,
    is_in_maintenance_window,
    load_schedule,
)
from tsupdate.status import is_tryboot_active, read_booted_os_release
from tsupdate.syncroot import execute_syncroot
from tsupdate.tryboot import execute_tryboot, persist_boot_configuration
from tsupdate.utils import ARTIFACTS_DIR, ensure_file

logger = logging.getLogger(__name__)

# Global cancellation flag for signal handling
_cancel_update = False
_shutdown_requested = False


def load_config(config_path: Path) -> dict:
    """
    Load and parse YAML configuration file.
    
    Args:
        config_path: Path to YAML configuration file
        
    Returns:
        Configuration dictionary with defaults applied
        
    Raises:
        SystemExit: If configuration file cannot be loaded or is invalid
    """
    # Default configuration
    default_config = {
        "check_interval": 3600,  # 1 hour
        "include_prereleases": False,
        "github_url": None,
        "max_releases": 5,
        "persist_timeout": 600,  # 10 minutes
        "update_countdown": 60,  # 1 minute
        "do": "check",  # Regular behavior: what to do (nothing, check, download, apply)
        "maintenance_check_interval": None,  # Maintenance behavior: check interval (None = use check_interval)
        "maintenance_do": None,  # Maintenance behavior: what to do (None = use do, or "apply" if maintenance schedule exists)
    }
    
    # Valid values for do and maintenance_do
    valid_modes = {"nothing", "check", "download", "apply"}
    
    # If config file doesn't exist, use defaults
    if not config_path.exists():
        logger.warning(f"Configuration file not found: {config_path}")
        logger.info("Using default configuration")
        return default_config
    
    # Load YAML file
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
        
        if user_config is None:
            logger.warning(f"Configuration file is empty: {config_path}")
            return default_config
        
        if not isinstance(user_config, dict):
            logger.error(f"Invalid configuration format in {config_path}: expected dictionary")
            sys.exit(1)
        
        # Merge user config with defaults
        config = default_config.copy()
        config.update(user_config)
        
        # Validate do (regular behavior)
        if "do" in user_config:
            mode = user_config.get("do")
            if mode not in valid_modes:
                logger.warning(
                    f"Invalid do value: {mode}. "
                    f"Valid values are: {', '.join(sorted(valid_modes))}. "
                    f"Using default: {default_config['do']}"
                )
                config["do"] = default_config["do"]
        
        # Validate maintenance_do (maintenance behavior)
        if "maintenance_do" in user_config:
            mode = user_config.get("maintenance_do")
            if mode not in valid_modes:
                logger.warning(
                    f"Invalid maintenance_do value: {mode}. "
                    f"Valid values are: {', '.join(sorted(valid_modes))}. "
                    f"Using default behavior"
                )
                config["maintenance_do"] = None
        
        # Validate and enforce minimum check_interval (must be integer, minimum 60 seconds)
        min_interval = 60
        check_interval = config.get("check_interval", 3600)
        if not isinstance(check_interval, int):
            logger.error(
                f"check_interval must be an integer in seconds (got {type(check_interval).__name__}: {check_interval})"
            )
            sys.exit(1)
        elif check_interval < min_interval:
            logger.warning(
                f"check_interval ({check_interval}) is less than minimum ({min_interval}s). "
                f"Setting to {min_interval}s"
            )
            config["check_interval"] = min_interval
        
        # Set maintenance_check_interval default if not specified
        if config.get("maintenance_check_interval") is None:
            config["maintenance_check_interval"] = config.get("check_interval", 3600)
        
        # Validate and enforce minimum maintenance_check_interval (must be integer, minimum 60 seconds)
        maintenance_check_interval = config.get("maintenance_check_interval", 3600)
        if not isinstance(maintenance_check_interval, int):
            logger.error(
                f"maintenance_check_interval must be an integer in seconds (got {type(maintenance_check_interval).__name__}: {maintenance_check_interval})"
            )
            sys.exit(1)
        elif maintenance_check_interval < min_interval:
            logger.warning(
                f"maintenance_check_interval ({maintenance_check_interval}) is less than minimum ({min_interval}s). "
                f"Setting to {min_interval}s"
            )
            config["maintenance_check_interval"] = min_interval
        
        logger.info(f"Loaded configuration from {config_path}")
        logger.debug(f"Configuration: {config}")
        
        return config
        
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse YAML configuration: {e}")
        sys.exit(1)
    except (OSError, IOError) as e:
        logger.error(f"Failed to read configuration file: {e}")
        sys.exit(1)


def signal_handler(signum: int, frame) -> None:
    """
    Handle termination signals.
    
    Args:
        signum: Signal number
        frame: Current stack frame
    """
    global _cancel_update, _shutdown_requested
    
    signal_name = signal.Signals(signum).name
    logger.info(f"Received signal {signal_name}")
    
    # If we're in countdown, cancel the update
    if _cancel_update is not None:
        logger.info("Cancelling update...")
        _cancel_update = True
    
    # Mark shutdown as requested
    _shutdown_requested = True


def setup_signal_handlers() -> None:
    """Register signal handlers for graceful shutdown and cancellation."""
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    logger.debug("Signal handlers registered for SIGTERM and SIGINT")


def get_system_uptime() -> float:
    """
    Get system uptime in seconds.
    
    Returns:
        System uptime in seconds
    """
    try:
        with open("/proc/uptime", "r") as f:
            uptime_seconds = float(f.read().split()[0])
        return uptime_seconds
    except (OSError, IOError, ValueError, IndexError):
        logger.warning("Could not read system uptime from /proc/uptime")
        return 0.0


def handle_tryboot_persist(timeout: int) -> None:
    """
    Handle tryboot persistence on daemon startup.
    
    If the system is booted via tryboot, waits until system uptime reaches
    the specified timeout, then persists the boot configuration.
    
    Args:
        timeout: Seconds of system uptime to wait before persisting
    """
    if not is_tryboot_active():
        logger.info("System booted regularly (not via tryboot)")
        return
    
    # Get current system uptime
    uptime = get_system_uptime()
    logger.info(f"System booted via tryboot (uptime: {uptime:.1f} seconds)")
    logger.info(f"Will persist configuration after {timeout} seconds of uptime")
    logger.info("This allows time to verify the update was successful")
    
    # Calculate remaining wait time based on uptime
    remaining_wait = timeout - uptime
    
    if remaining_wait <= 0:
        logger.info(f"System uptime ({uptime:.1f}s) already exceeds persist timeout ({timeout}s)")
        logger.info("Persisting configuration immediately")
    else:
        logger.info(f"Waiting {remaining_wait:.1f} seconds before persisting (until uptime reaches {timeout}s)")
        
        # Sleep in small intervals to allow signal handling
        elapsed = 0
        while elapsed < remaining_wait:
            if _shutdown_requested:
                logger.info("Shutdown requested during tryboot persist wait - exiting")
                sys.exit(0)
            
            sleep_time = min(10, remaining_wait - elapsed)
            time.sleep(sleep_time)
            elapsed += sleep_time
            
            remaining = remaining_wait - elapsed
            if remaining > 0 and remaining % 60 == 0:
                uptime_at_persist = uptime + remaining_wait
                logger.info(f"Persisting tryboot configuration in {remaining:.0f} seconds (at uptime {uptime_at_persist:.0f}s)...")
    
    # Persist the configuration
    logger.info("Persisting tryboot boot configuration...")
    if persist_boot_configuration():
        final_uptime = get_system_uptime()
        logger.info(f"Boot configuration persisted successfully at uptime {final_uptime:.1f}s")
    else:
        logger.error("Failed to persist boot configuration")
        logger.error("Manual intervention may be required")


def notify_users(message: str) -> None:
    """
    Notify users via logging and wall command.
    
    Args:
        message: Message to broadcast
    """
    # Log the message
    logger.info(message)
    
    # Broadcast via wall command
    try:
        subprocess.run(
            ["wall", message],
            check=False,  # Don't fail if wall is not available
            timeout=5,
            capture_output=True,
        )
        logger.debug("Message broadcast via wall command")
    except FileNotFoundError:
        logger.debug("wall command not available - skipping broadcast")
    except subprocess.TimeoutExpired:
        logger.warning("wall command timed out")
    except Exception as e:
        logger.debug(f"Failed to broadcast via wall: {e}")


def countdown_with_cancel(seconds: int) -> bool:
    """
    Countdown timer with cancellation support.
    
    Checks for cancellation signal every second. Sends wall reminders
    at 30 seconds and 10 seconds remaining.
    
    Args:
        seconds: Countdown duration in seconds
        
    Returns:
        True if countdown completed, False if cancelled
    """
    global _cancel_update
    _cancel_update = False
    
    logger.info(f"Starting countdown: {seconds} seconds until reboot")
    
    for remaining in range(seconds, 0, -1):
        if _cancel_update:
            logger.info("Update cancelled by signal")
            _cancel_update = False
            return False
        
        # Send reminders at specific intervals
        if remaining == 30:
            notify_users(f"WARNING: System will reboot in 30 seconds. Send SIGTERM to cancel.")
        elif remaining == 10:
            notify_users(f"WARNING: System will reboot in 10 seconds. Send SIGTERM to cancel.")
        elif remaining % 10 == 0:
            logger.debug(f"Countdown: {remaining} seconds remaining")
        
        time.sleep(1)
    
    logger.info("Countdown complete")
    return True


def download_update(update_url: str, config: dict) -> Optional[Path]:
    """
    Download update file.
    
    Downloads the update file from URL to the artifacts directory.
    
    Args:
        update_url: URL to update file
        config: Configuration dictionary
        
    Returns:
        Path to downloaded file if successful, None otherwise
    """
    try:
        logger.info(f"Downloading update from {update_url}...")
        
        # Use ensure_file to download (or use cached file if already downloaded)
        file_path, is_local = ensure_file(update_url, ARTIFACTS_DIR)
        
        if file_path is None:
            logger.error("Failed to download update file")
            return None
        
        if is_local:
            logger.info(f"Using existing local file: {file_path}")
        else:
            file_size = file_path.stat().st_size
            logger.info(f"Downloaded update file: {file_path} ({file_size} bytes)")
        
        return file_path
        
    except Exception as e:
        logger.error(f"Error downloading update: {e}", exc_info=True)
        return None


def get_current_behavior(
    config: dict, maintenance_entry: Optional[dict], in_maintenance_window: bool
) -> tuple[int, str]:
    """
    Get current behavior configuration based on maintenance window status.
    
    Args:
        config: Configuration dictionary
        maintenance_entry: Optional maintenance schedule entry dictionary
        in_maintenance_window: Whether currently in maintenance window
        
    Returns:
        Tuple of (check_interval, do_mode) for current behavior
    """
    # If no maintenance schedule, always use regular behavior
    if not maintenance_entry:
        check_interval = config.get("check_interval", 3600)
        do_mode = config.get("do", "check")
        return (check_interval, do_mode)
    
    # If inside maintenance window, use maintenance behavior
    if in_maintenance_window:
        maintenance_check_interval = config.get("maintenance_check_interval")
        if maintenance_check_interval is None:
            # Default to regular check_interval if not specified
            maintenance_check_interval = config.get("check_interval", 3600)
        
        maintenance_do = config.get("maintenance_do")
        if maintenance_do is None:
            # Default to "apply" if maintenance schedule exists but maintenance_do not specified
            maintenance_do = "apply"
        
        return (maintenance_check_interval, maintenance_do)
    
    # Outside maintenance window, use regular behavior
    check_interval = config.get("check_interval", 3600)
    do_mode = config.get("do", "check")
    return (check_interval, do_mode)


def check_for_updates(config: dict) -> Optional[str]:
    """
    Check for available updates.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        Update URL if available, None otherwise
    """
    try:
        # Read os-release for VERSION_ID and SUPPORT_URL
        os_release = read_booted_os_release()
        if not os_release:
            logger.error("Could not read /etc/os-release")
            return None
        
        # Get GitHub URL from config or os-release
        github_url = config.get("github_url")
        if github_url:
            support_url = github_url
            logger.debug(f"Using GitHub URL from config: {support_url}")
        else:
            support_url = os_release.get("SUPPORT_URL")
            if not support_url:
                logger.error("SUPPORT_URL not found in /etc/os-release and no github_url in config")
                return None
            logger.debug(f"Using SUPPORT_URL from os-release: {support_url}")
        
        # Get VERSION_ID
        version_id = os_release.version_id
        if not version_id:
            logger.error("VERSION_ID not found in /etc/os-release")
            return None
        
        logger.debug(f"Current VERSION_ID: {version_id}")
        
        # Parse GitHub repo URL
        repo_info = parse_github_repo_url(support_url)
        if not repo_info:
            logger.error(f"Could not parse GitHub repository URL: {support_url}")
            return None
        
        owner, repo = repo_info
        logger.debug(f"GitHub repository: {owner}/{repo}")
        
        # Fetch releases
        include_prereleases = config.get("include_prereleases", False)
        releases = fetch_releases(owner, repo, include_prereleases=include_prereleases)
        if not releases:
            logger.debug("Could not fetch releases from GitHub")
            return None
        
        # Find applicable batch update
        max_releases = config.get("max_releases", 5)
        batch_url = find_applicable_batch_update(releases, version_id, max_releases=max_releases)
        
        if batch_url:
            logger.info(f"Update available: {batch_url}")
            return batch_url
        else:
            logger.debug(f"No applicable batch update found for version {version_id}")
            return None
            
    except Exception as e:
        logger.error(f"Error checking for updates: {e}", exc_info=True)
        return None


def sync_and_apply_update(update_file_path: Path, config: dict) -> bool:
    """
    Sync root partition and apply update from file.
    
    Syncs root partition to inactive partition, applies update, notifies users,
    and initiates tryboot reboot after countdown.
    
    Args:
        update_file_path: Path to downloaded update file
        config: Configuration dictionary
        
    Returns:
        True if update applied and reboot initiated, False on error or cancellation
    """
    try:
        # Step 1: Sync root partition to inactive partition
        logger.info("Preparing inactive partition...")
        notify_users("Preparing system update - syncing partitions...")
        
        syncroot_result = execute_syncroot()
        if syncroot_result != 0:
            logger.error("Failed to sync root partition - aborting update")
            notify_users("Update failed: could not sync partitions")
            return False
        
        logger.info("Partition sync complete")
        
        # Step 2: Apply the update
        logger.info(f"Applying update from {update_file_path}...")
        notify_users(f"Applying update: {update_file_path.name}")
        
        # Use file path directly (execute_apply will handle it)
        apply_result = execute_apply(str(update_file_path), keep_download=False)
        if apply_result != 0:
            logger.error("Failed to apply update - aborting")
            notify_users("Update failed: could not apply update")
            return False
        
        logger.info("Update applied successfully")
        
        # Step 3: Notify users about impending reboot
        countdown = config.get("update_countdown", 60)
        notify_users(
            f"Update applied successfully!\n"
            f"System will reboot in {countdown} seconds to activate the update.\n"
            f"To cancel, send SIGTERM to the tsupdated process (kill -TERM <pid>)"
        )
        
        # Step 4: Countdown with cancellation support
        if not countdown_with_cancel(countdown):
            logger.info("Update cancelled - system will not reboot")
            notify_users("Update cancelled - system will not reboot. Update remains on inactive partition.")
            return False
        
        # Step 5: Execute tryboot (which will reboot the system)
        logger.info("Initiating tryboot reboot...")
        notify_users("Rebooting to activate update (via tryboot)...")
        
        tryboot_result = execute_tryboot(reboot=True)
        if tryboot_result != 0:
            logger.error("Failed to configure tryboot")
            notify_users("Failed to configure tryboot - manual intervention required")
            return False
        
        # If we get here, reboot was initiated
        # The daemon will exit and systemd will restart it after reboot
        return True
        
    except Exception as e:
        logger.error(f"Error during update workflow: {e}", exc_info=True)
        notify_users(f"Update failed: {e}")
        return False


def apply_update_workflow(update_url: str, config: dict, update_file_path: Optional[Path] = None) -> bool:
    """
    Complete update application workflow.
    
    Downloads update (if not already downloaded), syncs root partition, applies update,
    notifies users, and initiates tryboot reboot after countdown.
    
    Args:
        update_url: URL to update file
        config: Configuration dictionary
        update_file_path: Optional path to already downloaded update file
        
    Returns:
        True if update applied and reboot initiated, False on error or cancellation
    """
    try:
        # Download update if not already provided
        if update_file_path is None:
            update_file_path = download_update(update_url, config)
            if update_file_path is None:
                logger.error("Failed to download update - aborting")
                notify_users("Update failed: could not download update")
                return False
        else:
            logger.info(f"Using already downloaded update file: {update_file_path}")
        
        # Sync and apply the update
        return sync_and_apply_update(update_file_path, config)
        
    except Exception as e:
        logger.error(f"Error during update workflow: {e}", exc_info=True)
        notify_users(f"Update failed: {e}")
        return False


def main_loop(config: dict, maintenance_entry: Optional[dict] = None) -> None:
    """
    Main daemon loop.
    
    Periodically checks for updates and applies them when found.
    Uses regular behavior by default, switches to maintenance behavior when inside maintenance window.
    
    Args:
        config: Configuration dictionary
        maintenance_entry: Optional maintenance schedule entry dictionary
    """
    logger.info("Entering main update check loop")
    
    if maintenance_entry:
        regular_interval = config.get("check_interval", 3600)
        regular_do = config.get("do", "check")
        maintenance_interval = config.get("maintenance_check_interval", regular_interval)
        maintenance_do = config.get("maintenance_do", "apply")
        logger.info(
            f"Maintenance schedule is active - "
            f"Regular: check_interval={regular_interval}s, do={regular_do}; "
            f"Maintenance: check_interval={maintenance_interval}s, do={maintenance_do}"
        )
    else:
        check_interval = config.get("check_interval", 3600)
        do_mode = config.get("do", "check")
        logger.info(f"No maintenance schedule configured - check_interval={check_interval}s, do={do_mode}")
    
    while not _shutdown_requested:
        try:
            # Determine if we're in maintenance window
            in_maintenance_window = False
            if maintenance_entry:
                in_maintenance_window = is_in_maintenance_window(maintenance_entry)
            
            # Get current behavior based on maintenance window status
            check_interval, do_mode = get_current_behavior(config, maintenance_entry, in_maintenance_window)
            
            # If mode is "nothing", skip check entirely
            if do_mode == "nothing":
                if in_maintenance_window:
                    logger.warning("Mode is 'nothing' but inside maintenance window - this is unusual, skipping check")
                else:
                    logger.debug(f"Mode is 'nothing' - skipping check")
                
                # If outside maintenance window, wait until next maintenance window starts
                if maintenance_entry and not in_maintenance_window:
                    next_start = get_next_maintenance_window_start(maintenance_entry)
                    if next_start:
                        now = datetime.now(next_start.tzinfo) if next_start.tzinfo else datetime.now()
                        sleep_seconds = (next_start - now).total_seconds()
                        if sleep_seconds > 0:
                            logger.info(f"Waiting until next maintenance window starts at {next_start.strftime('%Y-%m-%d %H:%M:%S %Z')} ({sleep_seconds:.0f} seconds)")
                            elapsed = 0
                            while elapsed < sleep_seconds and not _shutdown_requested:
                                sleep_time = min(10, sleep_seconds - elapsed)
                                time.sleep(sleep_time)
                                elapsed += sleep_time
                        else:
                            # Next start is in the past (shouldn't happen, but handle gracefully)
                            logger.warning("Next maintenance window start is in the past, using check_interval")
                            elapsed = 0
                            while elapsed < check_interval and not _shutdown_requested:
                                sleep_time = min(10, check_interval - elapsed)
                                time.sleep(sleep_time)
                                elapsed += sleep_time
                    else:
                        # Could not determine next maintenance window, fall back to check_interval
                        logger.warning("Could not determine next maintenance window, using check_interval")
                        logger.info(f"Next update check in {check_interval} seconds")
                        elapsed = 0
                        while elapsed < check_interval and not _shutdown_requested:
                            sleep_time = min(10, check_interval - elapsed)
                            time.sleep(sleep_time)
                            elapsed += sleep_time
                else:
                    # Inside window or no schedule - use check_interval
                    logger.info(f"Next update check in {check_interval} seconds")
                    elapsed = 0
                    while elapsed < check_interval and not _shutdown_requested:
                        sleep_time = min(10, check_interval - elapsed)
                        time.sleep(sleep_time)
                        elapsed += sleep_time
                continue
            
            # Check for available updates
            logger.info("Checking for updates...")
            update_url = check_for_updates(config)
            
            if update_url:
                logger.info(f"Update found: {update_url}")
                
                # Determine what to do based on current behavior mode
                if do_mode == "check":
                    if in_maintenance_window:
                        logger.info(
                            "Update found in maintenance window (mode: check) - "
                            "deferring update. Will check again at next interval."
                        )
                    else:
                        logger.info(
                            "Update found (mode: check) - "
                            "deferring update. Will check again at next interval."
                        )
                    # Just log and continue to sleep
                elif do_mode == "download":
                    if in_maintenance_window:
                        logger.info(
                            "Update found in maintenance window (mode: download) - "
                            "downloading update file."
                        )
                    else:
                        logger.info(
                            "Update found (mode: download) - "
                            "downloading update file."
                        )
                    update_file_path = download_update(update_url, config)
                    if update_file_path:
                        logger.info(
                            f"Update downloaded successfully: {update_file_path}. "
                            "Will apply during next maintenance window or when mode changes to 'apply'."
                        )
                    else:
                        logger.error("Failed to download update")
                elif do_mode == "apply":
                    if in_maintenance_window:
                        logger.info("Update found in maintenance window (mode: apply) - proceeding with full update workflow")
                    else:
                        logger.info("Update found (mode: apply) - proceeding with full update workflow")
                    
                    success = apply_update_workflow(update_url, config)
                    if success:
                        logger.info("Tryboot reboot initiated - daemon exiting")
                        sys.exit(0)
                    else:
                        logger.info("Update workflow did not complete - continuing normal operation")
                # else: nothing mode already handled above
            else:
                logger.info("No updates available")
            
            # Sleep until next check
            logger.info(f"Next update check in {check_interval} seconds")
            
            # Sleep in small intervals to allow signal handling
            elapsed = 0
            while elapsed < check_interval and not _shutdown_requested:
                sleep_time = min(10, check_interval - elapsed)
                time.sleep(sleep_time)
                elapsed += sleep_time
            
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            logger.info("Continuing operation despite error")
            
            # Sleep a bit before retrying
            time.sleep(60)
    
    logger.info("Shutdown requested - exiting main loop")


def run_daemon(config_path: Path, schedule_path: Optional[Path] = None, verbose: bool = False) -> None:
    """
    Run the update daemon.
    
    Args:
        config_path: Path to configuration file
        schedule_path: Optional path to schedule YAML file
        verbose: Enable debug logging
    """
    # Configure logging
    configure_logging(verbose=verbose)
    
    logger.info("Starting tsupdated - automatic update daemon")
    logger.info(f"Configuration file: {config_path}")
    
    # Check if running as root
    if not is_root():
        logger.error("Daemon must be run as root")
        sys.exit(1)
    
    # Initialize GitHub token
    initialize_github_token()
    
    # Setup signal handlers
    setup_signal_handlers()
    
    # Load configuration
    config = load_config(config_path)
    
    # Load schedule and find maintenance entry
    maintenance_entry = None
    if schedule_path:
        logger.info(f"Schedule file: {schedule_path}")
        schedule = load_schedule(schedule_path)
        if schedule:
            maintenance_entry = find_maintenance_entry(schedule)
            if maintenance_entry:
                logger.info("Maintenance schedule loaded successfully")
            else:
                logger.info("No maintenance entry found in schedule - updates can be applied at any time")
        else:
            logger.info("Schedule file not found or invalid - updates can be applied at any time")
    
    # Handle tryboot persistence on startup
    persist_timeout = config.get("persist_timeout", 600)
    handle_tryboot_persist(persist_timeout)
    
    # Enter main loop
    try:
        main_loop(config, maintenance_entry=maintenance_entry)
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard - shutting down")
    except Exception as e:
        logger.error(f"Unexpected error in daemon: {e}", exc_info=True)
        sys.exit(1)
    
    logger.info("Daemon shutdown complete")

