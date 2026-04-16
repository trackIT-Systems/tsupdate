"""Apply update tool for applying pidiff updates to inactive partition."""

import logging
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tsupdate.status import read_os_release
from tsupdate.syncroot import (
    can_run_syncroot,
    get_inactive_partition_device,
    mount_context,
    ROOT_UP,
)
from tsupdate.utils import ARTIFACTS_DIR, ensure_file, resolve_rsync_timeout_seconds

logger = logging.getLogger(__name__)


def extract_update_archive(archive_path: Path) -> Optional[Path]:
    """
    Extract tar archive to temporary directory.
    
    Args:
        archive_path: Path to tar archive file
        
    Returns:
        Path to extracted directory, or None on error
    """
    if not archive_path.exists():
        logger.error(f"Update file does not exist: {archive_path}")
        return None
    
    try:
        # Create temporary directory
        temp_dir = tempfile.mkdtemp(prefix="tsupdate_apply_")
        temp_path = Path(temp_dir)
        logger.debug(f"Created temporary directory: {temp_path}")
        
        # Extract tar archive
        with tarfile.open(archive_path, "r:*") as tar:
            tar.extractall(temp_path)
        logger.debug(f"Extracted archive {archive_path} to {temp_path}")
        
        return temp_path
    except tarfile.TarError as e:
        logger.error(f"Could not extract archive {archive_path}: {e}")
        return None
    except OSError as e:
        logger.error(f"Could not create temporary directory: {e}")
        return None


def parse_batch_metadata(batch_sh_path: Path) -> Optional[Dict[str, str]]:
    """
    Parse metadata from batch.sh file.
    
    Args:
        batch_sh_path: Path to batch.sh file
        
    Returns:
        Dictionary of metadata variables, or None on error
    """
    if not batch_sh_path.exists():
        logger.error(f"batch.sh not found: {batch_sh_path}")
        return None
    
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
        
        logger.debug(f"Parsed metadata from batch.sh: {metadata}")
        return metadata
    except (OSError, IOError, UnicodeDecodeError) as e:
        logger.error(f"Could not parse batch.sh: {e}")
        return None


