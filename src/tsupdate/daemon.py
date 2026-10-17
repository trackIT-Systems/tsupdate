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

from tsupdate import configure_logging, is_root, state as update_state
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
from tsupdate.utils import ARTIFACTS_DIR, DEFAULT_RSYNC_TIMEOUT_SEC, ensure_file, prune_artifacts

logger = logging.getLogger(__name__)

# Global cancellation flag for signal handling
_cancel_update = False
_shutdown_requested = False


def _validate_int_option(config: dict, key: str, minimum: int, default: int) -> None:
    """
    Check that config[key] is an integer >= minimum.

    A value of the wrong type is a configuration error and ends the daemon, like check_interval.
    A number below the minimum is replaced by the default, like a negative rsync_timeout.
    """
    value = config.get(key)
    if value is None:
        config[key] = default
        return
    if isinstance(value, bool) or not isinstance(value, int):
        logger.error(f"{key} must be an integer (got {type(value).__name__}: {value!r})")
        sys.exit(1)
    if value < minimum:
        logger.warning(f"{key} ({value}) must be at least {minimum}; using default {default}")
        config[key] = default


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
        "rsync_timeout": DEFAULT_RSYNC_TIMEOUT_SEC,  # wall-clock limit for syncroot/apply rsync (0 = unlimited)
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
        
        rsync_timeout = config.get("rsync_timeout", DEFAULT_RSYNC_TIMEOUT_SEC)
        if rsync_timeout is None:
            rsync_timeout = DEFAULT_RSYNC_TIMEOUT_SEC
        if isinstance(rsync_timeout, bool) or not isinstance(rsync_timeout, int):
            logger.error(
                "rsync_timeout must be an integer in seconds (got %s: %r)",
                type(rsync_timeout).__name__,
                rsync_timeout,
            )
            sys.exit(1)
        if rsync_timeout < 0:
            logger.warning(
                "rsync_timeout (%s) is negative; using default %s seconds",
                rsync_timeout,
                DEFAULT_RSYNC_TIMEOUT_SEC,
            )
            rsync_timeout = DEFAULT_RSYNC_TIMEOUT_SEC
        config["rsync_timeout"] = rsync_timeout
        
        _validate_int_option(config, "update_countdown", minimum=0, default=default_config["update_countdown"])
        _validate_int_option(config, "persist_timeout", minimum=0, default=default_config["persist_timeout"])
        _validate_int_option(config, "max_releases", minimum=1, default=default_config["max_releases"])
        
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
    
    # Cancels a pending reboot countdown (a no-op at any other time) ...
    _cancel_update = True
    
    # ... and stops the daemon. A cancelled update is remembered across the
    # restart by sync_and_apply_update(), so systemd restarting the daemon does
    # not simply start the same update again.
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


def handle_tryboot_persist(timeout: int) -> bool:
    """
    Handle tryboot persistence on daemon startup.
    
    If the system is booted via tryboot, waits until system uptime reaches
    the specified timeout, then persists the boot configuration.
    
    Args:
        timeout: Seconds of system uptime to wait before persisting
        
    Returns:
        True if the boot configuration is settled (regular boot, or persisted successfully)
    """
    if not is_tryboot_active():
        logger.info("System booted regularly (not via tryboot)")
        return True
    
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
        elapsed = 0.0
        last_progress = 0.0
        while elapsed < remaining_wait:
            if _shutdown_requested:
                logger.info("Shutdown requested during tryboot persist wait - exiting")
                sys.exit(0)
            
            sleep_time = min(10, remaining_wait - elapsed)
            time.sleep(sleep_time)
            elapsed += sleep_time
            
            # A progress line about once a minute. (The remaining time is a float derived from
            # /proc/uptime, so testing it for an exact multiple of 60 practically never matched.)
            remaining = remaining_wait - elapsed
            if remaining > 0 and elapsed - last_progress >= 60:
                last_progress = elapsed
                uptime_at_persist = uptime + remaining_wait
                logger.info(f"Persisting tryboot configuration in {remaining:.0f} seconds (at uptime {uptime_at_persist:.0f}s)...")
    
    # Persist the configuration
    logger.info("Persisting tryboot boot configuration...")
    if persist_boot_configuration():
        final_uptime = get_system_uptime()
        logger.info(f"Boot configuration persisted successfully at uptime {final_uptime:.1f}s")
        return True
    
    logger.error("Failed to persist boot configuration")
    logger.error("Manual intervention may be required")
    return False


def finalize_boot(persist_timeout: int) -> None:
    """
    Startup housekeeping after a (try)boot.

    Persists a tryboot configuration once the new system has been up long enough. An update
    that was attempted from an older version is history once the running version differs;
    only then, with the new boot configuration settled, is its download no longer needed and
    removed. Until then a failed boot would fall back and retry from the cached file instead
    of downloading again.
    """
    settled = handle_tryboot_persist(persist_timeout)
    if update_state.forget_if_version_changed(_running_version()) and settled:
        prune_artifacts(ARTIFACTS_DIR)


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
        # _shutdown_requested also covers a SIGTERM that arrived while the update was
        # still being applied, before this countdown started.
        if _cancel_update or _shutdown_requested:
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


