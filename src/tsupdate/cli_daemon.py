"""Command-line entry point for tsupdated daemon."""

import argparse
import logging
import sys
from pathlib import Path

from tsupdate import __version__
from tsupdate.daemon import run_daemon

logger = logging.getLogger(__name__)


def main() -> None:
    """Main entry point for tsupdated daemon."""
    parser = argparse.ArgumentParser(
        prog="tsupdated",
        description=(
            "Automatic update daemon for tsOS-based Raspberry Pi devices.\n\n"
            "The daemon runs continuously, checking for updates periodically and applying them "
            "automatically using the tryboot mechanism for safe rollback."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=Path("/boot/firmware/tsupdate.yml"),
        metavar="PATH",
        help="Path to YAML configuration file (default: /boot/firmware/tsupdate.yml)",
    )
    
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    
    parser.add_argument(
        "--schedule",
        "-s",
        type=Path,
        default=Path("/boot/firmware/schedule.yml"),
        metavar="PATH",
        help="Path to schedule YAML file (default: /boot/firmware/schedule.yml)",
    )
    
    # Parse arguments
    args = parser.parse_args()
    
    # Run the daemon
    try:
        run_daemon(config_path=args.config, schedule_path=args.schedule, verbose=args.verbose)
    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard")
        sys.exit(0)
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()




