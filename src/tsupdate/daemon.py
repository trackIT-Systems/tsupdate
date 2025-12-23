"""Background daemon for automatic system updates."""

import logging
import signal
import subprocess
import sys
import time
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
from tsupdate.status import is_tryboot_active, read_booted_os_release
from tsupdate.syncroot import execute_syncroot
from tsupdate.tryboot import execute_tryboot, persist_boot_configuration

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
    }
    
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


def apply_update_workflow(update_url: str, config: dict) -> bool:
    """
    Complete update application workflow.
    
    Downloads update, syncs root partition, applies update, notifies users,
    and initiates tryboot reboot after countdown.
    
    Args:
        update_url: URL to update file
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
        logger.info(f"Applying update from {update_url}...")
        notify_users(f"Downloading and applying update: {update_url}")
        
        apply_result = execute_apply(update_url, keep_download=False)
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


def main_loop(config: dict) -> None:
    """
    Main daemon loop.
    
    Periodically checks for updates and applies them when found.
    
    Args:
        config: Configuration dictionary
    """
    check_interval = config.get("check_interval", 3600)
    
    logger.info("Entering main update check loop")
    logger.info(f"Will check for updates every {check_interval} seconds")
    
    while not _shutdown_requested:
        try:
            logger.info("Checking for updates...")
            
            # Check for available updates
            update_url = check_for_updates(config)
            
            if update_url:
                logger.info(f"Update found: {update_url}")
                
                # Apply the update
                success = apply_update_workflow(update_url, config)
                
                if success:
                    # Reboot was initiated - daemon will exit
                    logger.info("Tryboot reboot initiated - daemon exiting")
                    sys.exit(0)
                else:
                    logger.info("Update workflow did not complete - continuing normal operation")
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


def run_daemon(config_path: Path, verbose: bool = False) -> None:
    """
    Run the update daemon.
    
    Args:
        config_path: Path to configuration file
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
    
    # Handle tryboot persistence on startup
    persist_timeout = config.get("persist_timeout", 600)
    handle_tryboot_persist(persist_timeout)
    
    # Enter main loop
    try:
        main_loop(config)
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard - shutting down")
    except Exception as e:
        logger.error(f"Unexpected error in daemon: {e}", exc_info=True)
        sys.exit(1)
    
    logger.info("Daemon shutdown complete")