def sleep_interruptible(seconds: float) -> None:
    """Sleep in small steps so SIGTERM/SIGINT stop the daemon promptly."""
    elapsed = 0.0
    while elapsed < seconds and not _shutdown_requested:
        sleep_time = min(10, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time


def _seconds_until(moment: datetime) -> float:
    now = datetime.now(moment.tzinfo) if moment.tzinfo else datetime.now()
    return (moment - now).total_seconds()


def next_check_delay(
    check_interval: float, do_mode: str, maintenance_entry: Optional[dict], in_maintenance_window: bool
) -> float:
    """
    Seconds to wait until the next update check.

    Outside the maintenance window the delay is capped at the start of the next
    window. Otherwise a window shorter than check_interval could fall entirely
    between two checks and updates would never be applied. In mode "nothing"
    there is nothing to check outside the window, so the daemon sleeps until
    the window opens, however far away that is.
    """
    if not maintenance_entry or in_maintenance_window:
        return check_interval

    next_start = get_next_maintenance_window_start(maintenance_entry)
    if next_start is None:
        logger.warning("Could not determine next maintenance window, using check_interval")
        return check_interval

    # +1s so the check lands inside the window rather than right at its edge
    until_window = _seconds_until(next_start) + 1
    if until_window <= 1:
        logger.warning("Next maintenance window start is in the past, using check_interval")
        return check_interval

    if do_mode == "nothing" or until_window < check_interval:
        logger.info(
            f"Next maintenance window starts at {next_start.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"({until_window:.0f} seconds)"
        )
        return until_window
    return check_interval


def _running_version() -> Optional[str]:
    os_release = read_booted_os_release()
    return os_release.version_id if os_release else None


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
        max_releases = config.get("max_releases", 5)
        releases = fetch_releases(owner, repo, include_prereleases=include_prereleases, limit=max_releases)
        if not releases:
            logger.debug("Could not fetch releases from GitHub")
            return None
        
        # Find applicable batch update
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


def sync_and_apply_update(update_file_path: Path, config: dict, update_url: Optional[str] = None) -> bool:
    """
    Sync root partition and apply update from file.
    
    Syncs root partition to inactive partition, applies update, notifies users,
    and initiates tryboot reboot after countdown.
    
    Args:
        update_file_path: Path to downloaded update file
        config: Configuration dictionary
        update_url: URL the update came from; used to remember attempts and
            cancellations so the same update is not retried endlessly
        
    Returns:
        True if update applied and reboot initiated, False on error or cancellation
    """
    try:
        # Step 1: Sync root partition to inactive partition
        logger.info("Preparing inactive partition...")
        notify_users("Preparing system update - syncing partitions...")
        
        rsync_timeout = config.get("rsync_timeout", DEFAULT_RSYNC_TIMEOUT_SEC)
        syncroot_result = execute_syncroot(rsync_timeout_sec=rsync_timeout)
        if syncroot_result != 0:
            logger.error("Failed to sync root partition - aborting update")
            notify_users("Update failed: could not sync partitions")
            return False
        
        logger.info("Partition sync complete")
        
        # Step 2: Apply the update
        logger.info(f"Applying update from {update_file_path}...")
        notify_users(f"Applying update: {update_file_path.name}")
        
        # Use file path directly (execute_apply will handle it)
        apply_result = execute_apply(
            str(update_file_path),
            keep_download=False,
            rsync_timeout_sec=rsync_timeout,
        )
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
            if update_url:
                # Remember the cancellation: the daemon stops on this signal and systemd
                # restarts it, which would otherwise just apply the same update again.
                update_state.block(update_url, _running_version())
                logger.info(
                    f"This update will not be applied again automatically; "
                    f"remove {update_state.STATE_FILE} to allow it"
                )
            notify_users("Update cancelled - system will not reboot. Update remains on inactive partition.")
            return False
        
        # Step 5: Execute tryboot (which will reboot the system)
        logger.info("Initiating tryboot reboot...")
        notify_users("Rebooting to activate update (via tryboot)...")
        
        # Record the attempt *before* rebooting: if the new partition fails to boot, the
        # firmware falls back to this one and the daemon would find the same update again.
        if update_url:
            attempts = update_state.record_attempt(update_url, _running_version())
            logger.info(f"Update attempt {attempts} of {update_state.MAX_UPDATE_ATTEMPTS}")
        
        tryboot_result = execute_tryboot(reboot=True)
        if tryboot_result != 0:
            update_state.discard_attempt()
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
        return sync_and_apply_update(update_file_path, config, update_url)
        
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
                
                delay = next_check_delay(check_interval, do_mode, maintenance_entry, in_maintenance_window)
                logger.info(f"Next update check in {delay:.0f} seconds")
                sleep_interruptible(delay)
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
                    
                    if update_state.is_blocked(update_url, _running_version()):
                        logger.warning(
                            f"Not applying {update_url}: it was already attempted "
                            f"{update_state.MAX_UPDATE_ATTEMPTS} times from this version without success "
                            f"(failed to boot, or cancelled). Waiting for a newer update; "
                            f"remove {update_state.STATE_FILE} to retry."
                        )
                        success = False
                    else:
                        success = apply_update_workflow(update_url, config)
                    if success:
                        logger.info("Tryboot reboot initiated - daemon exiting")
                        sys.exit(0)
                    else:
                        logger.info("Update not applied - continuing normal operation")
                # else: nothing mode already handled above
            else:
                logger.info("No updates available")
            
            # Sleep until next check
            delay = next_check_delay(check_interval, do_mode, maintenance_entry, in_maintenance_window)
            logger.info(f"Next update check in {delay:.0f} seconds")
            sleep_interruptible(delay)
            
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
    finalize_boot(config.get("persist_timeout", 600))
    
    # Enter main loop
    try:
        main_loop(config, maintenance_entry=maintenance_entry)
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard - shutting down")
    except Exception as e:
        logger.error(f"Unexpected error in daemon: {e}", exc_info=True)
        sys.exit(1)
    
    logger.info("Daemon shutdown complete")

