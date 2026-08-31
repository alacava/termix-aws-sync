"""Termix side: list/create/update/delete hosts via the `termix` CLI.

Known unknown (see BUILD_BRIEF.md §3): the exact JSON shape emitted by
`termix hosts list --json` is assumed. Two assumptions are isolated here:

  * The top-level response is a bare array. `_extract_host_list()` also
    tolerates it being wrapped in an object under a `hosts`/`data`/`items`/
    `results` key, since real output has been observed doing this.
  * Each host object uses field names `id`, `name`, `ip`, `port`,
    `username`, `tags`, and `folder`. That assumption is isolated to
    `fetch_termix_hosts()` below and to `sync.drifted()`, which reads the
    same fields off the dicts this function returns.

If real-world output differs further, update `_extract_host_list()` (shape)
or the `.get(...)` calls in `fetch_termix_hosts()`/`sync.drifted()` (field
names) -- run with `--debug` to see the raw response and compare. See
README.md's troubleshooting section.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .aws import DesiredHost
from .config import Config
from .runner import Runner, default_runner

ID_TAG_RE = re.compile(r"^aws-id-(i-[0-9a-f]+)$")

log = logging.getLogger("termix-aws-sync")

# If `termix hosts list --json` wraps the array in an object instead of
# returning it bare, try these keys (in order) to find the actual list --
# part of the same known-unknown as the per-host field names above.
_LIST_WRAPPER_KEYS = ("hosts", "data", "items", "results")


def _extract_host_list(payload: Any, where: str) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_WRAPPER_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        raise RuntimeError(
            f"{where} returned a JSON object with no recognized list field "
            f"(tried {', '.join(_LIST_WRAPPER_KEYS)}); run with --debug to see "
            "the raw response and see README.md's troubleshooting section"
        )
    raise RuntimeError(f"{where} returned unexpected JSON: a bare {type(payload).__name__}")


def _termix_bin() -> str:
    """Resolved lazily (not at import time) so tests can point PATH at fakes."""
    return shutil.which("termix") or "termix"


@dataclass(frozen=True)
class TermixHost:
    """A managed Termix host, as reported by `termix hosts list --json`."""

    id: Any
    instance_id: str
    name: Optional[str]
    ip: Optional[str]
    port: Optional[int]
    username: Optional[str]
    tags: List[str]
    folder: Optional[str]
    raw: Dict[str, Any]


def fetch_termix_hosts(
    config: Config, runner: Runner = default_runner
) -> Dict[str, TermixHost]:
    """Return {instance_id: TermixHost} for hosts carrying the managed tag."""
    cmd = [_termix_bin(), "hosts", "list", "--tag", config.managed_tag, "--json"]
    payload = runner(cmd, parse_json=True)
    hosts = _extract_host_list(payload, " ".join(cmd))

    managed: Dict[str, TermixHost] = {}
    for h in hosts:
        if not isinstance(h, dict):
            log.warning(
                "skipping unexpected non-object entry in termix hosts list output: %r", h
            )
            continue
        tags = h.get("tags") or []
        instance_id = None
        for tag in tags:
            m = ID_TAG_RE.match(tag)
            if m:
                instance_id = m.group(1)
                break
        if instance_id is None:
            log.warning(
                "host %s has %s tag but no aws-id-* tag; ignoring",
                h.get("id"),
                config.managed_tag,
            )
            continue
        managed[instance_id] = TermixHost(
            id=h.get("id"),
            instance_id=instance_id,
            name=h.get("name"),
            ip=h.get("ip"),
            port=h.get("port"),
            username=h.get("username"),
            tags=list(tags),
            folder=h.get("folder"),
            raw=h,
        )
    return managed


def auth_args(spec: DesiredHost) -> List[str]:
    if spec.credential_id is not None:
        return ["--credential-id", str(spec.credential_id)]
    if spec.key_file:
        return ["--key-file", spec.key_file]
    return []


def create_host(spec: DesiredHost, runner: Runner = default_runner) -> None:
    cmd = [
        _termix_bin(),
        "hosts",
        "create",
        "--name",
        spec.name,
        "--ip",
        spec.ip,
        "--port",
        str(spec.port),
        "--username",
        spec.username,
        "--folder",
        spec.folder,
        "--tags",
        ",".join(spec.tags),
        "--enable-terminal",
    ]
    cmd += auth_args(spec)
    runner(cmd)
    log.info("created %s (%s) in %s", spec.name, spec.ip, spec.folder)


def update_host(host_id: Any, spec: DesiredHost, runner: Runner = default_runner) -> None:
    cmd = [
        _termix_bin(),
        "hosts",
        "update",
        str(host_id),
        "--name",
        spec.name,
        "--ip",
        spec.ip,
        "--port",
        str(spec.port),
        "--username",
        spec.username,
        "--folder",
        spec.folder,
        "--tags",
        ",".join(spec.tags),
    ]
    runner(cmd)
    log.info("updated %s (%s) in %s", spec.name, spec.ip, spec.folder)


def delete_host(host: TermixHost, runner: Runner = default_runner) -> None:
    runner([_termix_bin(), "hosts", "delete", str(host.id)])
    log.info("deleted %s (id %s)", host.name, host.id)
