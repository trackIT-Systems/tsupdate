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
from tsupdate.errors import TransientError, UpdateError
from tsupdate.github import (
    fetch_releases,
    find_applicable_batch_update,
    initialize_github_token,
    update_source,
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
        logger.error("%s must be an integer (got %s: %r)", key, type(value).__name__, value)
        sys.exit(1)
    if value < minimum:
        logger.warning("%s (%s) must be at least %s; using default %s", key, value, minimum, default)
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
        logger.warning("Configuration file not found, using default configuration: %s", config_path)
        return default_config
    
    # Load YAML file
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
        
        if user_config is None:
            logger.warning("Configuration file is empty: %s", config_path)
            return default_config
        
        if not isinstance(user_config, dict):
            logger.error("Invalid configuration format in %s: expected dictionary", config_path)
            sys.exit(1)
        
        # Merge user config with defaults
        config = default_config.copy()
        config.update(user_config)
        
        # Validate do (regular behavior)
        if "do" in user_config:
            mode = user_config.get("do")
            if mode not in valid_modes:
                logger.warning(
                    "Invalid do value: %s; valid values are: %s; using default: %s",
                    mode,
                    ', '.join(sorted(valid_modes)),
                    default_config['do'],
                )
                config["do"] = default_config["do"]
        
        # Validate maintenance_do (maintenance behavior)
        if "maintenance_do" in user_config:
            mode = user_config.get("maintenance_do")
            if mode not in valid_modes:
                logger.warning(
                    "Invalid maintenance_do value: %s; valid values are: %s; using default behavior",
                    mode,
                    ', '.join(sorted(valid_modes)),
                )
                config["maintenance_do"] = None
        
        # Validate and enforce minimum check_interval (must be integer, minimum 60 seconds)
        min_interval = 60
        check_interval = config.get("check_interval", 3600)
        if not isinstance(check_interval, int):
            logger.error(
                "check_interval must be an integer in seconds (got %s: %s)",
                type(check_interval).__name__,
                check_interval,
            )
            sys.exit(1)
        elif check_interval < min_interval:
            logger.warning(
                "check_interval (%s) is less than minimum (%ss); setting to %ss",
                check_interval,
                min_interval,
                min_interval,
            )
            config["check_interval"] = min_interval
        
        # Set maintenance_check_interval default if not specified
        if config.get("maintenance_check_interval") is None:
            config["maintenance_check_interval"] = config.get("check_interval", 3600)
        
        # Validate and enforce minimum maintenance_check_interval (must be integer, minimum 60 seconds)
        maintenance_check_interval = config.get("maintenance_check_interval", 3600)
        if not isinstance(maintenance_check_interval, int):
            logger.error(
                "maintenance_check_interval must be an integer in seconds (got %s: %s)",
                type(maintenance_check_interval).__name__,
                maintenance_check_interval,
            )
            sys.exit(1)
        elif maintenance_check_interval < min_interval:
            logger.warning(
                "maintenance_check_interval (%s) is less than minimum (%ss); setting to %ss",
                maintenance_check_interval,
                min_interval,
                min_interval,
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
        
        logger.info("Loaded configuration from %s", config_path)
        logger.debug("Configuration: %s", config)
        
        return config
        
    except yaml.YAMLError as e:
        logger.error("Failed to parse YAML configuration: %s", e)
        sys.exit(1)
    except (OSError, IOError) as e:
        logger.error("Failed to read configuration file: %s", e)
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
    logger.info("Received signal %s", signal_name)
    
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
    logger.info(
        "System booted via tryboot (uptime: %.1f seconds), will persist configuration after %s seconds of uptime "
        "to allow time to verify the update was successful",
        uptime,
        timeout,
    )
    
    # Calculate remaining wait time based on uptime
    remaining_wait = timeout - uptime
    
    if remaining_wait <= 0:
        logger.info(
            "System uptime (%.1fs) already exceeds persist timeout (%ss), persisting configuration immediately",
            uptime,
            timeout,
        )
    else:
        logger.info("Waiting %.1f seconds before persisting (until uptime reaches %ss)", remaining_wait, timeout)
        
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
                logger.debug(
                    "Persisting tryboot configuration in %.0f seconds (at uptime %.0fs)",
                    remaining,
                    uptime_at_persist,
                )
    
    # Persist the configuration
    logger.debug("Persisting tryboot boot configuration")
    try:
        persist_boot_configuration()
    except UpdateError as e:
        logger.error("Failed to persist boot configuration, manual intervention may be required: %s", e)
        return False
    final_uptime = get_system_uptime()
    logger.info("Boot configuration persisted successfully at uptime %.1fs", final_uptime)
    return True


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
    Notify users via the wall command.

    The message is logged at DEBUG only: every caller logs the event itself.
    
    Args:
        message: Message to broadcast
    """
    logger.debug("Broadcasting via wall: %s", message)
    
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
        logger.debug("Failed to broadcast via wall: %s", e)


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
    
    logger.info("Starting countdown: %s seconds until reboot", seconds)
    
    for remaining in range(seconds, 0, -1):
        # _shutdown_requested also covers a SIGTERM that arrived while the update was
        # still being applied, before this countdown started.
        if _cancel_update or _shutdown_requested:
            logger.debug("Update cancelled by signal")
            _cancel_update = False
            return False
        
        # Send reminders at specific intervals
        if remaining == 30:
            notify_users(f"WARNING: System will reboot in 30 seconds. Send SIGTERM to cancel.")
        elif remaining == 10:
            notify_users(f"WARNING: System will reboot in 10 seconds. Send SIGTERM to cancel.")
        elif remaining % 10 == 0:
            logger.debug("Countdown: %s seconds remaining", remaining)
        
        time.sleep(1)
    
    logger.debug("Countdown complete")
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

    Raises:
        ConfigurationError: The next window cannot be determined. The main loop then
            waits check_interval.
        UpdateError: The next window starts in the past.
    """
    if not maintenance_entry or in_maintenance_window:
        return check_interval

    next_start = get_next_maintenance_window_start(maintenance_entry)

    # +1s so the check lands inside the window rather than right at its edge
    until_window = _seconds_until(next_start) + 1
    if until_window <= 1:
        # Without the time itself, which changes on every check, so that the error repeats
        logger.debug("Next maintenance window start is in the past: %s", next_start)
        raise UpdateError(
            f"Next start of the maintenance window ({maintenance_entry.get('start')} - "
            f"{maintenance_entry.get('stop')}) is in the past"
        )

    if do_mode == "nothing" or until_window < check_interval:
        logger.debug(
            "Next maintenance window starts at %s (%.0f seconds)",
            next_start.strftime('%Y-%m-%d %H:%M:%S %Z'),
            until_window,
        )
        return until_window
    return check_interval


def _running_version() -> Optional[str]:
    os_release = read_booted_os_release()
    return os_release.version_id if os_release else None


def download_update(update_url: str, config: dict) -> Path:
    """
    Download update file.
    
    Downloads the update file from URL to the artifacts directory.
    
    Args:
        update_url: URL to update file
        config: Configuration dictionary
        
    Returns:
        Path to the downloaded (or already cached) file

    Raises:
        UpdateError: The file could not be downloaded (see ensure_file()).
    """
    logger.debug("Downloading update from %s", update_url)
    
    # Use ensure_file to download (or use cached file if already downloaded)
    file_path, is_local = ensure_file(update_url, ARTIFACTS_DIR)
    
    if is_local:
        logger.debug("Using existing local file: %s", file_path)
    else:
        logger.debug("Downloaded update file: %s (%s bytes)", file_path, file_path.stat().st_size)
    
    return file_path


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
        Update URL if available, None if there is no update

    Raises:
        TransientError: GitHub is unreachable, returned 5xx or is rate limited.
        ConfigurationError: The update source is missing or not allowed, the repository does
            not exist or access was denied.
        UpdateError: /etc/os-release cannot be read, or any other GitHub API error.
    """
    owner, repo, version_id = update_source(read_booted_os_release(), config.get("github_url"))
    
    include_prereleases = config.get("include_prereleases", False)
    max_releases = config.get("max_releases", 5)
    releases = fetch_releases(owner, repo, include_prereleases=include_prereleases, limit=max_releases)
    
    batch_url = find_applicable_batch_update(releases, version_id, max_releases=max_releases)
    if batch_url:
        logger.debug("Update available: %s", batch_url)
    else:
        logger.debug("No applicable batch update found for version %s", version_id)
    return batch_url


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
        True if update applied and reboot initiated, False if the update was cancelled

    Raises:
        UpdateError: Syncing, applying or configuring tryboot failed. Users are notified via
            wall before; the caller logs the error.
    """
    rsync_timeout = config.get("rsync_timeout", DEFAULT_RSYNC_TIMEOUT_SEC)
    try:
        # Step 1: Sync root partition to inactive partition
        logger.info("Preparing inactive partition")
        notify_users("Preparing system update - syncing partitions...")
        execute_syncroot(rsync_timeout_sec=rsync_timeout)
        logger.info("Partition sync complete")
        
        # Step 2: Apply the update
        logger.info("Applying update from %s", update_file_path)
        notify_users(f"Applying update: {update_file_path.name}")
        execute_apply(
            str(update_file_path),
            keep_download=False,
            rsync_timeout_sec=rsync_timeout,
        )
    except Exception as e:
        notify_users(f"Update failed: {e}")
        raise
    
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
        if update_url:
            # Remember the cancellation: the daemon stops on this signal and systemd
            # restarts it, which would otherwise just apply the same update again.
            update_state.block(update_url, _running_version())
            logger.info(
                "Update cancelled, system will not reboot; this update will not be applied again "
                "automatically, remove %s to allow it",
                update_state.STATE_FILE,
            )
        else:
            logger.info("Update cancelled, system will not reboot")
        notify_users("Update cancelled - system will not reboot. Update remains on inactive partition.")
        return False
    
    # Step 5: Execute tryboot (which will reboot the system)
    logger.info("Initiating tryboot reboot")
    notify_users("Rebooting to activate update (via tryboot)...")
    
    # Record the attempt *before* rebooting: if the new partition fails to boot, the
    # firmware falls back to this one and the daemon would find the same update again.
    if update_url:
        attempts = update_state.record_attempt(update_url, _running_version())
        logger.info("Update attempt %s of %s", attempts, update_state.MAX_UPDATE_ATTEMPTS)
    
    try:
        execute_tryboot(reboot=True)
    except Exception as e:
        update_state.discard_attempt()
        notify_users(f"Failed to configure tryboot - manual intervention required: {e}")
        raise
    
    # If we get here, reboot was initiated
    # The daemon will exit and systemd will restart it after reboot
    return True


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
        True if update applied and reboot initiated, False if the update was cancelled

    Raises:
        UpdateError: Downloading, syncing, applying or configuring tryboot failed.
    """
    if update_file_path is None:
        try:
            update_file_path = download_update(update_url, config)
        except Exception as e:
            notify_users(f"Update failed: could not download update: {e}")
            raise
    else:
        logger.info("Using already downloaded update file: %s", update_file_path)
    
    return sync_and_apply_update(update_file_path, config, update_url)


# What each stage of the main loop is called in its log messages
_STAGES = {
    "loop": "Main loop",
    "schedule": "Evaluating the maintenance window",
    "check": "Checking for updates",
    "download": "Downloading the update",
    "apply": "Applying the update",
}


class _LoopLog:
    """
    What the main loop last logged, so that a state repeated every iteration is logged once.

    Failures are tracked per stage: "schedule" (evaluating the maintenance window), "check",
    "download", "apply", and "loop" for anything else. A failure is logged when it starts and
    again only when it changes: an UpdateError without traceback, since its message says it
    all, at WARNING if it is a TransientError and at ERROR otherwise; any other exception at
    ERROR with traceback. The repeats go to DEBUG, and the recovery is logged once at INFO
    ("Checking for updates works again"). A pending update, a blocked update and the
    maintenance window are likewise logged when they change.
    """

    def __init__(self) -> None:
        self.errors: dict[str, str] = {}
        self.found: tuple[str, str] | None = None
        self.blocked: str | None = None
        self.in_window: bool | None = None

    def failed(self, stage: str, error: Exception) -> None:
        name = _STAGES[stage]
        key = f"{type(error).__name__}: {error}"
        if isinstance(error, UpdateError):
            message, exc_info = str(error), None
            level = logging.WARNING if isinstance(error, TransientError) else logging.ERROR
        else:
            message, exc_info, level = key, error, logging.ERROR
        if self.errors.get(stage) != key:
            logger.log(level, "%s failed: %s", name, message, exc_info=exc_info)
        else:
            logger.debug("%s failed again: %s", name, message, exc_info=exc_info)
        self.errors[stage] = key

    def succeeded(self, stage: str) -> None:
        if self.errors.pop(stage, None) is not None:
            logger.info("%s works again", _STAGES[stage])

    def outcome(self, stage: str, error: Exception | None) -> None:
        if error is None:
            self.succeeded(stage)
        else:
            self.failed(stage, error)

    def window(self, in_window: bool, maintenance_entry: dict) -> bool:
        """Log entering and leaving the maintenance window. Returns whether that changed."""
        changed = in_window != self.in_window
        if changed:
            start, stop = maintenance_entry.get("start"), maintenance_entry.get("stop")
            if self.in_window is None:
                where = "inside" if in_window else "outside"
                logger.info("Starting %s the maintenance window (%s - %s)", where, start, stop)
            elif in_window:
                logger.info("Entering maintenance window (%s - %s)", start, stop)
            else:
                logger.info("Leaving maintenance window (%s - %s)", start, stop)
        self.in_window = in_window
        return changed

    def found_level(self, update_url: str | None, do_mode: str) -> int:
        """INFO for an update not found before (or found in another mode), DEBUG while it stays pending."""
        found = (update_url, do_mode) if update_url else None
        level = logging.INFO if found is not None and found != self.found else logging.DEBUG
        self.found = found
        if not update_url:
            self.blocked = None
        return level

    def blocked_level(self, update_url: str) -> int:
        """WARNING the first time an update is found blocked, DEBUG while it stays blocked."""
        level = logging.WARNING if update_url != self.blocked else logging.DEBUG
        self.blocked = update_url
        return level


def _check_and_update(config: dict, do_mode: str, in_maintenance_window: bool, loop_log: _LoopLog) -> None:
    """
    One update check, and the download or apply that do_mode asks for.

    A failure of the check, download or apply is logged by loop_log and the loop carries on
    as if there was no update: it checks again after the usual delay. Exits the process
    (SystemExit) once a tryboot reboot was initiated.
    """
    logger.debug("Checking for updates")
    try:
        update_url = check_for_updates(config)
    except Exception as e:  # noqa: BLE001 - logged by loop_log; checked again after the usual delay
        loop_log.failed("check", e)
        return
    loop_log.succeeded("check")

    # An update is logged at INFO when it is found, and at DEBUG on every
    # further check while it stays pending.
    level = loop_log.found_level(update_url, do_mode)
    if not update_url:
        logger.debug("No updates available")
        return

    where = " in maintenance window" if in_maintenance_window else ""
    if do_mode == "check":
        logger.log(
            level,
            "Update found%s (mode: check), deferring update until the next check: %s",
            where,
            update_url,
        )
    elif do_mode == "download":
        logger.log(level, "Update found%s (mode: download), downloading update file: %s", where, update_url)
        try:
            update_file_path = download_update(update_url, config)
        except Exception as e:  # noqa: BLE001 - logged by loop_log; retried at the next check
            loop_log.failed("download", e)
            return
        loop_log.succeeded("download")
        logger.log(
            level,
            "Update downloaded successfully to %s, it will be applied during the next maintenance "
            "window or when mode changes to 'apply'",
            update_file_path,
        )
    elif do_mode == "apply":
        logger.log(
            level,
            "Update found%s (mode: apply), proceeding with full update workflow: %s",
            where,
            update_url,
        )
        if update_state.is_blocked(update_url, _running_version()):
            logger.log(
                loop_log.blocked_level(update_url),
                "Not applying %s: it was already attempted %s times from this version without success "
                "(failed to boot, or cancelled); waiting for a newer update, remove %s to retry",
                update_url,
                update_state.MAX_UPDATE_ATTEMPTS,
                update_state.STATE_FILE,
            )
            return
        try:
            success = apply_update_workflow(update_url, config)
        except Exception as e:  # noqa: BLE001 - logged by loop_log; retried at the next check
            loop_log.failed("apply", e)
            success = False
        else:
            loop_log.succeeded("apply")
            # the workflow downloads too: a download failure logged in download mode is over
            loop_log.succeeded("download")
        if success:
            logger.info("Tryboot reboot initiated - daemon exiting")
            sys.exit(0)
        logger.debug("Update not applied - continuing normal operation")


def main_loop(config: dict, maintenance_entry: Optional[dict] = None) -> None:
    """
    Main daemon loop.
    
    Periodically checks for updates and applies them when found.
    Uses regular behavior by default, switches to maintenance behavior when inside maintenance window.
    
    This is the one place where failures of the update check, download, apply and the
    maintenance window evaluation are logged, through _LoopLog.
    
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
            "Maintenance schedule is active - Regular: check_interval=%ss, do=%s; Maintenance: check_interval=%ss, "
            "do=%s",
            regular_interval,
            regular_do,
            maintenance_interval,
            maintenance_do,
        )
    else:
        check_interval = config.get("check_interval", 3600)
        do_mode = config.get("do", "check")
        logger.info("No maintenance schedule configured - check_interval=%ss, do=%s", check_interval, do_mode)
    
    loop_log = _LoopLog()
    while not _shutdown_requested:
        try:
            # Determine if we're in maintenance window. A window that cannot be evaluated
            # counts as outside the window.
            in_maintenance_window = False
            window_changed = False
            schedule_error: UpdateError | None = None
            if maintenance_entry:
                try:
                    in_maintenance_window = is_in_maintenance_window(maintenance_entry)
                except UpdateError as e:
                    schedule_error = e
                window_changed = loop_log.window(in_maintenance_window, maintenance_entry)
            
            # Get current behavior based on maintenance window status
            check_interval, do_mode = get_current_behavior(config, maintenance_entry, in_maintenance_window)
            
            # If mode is "nothing", skip check entirely
            if do_mode == "nothing":
                if in_maintenance_window:
                    logger.log(
                        logging.WARNING if window_changed else logging.DEBUG,
                        "Mode is 'nothing' but inside maintenance window - this is unusual, skipping check",
                    )
                else:
                    logger.debug("Mode is 'nothing' - skipping check")
            else:
                _check_and_update(config, do_mode, in_maintenance_window, loop_log)
            
            # Sleep until next check; check_interval if the next window cannot be determined
            try:
                delay = next_check_delay(check_interval, do_mode, maintenance_entry, in_maintenance_window)
            except UpdateError as e:
                schedule_error = schedule_error or e
                delay = check_interval
            if maintenance_entry:
                loop_log.outcome("schedule", schedule_error)
            loop_log.succeeded("loop")
            logger.debug("Next update check in %.0f seconds", delay)
            sleep_interruptible(delay)
            
        except Exception as e:  # noqa: BLE001 - logged by loop_log; the daemon keeps running
            loop_log.failed("loop", e)
            
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
    configure_logging("tsupdated", verbose=verbose, default_level=logging.WARNING)
    
    logger.info("Starting tsupdated, the automatic update daemon, with configuration file %s", config_path)
    
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
        schedule = load_schedule(schedule_path)
        if schedule:
            maintenance_entry = find_maintenance_entry(schedule)
            if maintenance_entry:
                logger.info("Maintenance schedule loaded from %s", schedule_path)
            else:
                logger.info("No maintenance entry found in %s, updates can be applied at any time", schedule_path)
        else:
            logger.info("Schedule file %s not found or invalid, updates can be applied at any time", schedule_path)
    
    # Handle tryboot persistence on startup
    finalize_boot(config.get("persist_timeout", 600))
    
    # Enter main loop
    try:
        main_loop(config, maintenance_entry=maintenance_entry)
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard - shutting down")
    except Exception:
        logger.exception("Unexpected error in daemon")
        sys.exit(1)
    
    logger.info("Daemon shutdown complete")

