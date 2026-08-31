"""argparse entry point, one-shot and loop-mode driver."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from typing import List, Optional

from . import __version__
from .aws import DuplicateInstanceError, fetch_instances
from .config import Config, ConfigError, load_config
from .runner import Runner, default_runner
from .sync import apply_plan, build_plan, log_plan
from .termix import fetch_termix_hosts

log = logging.getLogger("termix-aws-sync")

EXIT_OK = 0
EXIT_OPERATION_FAILURE = 1
EXIT_CONFIG_ERROR = 2


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="termix-aws-sync")
    parser.add_argument("--config", help="path to config TOML")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="loop mode: sync every SECONDS (default: one-shot)",
    )
    return parser.parse_args(argv)


def run_cycle(config: Config, dry_run: bool, runner: Runner = default_runner) -> int:
    """Run one sync cycle. Returns a process exit code (0/1/2)."""
    try:
        desired = fetch_instances(config, runner)
        current = fetch_termix_hosts(config, runner)
    except DuplicateInstanceError as exc:
        log.error(str(exc))
        return EXIT_CONFIG_ERROR
    except RuntimeError as exc:
        log.error("failed to fetch state (check AWS/Termix auth and config): %s", exc)
        return EXIT_CONFIG_ERROR

    log.info(
        "aws: %d running instance(s); termix: %d managed host(s)",
        len(desired),
        len(current),
    )

    plan = build_plan(desired, current)
    if plan.is_empty:
        log.info("in sync; nothing to do")
        return EXIT_OK

    log_plan(plan, desired, current)
    if dry_run:
        return EXIT_OK

    failures = apply_plan(plan, desired, current, runner)
    if failures:
        log.error("%d operation(s) failed", failures)
        return EXIT_OPERATION_FAILURE
    log.info("sync complete")
    return EXIT_OK


def _configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def resolve_interval(args: argparse.Namespace) -> Optional[int]:
    """Resolve the loop interval: --interval > SYNC_INTERVAL env.

    --dry-run always means "print the plan once, right now": SYNC_INTERVAL is
    a Docker/daemon convenience and must not turn an explicit --dry-run
    invocation (e.g. `docker compose run --rm sync --dry-run`) into a loop.
    """
    if args.interval is not None:
        return args.interval
    if args.dry_run:
        return None
    env_interval = os.environ.get("SYNC_INTERVAL")
    if not env_interval:
        return None
    try:
        return int(env_interval)
    except ValueError as exc:
        raise ConfigError("SYNC_INTERVAL must be an integer number of seconds") from exc


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.debug)

    try:
        config = load_config(args.config)
        interval = resolve_interval(args)
    except ConfigError as exc:
        log.error(str(exc))
        return EXIT_CONFIG_ERROR

    if not config.termix_api_key:
        log.error("TERMIX_API_KEY environment variable is required")
        return EXIT_CONFIG_ERROR

    if not interval:
        return run_cycle(config, args.dry_run)

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("received signal %d; stopping after this cycle", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("starting loop mode: interval=%ds", interval)
    while not stop_event.is_set():
        exit_code = run_cycle(config, args.dry_run)
        if exit_code != EXIT_OK:
            log.error("cycle failed (exit code %d); continuing loop", exit_code)
        stop_event.wait(interval)

    log.info("stopped")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
