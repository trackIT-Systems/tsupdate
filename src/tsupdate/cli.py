"""Command-line interface for tsupdate."""

import argparse
import sys
from tsupdate import __version__
from tsupdate.status import get_system_status, format_status_text, format_status_json
from tsupdate.tryboot import execute_tryboot, execute_persist


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
    
    # Parse arguments
    args = parser.parse_args()
    
    # If no command is specified, print help
    if not args.command:
        parser.print_help()
        sys.exit(0)
    
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
    
    # This should not be reached if argparse is working correctly
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()

