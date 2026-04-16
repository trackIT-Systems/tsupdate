"""Command-line interface for tsupdate."""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from tsupdate import __version__, configure_logging, is_root
from tsupdate.status import get_system_status, format_status_text, format_status_json, read_booted_os_release
from tsupdate.tryboot import execute_tryboot, execute_persist, rollback_tryboot
from tsupdate.syncroot import execute_syncroot, execute_mount, execute_unmount
from tsupdate.apply import execute_apply
from tsupdate.restore import execute_restore
from tsupdate.utils import ensure_file, ARTIFACTS_DIR
from tsupdate.github import parse_github_repo_url, fetch_releases, find_latest_image, find_applicable_batch_update, initialize_github_token
import logging

logger = logging.getLogger(__name__)


def execute_download(file_source: str) -> int:
    """
    Execute the download command.
    
    Downloads a file from URL or uses local file if provided.
    Reads GH_TOKEN from environment variable for GitHub authentication.
    
    Args:
        file_source: URL or local file path to download
    
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    file_path, is_local = ensure_file(file_source, ARTIFACTS_DIR)
    
    if file_path is None:
        logger.error("Failed to download file")
        return 1
    
    if is_local:
        print(f"Using local file: {file_path}")
    else:
        file_size = file_path.stat().st_size
        print(f"Downloaded file: {file_path}")
        print(f"Size: {file_size} bytes")
    
    return 0


def execute_check(include_prereleases: bool = False, max_releases: int = 5, github_url: Optional[str] = None) -> int:
    """
    Execute the check command.
    
    Checks GitHub releases for latest image and applicable batch update.
    Reads GH_TOKEN from environment variable for GitHub authentication.
    
    Args:
        include_prereleases: If True, include pre-releases in the search
        max_releases: Maximum number of recent releases to check for batch updates
        github_url: Optional GitHub repository URL (overrides SUPPORT_URL from os-release)
    
    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Read os-release (needed for VERSION_ID, and possibly SUPPORT_URL)
    os_release = read_booted_os_release()
    if not os_release:
        logger.error("Could not read /etc/os-release")
        return 1
    
    # Get GitHub URL - use provided URL or fall back to SUPPORT_URL from os-release
    if github_url:
        support_url = github_url
        logger.debug(f"Using provided GitHub URL: {support_url}")
    else:
        support_url = os_release.get("SUPPORT_URL")
        if not support_url:
            logger.error("SUPPORT_URL not found in /etc/os-release and no GitHub URL provided")
            return 1
        logger.debug(f"Using SUPPORT_URL from os-release: {support_url}")
    
    # Get VERSION_ID from os-release (needed for batch update matching)
    version_id = os_release.version_id
    if not version_id:
        logger.error("VERSION_ID not found in /etc/os-release")
        return 1
    
    logger.debug(f"VERSION_ID: {version_id}")
    
    # Parse GitHub repo URL
    repo_info = parse_github_repo_url(support_url)
    if not repo_info:
        logger.error(f"Could not parse GitHub repository URL: {support_url}")
        return 1
    
    owner, repo = repo_info
    logger.debug(f"GitHub repository: {owner}/{repo}")
    
    # Fetch releases
    releases = fetch_releases(owner, repo, include_prereleases=include_prereleases)
    if not releases:
        logger.error("Could not fetch releases from GitHub")
        return 1
    
    # Find latest image
    image_url = find_latest_image(releases)
    
    # Find applicable batch update
    batch_url = find_applicable_batch_update(releases, version_id, max_releases=max_releases)
    
    # Print results
    if image_url:
        print(f"✓ Latest Release Image:")
        print(f"  {image_url}")
    else:
        print("✗ Latest Release Image: Not found")
    
    print()
    
    if batch_url:
        print(f"✓ Next Applicable Batch Update:")
        print(f"  {batch_url}")
    else:
        print(f"✗ Next Applicable Batch Update: Not found")
        print(f"  (current version: {version_id})")
    
    return 0


