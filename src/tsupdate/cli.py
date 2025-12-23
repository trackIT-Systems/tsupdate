"""Command-line interface for tsupdate."""

import argparse
import sys
from tsupdate import __version__, configure_logging, is_root
from pathlib import Path

from tsupdate.status import get_system_status, format_status_text, format_status_json
from tsupdate.tryboot import execute_tryboot, execute_persist, rollback_tryboot
from tsupdate.syncroot import execute_syncroot, execute_mount, execute_unmount
from tsupdate.apply import execute_apply
from tsupdate.restore import execute_restore


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
            "  - Adds cmdline=tryline.txt entry to tryboot.txt\n\n"
            "The system must be booted regularly (not via tryboot) to use this command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_tryboot.add_argument(
        "--reboot",
        "-r",
        action="store_true",
        help="Automatically reboot the system after configuring tryboot",
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
            "  - Optionally reboots to complete the rollback\n\n"
            "Used when booted via tryboot but something went wrong and you want to go back.\n"
            "The system must be booted via tryboot to use this command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_rollback.add_argument(
        "--reboot",
        "-r",
        action="store_true",
        help="Automatically reboot the system after rollback",
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
        type=Path,
        help="Path to update tar archive file",
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
    
    # Parse arguments
    args = parser.parse_args()
    
    # Configure logging based on verbose flag
    configure_logging(verbose=args.verbose)
    
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
        exit_code = execute_tryboot(reboot=args.reboot)
        sys.exit(exit_code)
    
    # Handle persist command
    if args.command == "persist":
        exit_code = execute_persist(reboot=args.reboot)
        sys.exit(exit_code)
    
    # Handle rollback command
    if args.command == "rollback":
        exit_code = rollback_tryboot(reboot=args.reboot)
        sys.exit(exit_code)
    
    # Handle syncroot command
    if args.command == "syncroot":
        exit_code = execute_syncroot()
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
        exit_code = execute_apply(args.update_file)
        sys.exit(exit_code)
    
    # Handle restore command
    if args.command == "restore":
        exit_code = execute_restore(
            image_source=args.image_source,
            partition=args.partition,
            keep_image=args.keep_image,
        )
        sys.exit(exit_code)
    
    # This should not be reached if argparse is working correctly
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()

