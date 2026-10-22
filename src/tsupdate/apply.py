"""Apply update tool for applying pidiff updates to inactive partition."""

import logging
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tsupdate.errors import UpdateError, timeout_error
from tsupdate.status import read_os_release
from tsupdate.syncroot import (
    can_run_syncroot,
    get_inactive_partition_device,
    mount_context,
    ROOT_UP,
)
from tsupdate.utils import ARTIFACTS_DIR, ensure_file, resolve_rsync_timeout_seconds, safe_cleanup

logger = logging.getLogger(__name__)

EXTRACT_DIR = ARTIFACTS_DIR / "extract"


def extract_update_archive(archive_path: Path) -> Path:
    """
    Extract tar archive to EXTRACT_DIR.

    The archive is extracted below /data rather than the default temp dir, since /tmp may be
    RAM-backed on a read-only root. EXTRACT_DIR is wiped first, so leftovers from an
    interrupted run never pile up, and it is removed again if extraction fails.
    Members are extracted with filter="data", so paths and links cannot escape the directory.

    Args:
        archive_path: Path to tar archive file

    Returns:
        Path to extracted directory

    Raises:
        UpdateError: The archive does not exist or cannot be extracted.
    """
    if not archive_path.exists():
        raise UpdateError(f"Update file does not exist: {archive_path}")

    try:
        shutil.rmtree(EXTRACT_DIR, ignore_errors=True)
        EXTRACT_DIR.mkdir(parents=True, mode=0o700)
        logger.debug("Created extraction directory: %s", EXTRACT_DIR)
    except OSError as e:
        raise UpdateError(f"Could not create extraction directory {EXTRACT_DIR}: {e}") from e

    try:
        with tarfile.open(archive_path, "r:*") as tar:
            tar.extractall(EXTRACT_DIR, filter="data")
    except (tarfile.TarError, OSError) as e:
        shutil.rmtree(EXTRACT_DIR, ignore_errors=True)
        raise UpdateError(f"Could not extract archive {archive_path}: {e}") from e
    logger.debug("Extracted archive %s to %s", archive_path, EXTRACT_DIR)
    return EXTRACT_DIR