def main():
    """Main entry point for tsupdate CLI."""
    parser = argparse.ArgumentParser(
        prog="tsupdate",
        description=(
            "Update daemon for tsOS-based Raspberry Pi devices.\n\n"
            "Manages A/B partition updates using pidiff files and the Raspberry Pi tryboot mechanism for safe, atomic updates with automatic rollback."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Use 'tsupdate <command> --help' for more information on a specific command.",
    )
    
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    
    subparsers = parser.add_subparsers(
        title="commands",
        dest="command",
        metavar="<command>",
    )
    
    # status command
    parser_status = subparsers.add_parser(
        "status",
        help="Show current system status",
        description=(
            "Show current system status.\n\n"
            "Displays information about:\n"
            "  - Current OS version\n"
            "  - Active/inactive partitions\n"
            "  - Boot method (tryboot or regular)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_status.add_argument(
        "--json",
        action="store_true",
        help="Output status in JSON format",
    )
    
    # tryboot command
    parser_tryboot = subparsers.add_parser(
        "tryboot",
        help="Configure tryboot to switch to alternate partition",
        description=(
            "Configure tryboot to switch to alternate partition.\n\n"
            "This command:\n"
            "  - Copies cmdline.txt to tryline.txt and switches the partition\n"
            "  - Copies config.txt to tryboot.txt\n"
            "  - Adds cmdline=tryline.txt entry to tryboot.txt\n"
            "  - Automatically reboots the system (unless --no-reboot is specified)\n\n"
            "The system must be booted regularly (not via tryboot) to use this command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_tryboot.add_argument(
        "--no-reboot",
        action="store_true",
        help="Do not reboot the system after configuring tryboot (default: reboot automatically)",
    )
    
    # persist command
    parser_persist = subparsers.add_parser(
        "persist",
        help="Persist current boot configuration",
        description=(
            "Persist the current boot configuration by copying tryline.txt to cmdline.txt.\n\n"
            "This command makes the current tryboot configuration permanent.\n"
            "The system must be booted via tryboot to use this command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_persist.add_argument(
        "--reboot",
        "-r",
        action="store_true",
        help="Automatically reboot the system after persisting configuration",
    )
    
    # rollback command
    parser_rollback = subparsers.add_parser(
        "rollback",
        help="Rollback from tryboot to previous partition",
        description=(
            "Rollback from tryboot to the previous partition.\n\n"
            "This command:\n"
            "  - Restores cmdline.txt to point to the previous partition\n"
            "  - Removes tryboot configuration files (tryboot.txt, tryline.txt)\n"
            "  - Automatically reboots the system (unless --no-reboot is specified)\n\n"
            "Used when booted via tryboot but something went wrong and you want to go back.\n"
            "The system must be booted via tryboot to use this command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_rollback.add_argument(
        "--no-reboot",
        action="store_true",
        help="Do not reboot the system after rollback (default: reboot automatically)",
    )
    
    # syncroot command
    parser_syncroot = subparsers.add_parser(
        "syncroot",
        help="Sync root partition to inactive partition",
        description=(
            "Sync the read-only root partition to the inactive partition.\n\n"
            "This command:\n"
            "  - Mounts the inactive partition to /media/root-up\n"
            "  - Syncs /media/root-ro to /media/root-up using rsync\n"
            "  - Unmounts the partition after syncing\n\n"
            "Can only be run when booted regularly or when tryboot is persisted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_syncroot.add_argument(
        "--rsync-timeout",
        type=int,
        default=None,
        metavar="SEC",
        help="Maximum wall-clock seconds for rsync (default: 3600; 0 = unlimited)",
    )
    
    # mount command
    parser_mount = subparsers.add_parser(
        "mount",
        help="Mount inactive partition to /media/root-up",
        description=(
            "Mount the inactive partition to /media/root-up.\n\n"
            "This allows manual access to the inactive partition for inspection or modification."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # unmount command
    parser_unmount = subparsers.add_parser(
        "unmount",
        help="Unmount inactive partition from /media/root-up",
        description=(
            "Unmount the inactive partition from /media/root-up.\n\n"
            "This unmounts the partition that was previously mounted with the mount command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # apply command
    parser_apply = subparsers.add_parser(
        "apply",
        help="Apply pidiff update to inactive partition",
        description=(
            "Apply a pidiff update to the inactive partition.\n\n"
            "This command:\n"
            "  - Downloads the update archive from URL (or uses local file)\n"
            "  - Extracts the update archive (tar file)\n"
            "  - Parses metadata from batch.sh\n"
            "  - Mounts the inactive partition to /media/root-up\n"
            "  - Checks version compatibility\n"
            "  - Applies the update using rsync batch file\n"
            "  - Unmounts the partition after completion\n\n"
            "Can only be run when booted regularly or when tryboot is persisted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_apply.add_argument(
        "update_file",
        type=str,
        help="URL or local file path to update tar archive file",
    )
    parser_apply.add_argument(
        "--keep-download",
        "-k",
        action="store_true",
        help="Keep downloaded file after apply",
    )
    parser_apply.add_argument(
        "--rsync-timeout",
        type=int,
        default=None,
        metavar="SEC",
        help="Maximum wall-clock seconds for rsync (default: 3600; 0 = unlimited)",
    )
    
    # restore command
    parser_restore = subparsers.add_parser(
        "restore",
        help="Restore OS image to inactive partition",
        description=(
            "Download and restore an OS image to the inactive partition.\n\n"
            "This command:\n"
            "  - Downloads the OS image from URL (or uses local file)\n"
            "  - Extracts the image if compressed (.gz, .xz, .zip)\n"
            "  - For .zip files, uses the largest .img file\n"
            "  - Mounts the rootfs partition via loopback\n"
            "  - Syncs the filesystem to the inactive partition\n"
            "  - Cleans up temporary files and mounts\n\n"
            "Can only be run when booted regularly or when tryboot is persisted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_restore.add_argument(
        "image_source",
        type=str,
        help="URL or local file path to OS image",
    )
    parser_restore.add_argument(
        "--partition",
        "-p",
        type=int,
        default=2,
        help="Partition number in image to use as rootfs (default: 2)",
    )
    parser_restore.add_argument(
        "--keep-image",
        "-k",
        action="store_true",
        help="Keep downloaded image file after restore",
    )
    
    # download command
    parser_download = subparsers.add_parser(
        "download",
        help="Download update or restore file",
        description=(
            "Download a file from URL or use local file if provided.\n\n"
            "This command:\n"
            "  - Downloads the file from URL to the artifacts directory (/data/tsupdate/)\n"
            "  - Supports resumable downloads (automatically resumes interrupted downloads)\n"
            "  - Handles GitHub release URLs with authentication (uses GH_TOKEN if set)\n"
            "  - Reuses cached files if they already exist\n"
            "  - Prints the path where the file was downloaded\n\n"
            "The file is saved in the artifacts directory and can be used later with apply or restore commands."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_download.add_argument(
        "file_source",
        type=str,
        help="URL or local file path to download",
    )
    
    # check command
    parser_check = subparsers.add_parser(
        "check",
        help="Check GitHub releases for latest image and applicable batch update",
        description=(
            "Check GitHub releases for latest image and applicable batch update.\n\n"
            "This command:\n"
            "  - Reads SUPPORT_URL from /etc/os-release\n"
            "  - Queries GitHub Releases API\n"
            "  - Finds the latest release's image file URL\n"
            "  - Finds the next applicable batch update tar file (compatible with current version)\n\n"
            "The batch update file name format is: tsOS-{variant}-arm64-update-{current_version}-to-{next_version}.tar\n\n"
            "By default, only regular releases are considered. Use --pre to include pre-releases."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_check.add_argument(
        "--pre",
        action="store_true",
        help="Include pre-releases in the search",
    )
    parser_check.add_argument(
        "--max-releases",
        type=int,
        default=5,
        metavar="N",
        help="Maximum number of recent releases to check for batch updates (default: 5)",
    )
    parser_check.add_argument(
        "--github-url",
        type=str,
        metavar="URL",
        help="GitHub repository URL (overrides SUPPORT_URL from /etc/os-release). Example: https://github.com/owner/repo",
    )
    
    # Parse arguments
    args = parser.parse_args()
    
    # Configure logging based on verbose flag
    configure_logging(verbose=args.verbose)
    
    # Initialize GitHub token once at program start
    initialize_github_token()
    
    # If no command is specified, print help
    if not args.command:
        parser.print_help()
        sys.exit(0)
    
    # Commands that require root privileges
    privileged_commands = {"syncroot", "mount", "unmount", "apply", "restore", "tryboot", "persist", "rollback"}
    
    # Check if root is required for the requested command
    if args.command in privileged_commands:
        if not is_root():
            print("ERROR: This command requires root privileges. Please run as root or use sudo.", file=sys.stderr)
            sys.exit(1)
    
    # Handle status command
    if args.command == "status":
        status = get_system_status()
        if args.json:
            print(format_status_json(status))
        else:
            print(format_status_text(status))
        sys.exit(0)
    
    # Handle tryboot command
    if args.command == "tryboot":
        exit_code = execute_tryboot(reboot=not args.no_reboot)
        sys.exit(exit_code)
    
    # Handle persist command
    if args.command == "persist":
        exit_code = execute_persist(reboot=args.reboot)
        sys.exit(exit_code)
    
    # Handle rollback command
    if args.command == "rollback":
        exit_code = rollback_tryboot(reboot=not args.no_reboot)
        sys.exit(exit_code)
    
    # Handle syncroot command
    if args.command == "syncroot":
        exit_code = execute_syncroot(rsync_timeout_sec=args.rsync_timeout)
        sys.exit(exit_code)
    
    # Handle mount command
    if args.command == "mount":
        exit_code = execute_mount()
        sys.exit(exit_code)
    
    # Handle unmount command
    if args.command == "unmount":
        exit_code = execute_unmount()
        sys.exit(exit_code)
    
    # Handle apply command
    if args.command == "apply":
        exit_code = execute_apply(
            args.update_file,
            keep_download=args.keep_download,
            rsync_timeout_sec=args.rsync_timeout,
        )
        sys.exit(exit_code)
    
    # Handle restore command
    if args.command == "restore":
        exit_code = execute_restore(
            image_source=args.image_source,
            partition=args.partition,
            keep_image=args.keep_image,
        )
        sys.exit(exit_code)
    
    # Handle download command
    if args.command == "download":
        exit_code = execute_download(args.file_source)
        sys.exit(exit_code)
    
    # Handle check command
    if args.command == "check":
        exit_code = execute_check(
            include_prereleases=args.pre,
            max_releases=args.max_releases,
            github_url=args.github_url
        )
        sys.exit(exit_code)
    
    # This should not be reached if argparse is working correctly
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()

