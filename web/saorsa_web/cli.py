import argparse
import logging

from saorsa_web.monitor import run_monitor


def main():
    parser = argparse.ArgumentParser(description="Saorsa Testnet Registry CLI")
    subparsers = parser.add_subparsers(dest="command")

    monitor_parser = subparsers.add_parser("monitor", help="Run the status monitor")
    monitor_parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Poll interval in seconds (default: 60)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    if args.command == "monitor":
        run_monitor(interval=args.interval)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