def parse_batch_metadata(batch_sh_path: Path) -> Dict[str, str]:
    """
    Parse metadata from batch.sh file.
    
    Args:
        batch_sh_path: Path to batch.sh file
        
    Returns:
        Dictionary of metadata variables

    Raises:
        UpdateError: batch.sh does not exist or cannot be read.
    """
    if not batch_sh_path.exists():
        raise UpdateError(f"batch.sh not found: {batch_sh_path}")
    
    metadata = {}
    
    try:
        with open(batch_sh_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines, comments, and non-variable lines
                if not line or line.startswith("#") or line.startswith("echo") or line.startswith("if") or line.startswith("set"):
                    continue
                
                # Parse KEY="VALUE" or KEY=VALUE format
                # Match: KEY="VALUE" or KEY=VALUE
                match = re.match(r'^([A-Z_][A-Z0-9_]*)=(.+)$', line)
                if match:
                    key = match.group(1)
                    value = match.group(2).strip()
                    
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    
                    metadata[key] = value
        
        logger.debug("Parsed metadata from batch.sh: %s", metadata)
        return metadata
    except (OSError, IOError, UnicodeDecodeError) as e:
        raise UpdateError(f"Could not parse batch.sh: {e}") from e


def extract_rsync_command_from_batch_sh(batch_sh_path: Path) -> Tuple[List[str], Optional[str]]:
    """
    Extract rsync command options and filter rules from batch.sh file.
    
    Args:
        batch_sh_path: Path to batch.sh file
        
    Returns:
        Tuple of (rsync_options_list, filter_rules_string)

    Raises:
        UpdateError: batch.sh does not exist, cannot be read or has no rsync command.
    """
    if not batch_sh_path.exists():
        raise UpdateError(f"batch.sh not found: {batch_sh_path}")
    
    try:
        with open(batch_sh_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # Find the rsync command line (contains --read-batch)
        rsync_line_idx = None
        for i, line in enumerate(lines):
            if "--read-batch" in line and "rsync" in line:
                rsync_line_idx = i
                break
        
        if rsync_line_idx is None:
            raise UpdateError("Could not find rsync command in batch.sh")
        
        rsync_line = lines[rsync_line_idx].strip()
        logger.debug("Found rsync command line: %s", rsync_line)
        
        # Extract heredoc marker
        heredoc_match = re.search(r'<<[\'"]?([^\'"\s]+)[\'"]?', rsync_line)
        if not heredoc_match:
            raise UpdateError("Could not find heredoc marker in the rsync command of batch.sh")
        
        heredoc_marker = heredoc_match.group(1)
        logger.debug("Found heredoc marker: %s", heredoc_marker)
        
        # Extract filter rules from heredoc
        filter_rules = []
        for i in range(rsync_line_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            
            if stripped == heredoc_marker:
                break
            
            filter_rules.append(line.rstrip('\n\r'))
        
        filter_rules_str = '\n'.join(filter_rules) + '\n' if filter_rules else None
        
        # Parse rsync command line to extract options
        # Remove heredoc part and variable references
        # Example: rsync --filter=._- --recursive ... --read-batch="..." ${TARGET_DIR} <<'#E#'
        # We need to extract all options except --read-batch (we'll replace that) and ${TARGET_DIR}
        
        # Remove heredoc part (everything from << onwards)
        rsync_cmd_part = rsync_line.split('<<')[0].strip()
        
        # Remove ${TARGET_DIR} or similar variable references at the end
        rsync_cmd_part = re.sub(r'\$\{[^}]+\}\s*$', '', rsync_cmd_part).strip()
        
        # Use shlex to properly parse the command line (handles quotes correctly)
        try:
            rsync_parts = shlex.split(rsync_cmd_part)
        except ValueError:
            # Fallback to simple split if shlex fails
            rsync_parts = rsync_cmd_part.split()
        
        # Remove 'rsync' command itself if present
        if rsync_parts and rsync_parts[0] == 'rsync':
            rsync_parts = rsync_parts[1:]
        
        # Filter out --read-batch option (we'll add our own with correct batch file path)
        rsync_options = [opt for opt in rsync_parts if not opt.startswith('--read-batch')]
        
        logger.debug("Extracted rsync options: %s", rsync_options)
        logger.debug("Extracted filter rules:\n%s", filter_rules_str)
        
        return (rsync_options, filter_rules_str)
        
    except (OSError, IOError, UnicodeDecodeError) as e:
        raise UpdateError(f"Could not extract rsync command from batch.sh: {e}") from e


def find_rsync_batch_file(extracted_dir: Path, update_filename: str) -> Optional[Path]:
    """
    Find rsync batch file in extracted archive.
    
    Args:
        extracted_dir: Path to extracted archive directory
        update_filename: Base filename of update archive (without extension)
        
    Returns:
        Path to rsync batch file, or None if not found
    """
    # Remove .tar, .tar.gz, .tgz extensions if present
    base_name = update_filename
    for ext in [".tar.gz", ".tgz", ".tar"]:
        if base_name.endswith(ext):
            base_name = base_name[:-len(ext)]
            break
    
    # Look for batch file matching the update filename pattern
    # The batch file name is typically the same as the update filename
    batch_file = extracted_dir / base_name
    
    if batch_file.exists():
        logger.debug("Found rsync batch file: %s", batch_file)
        return batch_file
    
    # Try to find any file that might be the batch file
    # Look for files that don't end in .sh
    for item in extracted_dir.iterdir():
        if item.is_file() and not item.name.endswith(".sh"):
            logger.debug("Found potential rsync batch file: %s", item)
            return item
    
    logger.debug("Could not find rsync batch file in %s", extracted_dir)
    return None


def check_version_compatibility(base_pretty_name: str, target_os_release_path: Path) -> Tuple[bool, Optional[str]]:
    """
    Check if target partition version matches base version.
    
    Args:
        base_pretty_name: Expected PRETTY_NAME from update metadata
        target_os_release_path: Path to /etc/os-release in target partition
        
    Returns:
        Tuple of (is_compatible, found_pretty_name)
    """
    if not target_os_release_path.exists():
        logger.warning("/etc/os-release not found in target partition, skipping version check")
        return (True, None)  # Allow update if os-release doesn't exist
    
    os_release = read_os_release(target_os_release_path)
    if not os_release:
        logger.warning("Could not parse /etc/os-release, skipping version check")
        return (True, None)
    
    found_pretty_name = os_release.pretty_name
    if not found_pretty_name:
        logger.warning("PRETTY_NAME not found in /etc/os-release, skipping version check")
        return (True, None)
    
    if found_pretty_name != base_pretty_name:
        return (False, found_pretty_name)
    
    return (True, found_pretty_name)


def apply_rsync_batch(
    batch_file: Path,
    target_dir: Path,
    rsync_options: List[str],
    filter_rules: Optional[str] = None,
    rsync_timeout_sec: Optional[int] = None,
) -> int:
    """
    Apply rsync batch file to target directory.
    
    Args:
        batch_file: Path to rsync batch file
        target_dir: Target directory (mounted inactive partition)
        rsync_options: List of rsync options from batch.sh
        filter_rules: Optional filter rules to provide via stdin (only used if --filter option is present)
        rsync_timeout_sec: Wall-clock limit in seconds (None = default 3600; 0 = unlimited)
        
    Returns:
        Rsync exit code

    Raises:
        TransientError: rsync ran into its timeout (exit code 124).
        UpdateError: The batch file or target directory is missing, or rsync cannot be run.
    """
    logger.debug("Preparing to apply rsync batch file: %s", batch_file)
    logger.debug("Target directory: %s", target_dir)
    
    # Verify batch file exists
    if not batch_file.exists():
        raise UpdateError(f"Rsync batch file does not exist: {batch_file}")
    
    logger.debug("Batch file exists, size: %s bytes", batch_file.stat().st_size)
    
    # Verify target directory exists and is accessible
    if not target_dir.exists():
        raise UpdateError(f"Target directory does not exist: {target_dir}")
    
    if not target_dir.is_dir():
        raise UpdateError(f"Target path is not a directory: {target_dir}")
    
    logger.debug("Target directory exists and is accessible")
    
    logger.debug("Rsync options from batch.sh: %s", rsync_options)
    
    # Check if --filter option is present in rsync options
    has_filter_option = any(opt.startswith('--filter') for opt in rsync_options)
    
    # Process rsync options: add --stats if not present, handle --itemize-changes based on verbose mode
    is_verbose = logger.isEnabledFor(logging.DEBUG)
    
    # Start with base options, removing --itemize-changes if present
    processed_options = [opt for opt in rsync_options if opt != '--itemize-changes']
    
    # Add --stats if not already present
    if not any(opt == '--stats' or opt.startswith('--stats=') for opt in processed_options):
        processed_options.append('--stats')
        logger.debug("Added --stats option to rsync command")
    
    # Add --itemize-changes only in verbose mode
    if is_verbose:
        processed_options.append('--itemize-changes')
        logger.debug("Added --itemize-changes option (verbose mode)")
    
    # Build rsync command with processed options from batch.sh
    rsync_cmd = ["rsync"] + processed_options + [f"--read-batch={batch_file}", str(target_dir)]
    
    logger.debug("Executing rsync command: %s", ' '.join(rsync_cmd))
    
    # Determine if stdin input is needed
    stdin_input = None
    if has_filter_option:
        if filter_rules is None:
            logger.warning(
                "--filter option is set but no filter rules were extracted from batch.sh; "
                "providing empty stdin to prevent rsync from hanging"
            )
            stdin_input = "".encode('utf-8')
        else:
            logger.debug("Filter rules to apply:\n%s", filter_rules)
            stdin_input = filter_rules.encode('utf-8')
            logger.info("Starting rsync batch transfer with filter rules (this may take a while)")
    else:
        logger.info("Starting rsync batch transfer (this may take a while)")
    
    timeout = resolve_rsync_timeout_seconds(rsync_timeout_sec)
    run_kw = {"check": False, "timeout": timeout}
    if has_filter_option:
        run_kw["input"] = stdin_input if stdin_input is not None else b""
    else:
        run_kw["stdin"] = subprocess.DEVNULL
    
    try:
        result = subprocess.run(rsync_cmd, **run_kw)
    except subprocess.TimeoutExpired as e:
        raise timeout_error(
            e, "Rsync", "increase rsync_timeout in tsupdate.yml or pass --rsync-timeout (0 = no limit)"
        ) from e
    except FileNotFoundError as e:
        raise UpdateError("Command not found: rsync") from e
    except OSError as e:
        raise UpdateError(f"Cannot run rsync: {e}") from e
    logger.debug("Rsync command completed with exit code: %s", result.returncode)
    return result.returncode


def handle_rsync_exit_code(exit_code: int) -> str:
    """
    Handle and interpret rsync exit codes.
    
    Args:
        exit_code: Rsync exit code
        
    Returns:
        User-friendly message
    """
    messages = {
        0: "Update completed successfully!",
        1: "Error: Syntax or usage error",
        2: "Error: Protocol incompatibility",
        11: "Error: File I/O error",
        23: "Error: Partial transfer or verification failure (inactive tree may not match update base; re-run syncroot)",
        24: "Error: Partial transfer due to vanished source files",
        124: "Error: rsync exceeded time limit",
    }
    
    return messages.get(exit_code, f"Error: Update failed with exit code {exit_code}")


def execute_apply(
    update_source: str,
    keep_download: bool = False,
    rsync_timeout_sec: Optional[int] = None,
) -> int:
    """
    Execute the apply update process.
    
    Reads GH_TOKEN from environment variable for GitHub authentication.
    
    Args:
        update_source: URL or local file path to update tar archive
        keep_download: If True, keep downloaded file after apply
        rsync_timeout_sec: Wall-clock limit for rsync in seconds (None = default 3600; 0 = unlimited)
        
    Returns:
        0 (success)

    Raises:
        UpdateError: The update cannot be applied now, cannot be downloaded (see
            ensure_file()), is invalid or does not fit the inactive partition, or rsync failed.
            A failed rsync carries rsync's exit code as exit_code.
    """
    logger.debug("Starting apply process for update source: %s", update_source)
    
    # Check if apply can be run
    if not can_run_syncroot():
        raise UpdateError("The apply command can only be run when booted regularly or when tryboot is persisted")
    
    # Get inactive partition device
    device = get_inactive_partition_device()
    if not device:
        raise UpdateError("Could not determine inactive partition")
    logger.debug("Inactive partition device: %s", device)
    
    # Get partition info for user feedback
    from tsupdate.status import get_inactive_partition
    inactive = get_inactive_partition()
    if inactive:
        _, partition_num, label = inactive
        logger.info("Inactive partition: %s (p%s)", label, partition_num)
    
    # Ensure file is available (download from URL if needed)
    update_file_path, is_local_file = ensure_file(update_source, ARTIFACTS_DIR)
    logger.debug("Update file path: %s", update_file_path.resolve())
    
    extracted_dir = None
    try:
        # Extract update archive
        logger.debug("Extracting update archive: %s", update_file_path)
        extracted_dir = extract_update_archive(update_file_path)
        logger.debug("Update archive extracted to: %s", extracted_dir)
        
        # Find batch.sh file
        batch_sh = extracted_dir / "batch.sh"
        if not batch_sh.exists():
            raise UpdateError("batch.sh not found in update archive")
        
        # Parse metadata
        metadata = parse_batch_metadata(batch_sh)
        base_pretty_name = metadata.get("BASE_PRETTY_NAME")
        updated_pretty_name = metadata.get("UPDATED_PRETTY_NAME")
        base_image = metadata.get("BASE_IMAGE", "unknown")
        updated_image = metadata.get("UPDATED_IMAGE", "unknown")
        
        if not base_pretty_name:
            raise UpdateError("BASE_PRETTY_NAME not found in batch.sh metadata")
        
        logger.info("Base image: %s (%s)", base_image, base_pretty_name)
        logger.info("Updated image: %s (%s)", updated_image, updated_pretty_name)
        
        # Mount inactive partition and apply update
        logger.debug("Mounting %s to %s", device, ROOT_UP)
        with mount_context(device, ROOT_UP) as mount_point:
            logger.info("Mounted %s to %s", device, mount_point)
            
            # Check version compatibility
            target_os_release = mount_point / "etc" / "os-release"
            logger.debug("Checking version compatibility with: %s", target_os_release)
            is_compatible, found_pretty_name = check_version_compatibility(base_pretty_name, target_os_release)
            if not is_compatible:
                raise UpdateError(
                    f"Version mismatch: this update is designed for base version {base_pretty_name}, "
                    f"found target version {found_pretty_name}"
                )
            
            # Extract rsync command and filter rules from batch.sh
            logger.debug("Extracting rsync command and filter rules from batch.sh")
            rsync_options, filter_rules = extract_rsync_command_from_batch_sh(batch_sh)
            if not rsync_options:
                raise UpdateError("No rsync options found in batch.sh")
            
            # Find rsync batch file, by the original source filename
            logger.debug("Looking for rsync batch file in extracted directory: %s", extracted_dir)
            batch_file = find_rsync_batch_file(extracted_dir, Path(update_source).name)
            if not batch_file:
                logger.debug("Contents of extracted directory: %s", list(extracted_dir.iterdir()))
                raise UpdateError("Could not find rsync batch file in update archive")
            
            logger.info("Found rsync batch file: %s", batch_file)
            
            logger.info("Applying update to %s, this will modify files in the target directory", mount_point)
            exit_code = apply_rsync_batch(
                batch_file,
                mount_point,
                rsync_options,
                filter_rules,
                rsync_timeout_sec=rsync_timeout_sec,
            )
            logger.debug("apply_rsync_batch returned with exit code: %s", exit_code)
            
            message = handle_rsync_exit_code(exit_code)
            if exit_code != 0:
                message = message.removeprefix("Error: ")
                if exit_code in (23, 24):
                    message += (
                        "; if PRETTY_NAME matched but rsync still failed, the inactive partition "
                        "is probably not a byte-accurate clone of the running root; run "
                        "`tsupdate syncroot` (with checksum sync) or restore the partition"
                    )
                raise UpdateError(f"Rsync failed with exit code {exit_code}: {message}", exit_code=exit_code)
            logger.info(message)
    finally:
        # Cleanup temporary directory
        if extracted_dir:
            try:
                shutil.rmtree(extracted_dir)
                logger.debug("Removed temporary directory: %s", extracted_dir)
            except OSError as e:
                logger.warning("Could not remove temporary directory %s: %s", extracted_dir, e)
    
    # The update is applied: the download is no longer needed. It is kept after a failure, so a
    # retry doesn't have to download it again. A file the user passed in is never deleted.
    if not is_local_file and not keep_download:
        logger.debug("Removing downloaded update %s", update_file_path)
        safe_cleanup(update_file_path)
    
    return 0