def extract_rsync_command_from_batch_sh(batch_sh_path: Path) -> Optional[Tuple[List[str], str]]:
    """
    Extract rsync command options and filter rules from batch.sh file.
    
    Args:
        batch_sh_path: Path to batch.sh file
        
    Returns:
        Tuple of (rsync_options_list, filter_rules_string), or None on error
    """
    if not batch_sh_path.exists():
        logger.error(f"batch.sh not found: {batch_sh_path}")
        return None
    
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
            logger.warning("Could not find rsync command in batch.sh")
            return None
        
        rsync_line = lines[rsync_line_idx].strip()
        logger.debug(f"Found rsync command line: {rsync_line}")
        
        # Extract heredoc marker
        heredoc_match = re.search(r'<<[\'"]?([^\'"\s]+)[\'"]?', rsync_line)
        if not heredoc_match:
            logger.warning("Could not find heredoc marker in rsync command")
            return None
        
        heredoc_marker = heredoc_match.group(1)
        logger.debug(f"Found heredoc marker: {heredoc_marker}")
        
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
        
        logger.debug(f"Extracted rsync options: {rsync_options}")
        logger.debug(f"Extracted filter rules:\n{filter_rules_str}")
        
        return (rsync_options, filter_rules_str)
        
    except (OSError, IOError, UnicodeDecodeError) as e:
        logger.error(f"Could not extract rsync command from batch.sh: {e}")
        return None


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
        logger.debug(f"Found rsync batch file: {batch_file}")
        return batch_file
    
    # Try to find any file that might be the batch file
    # Look for files that don't end in .sh
    for item in extracted_dir.iterdir():
        if item.is_file() and not item.name.endswith(".sh"):
            logger.debug(f"Found potential rsync batch file: {item}")
            return item
    
    logger.debug(f"Could not find rsync batch file in {extracted_dir}")
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
    """
    logger.debug(f"Preparing to apply rsync batch file: {batch_file}")
    logger.debug(f"Target directory: {target_dir}")
    
    # Verify batch file exists
    if not batch_file.exists():
        logger.error(f"Rsync batch file does not exist: {batch_file}")
        return 1
    
    logger.debug(f"Batch file exists, size: {batch_file.stat().st_size} bytes")
    
    # Verify target directory exists and is accessible
    if not target_dir.exists():
        logger.error(f"Target directory does not exist: {target_dir}")
        return 1
    
    if not target_dir.is_dir():
        logger.error(f"Target path is not a directory: {target_dir}")
        return 1
    
    logger.debug(f"Target directory exists and is accessible")
    
    logger.debug(f"Rsync options from batch.sh: {rsync_options}")
    
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
    
    logger.debug(f"Executing rsync command: {' '.join(rsync_cmd)}")
    
    # Determine if stdin input is needed
    stdin_input = None
    if has_filter_option:
        if filter_rules is None:
            logger.warning("--filter option is set but no filter rules were extracted from batch.sh")
            logger.warning("Providing empty stdin to prevent rsync from hanging")
            stdin_input = "".encode('utf-8')
        else:
            logger.debug(f"Filter rules to apply:\n{filter_rules}")
            stdin_input = filter_rules.encode('utf-8')
            logger.info("Starting rsync batch transfer with filter rules (this may take a while)...")
    else:
        logger.info("Starting rsync batch transfer (this may take a while)...")
    
    timeout = resolve_rsync_timeout_seconds(rsync_timeout_sec)
    run_kw = {"check": False, "timeout": timeout}
    if has_filter_option:
        run_kw["input"] = stdin_input if stdin_input is not None else b""
    else:
        run_kw["stdin"] = subprocess.DEVNULL
    
    try:
        result = subprocess.run(rsync_cmd, **run_kw)
        logger.debug(f"Rsync command completed with exit code: {result.returncode}")
        return result.returncode
    except subprocess.TimeoutExpired:
        limit = f"{int(timeout)}s" if timeout is not None else "unlimited"
        logger.error(
            "rsync timed out (limit was %s); increase rsync_timeout in tsupdate.yml "
            "or pass --rsync-timeout (0 = no limit)",
            limit,
        )
        return 124
    except FileNotFoundError:
        logger.error("rsync command not found")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error executing rsync: {e}")
        return 1


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
        Exit code (0 for success, non-zero for failure)
    """
    logger.debug(f"Starting apply process for update source: {update_source}")
    
    # Check if apply can be run
    logger.debug("Checking if apply can be run (regular boot or persisted tryboot)")
    if not can_run_syncroot():
        logger.error("apply can only be run when booted regularly or when tryboot is persisted.")
        return 1
    logger.debug("Apply can be run")
    
    # Get inactive partition device
    logger.debug("Getting inactive partition device")
    device = get_inactive_partition_device()
    if not device:
        logger.error("Could not determine inactive partition")
        return 1
    logger.debug(f"Inactive partition device: {device}")
    
    # Get partition info for user feedback
    from tsupdate.status import get_inactive_partition
    inactive = get_inactive_partition()
    if inactive:
        _, partition_num, label = inactive
        logger.info(f"Inactive partition: {label} (p{partition_num})")
    
    # Ensure file is available (download from URL if needed)
    artifacts_path = ARTIFACTS_DIR
    update_file_path = None
    is_local_file = False
    extracted_dir = None
    temp_dir_cleanup = True
    
    try:
        update_file_path, is_local_file = ensure_file(update_source, artifacts_path)
        if update_file_path is None:
            logger.error("Failed to obtain update file")
            return 1
        
        logger.debug(f"Update file path: {update_file_path}")
        logger.debug(f"Update file absolute path: {update_file_path.resolve()}")
        
        # Extract update archive
        logger.debug(f"Extracting update archive: {update_file_path}")
        extracted_dir = extract_update_archive(update_file_path)
        if not extracted_dir:
            logger.error("Failed to extract update archive")
            return 1
        logger.debug(f"Update archive extracted to: {extracted_dir}")
        
        # Find batch.sh file
        batch_sh = extracted_dir / "batch.sh"
        if not batch_sh.exists():
            logger.error("batch.sh not found in update archive")
            return 1
        
        # Parse metadata
        metadata = parse_batch_metadata(batch_sh)
        if not metadata:
            return 1
        
        base_pretty_name = metadata.get("BASE_PRETTY_NAME")
        updated_pretty_name = metadata.get("UPDATED_PRETTY_NAME")
        base_image = metadata.get("BASE_IMAGE", "unknown")
        updated_image = metadata.get("UPDATED_IMAGE", "unknown")
        
        if not base_pretty_name:
            logger.error("BASE_PRETTY_NAME not found in batch.sh metadata")
            return 1
        
        logger.info(f"Base image: {base_image} ({base_pretty_name})")
        logger.info(f"Updated image: {updated_image} ({updated_pretty_name})")
        
        # Mount inactive partition and apply update
        logger.debug(f"Mounting {device} to {ROOT_UP}")
        try:
            with mount_context(device, ROOT_UP) as mount_point:
                logger.info(f"Mounted {device} to {mount_point}")
                logger.debug(f"Verifying mount point: {mount_point}")
                logger.debug(f"Mount point exists: {mount_point.exists()}")
                logger.debug(f"Mount point is directory: {mount_point.is_dir()}")
                
                # Check version compatibility
                target_os_release = mount_point / "etc" / "os-release"
                logger.debug(f"Checking version compatibility with: {target_os_release}")
                is_compatible, found_pretty_name = check_version_compatibility(base_pretty_name, target_os_release)
                logger.debug(f"Version compatibility check result: compatible={is_compatible}, found_pretty_name={found_pretty_name}")
                
                if not is_compatible:
                    logger.error("Version mismatch")
                    logger.error(f"  Expected base version: {base_pretty_name}")
                    logger.error(f"  Found target version: {found_pretty_name}")
                    logger.error(f"  This update is designed for base version {base_pretty_name}")
                    return 1
                
                # Extract rsync command and filter rules from batch.sh
                logger.debug("Extracting rsync command and filter rules from batch.sh")
                rsync_config = extract_rsync_command_from_batch_sh(batch_sh)
                if rsync_config is None:
                    logger.error("Could not extract rsync command from batch.sh")
                    return 1
                
                rsync_options, filter_rules = rsync_config
                
                if not rsync_options:
                    logger.error("No rsync options found in batch.sh")
                    return 1
                
                # Find rsync batch file
                logger.debug(f"Looking for rsync batch file in extracted directory: {extracted_dir}")
                # Use original source filename for finding batch file
                source_filename = Path(update_source).name
                batch_file = find_rsync_batch_file(extracted_dir, source_filename)
                if not batch_file:
                    logger.error("Could not find rsync batch file in update archive")
                    logger.debug(f"Contents of extracted directory: {list(extracted_dir.iterdir())}")
                    return 1
                
                logger.info(f"Found rsync batch file: {batch_file}")
                logger.debug(f"Batch file absolute path: {batch_file.resolve()}")
                
                # Verify target mount point
                logger.debug(f"Verifying target mount point: {mount_point}")
                if not mount_point.exists():
                    logger.error(f"Target mount point does not exist: {mount_point}")
                    return 1
                
                logger.debug(f"Target mount point exists: {mount_point}")
                
                logger.info(f"Applying update to: {mount_point}")
                logger.info("This will modify files in the target directory.")
                logger.debug(f"About to call apply_rsync_batch with batch_file={batch_file}, target_dir={mount_point}")
                
                # Apply rsync batch
                exit_code = apply_rsync_batch(
                    batch_file,
                    mount_point,
                    rsync_options,
                    filter_rules,
                    rsync_timeout_sec=rsync_timeout_sec,
                )
                
                logger.debug(f"apply_rsync_batch returned with exit code: {exit_code}")
                
                message = handle_rsync_exit_code(exit_code)
                if exit_code == 0:
                    logger.info(message)
                else:
                    logger.error(message)
                    if exit_code in (23, 24):
                        logger.error(
                            "If PRETTY_NAME matched but rsync still failed, the inactive partition "
                            "is probably not a byte-accurate clone of the running root; run "
                            "`tsupdate syncroot` (with checksum sync) or restore the partition."
                        )
                    return exit_code
        except RuntimeError as e:
            logger.error(str(e))
            return 1
    finally:
        # Cleanup temporary directory
        if temp_dir_cleanup and extracted_dir:
            try:
                shutil.rmtree(extracted_dir)
                logger.debug(f"Removed temporary directory: {extracted_dir}")
            except OSError as e:
                logger.warning(f"Could not remove temporary directory {extracted_dir}: {e}")
    
    return 0

