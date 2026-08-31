"""Subprocess execution, isolated behind an injectable callable so unit
tests can supply a fake runner instead of spawning real processes.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, List, Protocol

log = logging.getLogger("termix-aws-sync")


class Runner(Protocol):
    def __call__(self, cmd: List[str], parse_json: bool = False) -> Any: ...


def default_runner(cmd: List[str], parse_json: bool = False) -> Any:
    """Run a command; return parsed JSON or stdout. Raises RuntimeError on failure."""
    log.debug("exec: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(f"could not execute {cmd[0]!r}: {exc}") from exc
    log.debug(
        "exit=%d stdout=%.2000r stderr=%.500r", proc.returncode, proc.stdout, proc.stderr
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    if not parse_json:
        return proc.stdout
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"could not parse JSON from {' '.join(cmd)}: {exc}\noutput: {proc.stdout[:500]!r}"
        ) from exc
