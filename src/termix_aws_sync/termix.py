"""Termix side: list/create/update/delete hosts via the `termix` CLI.

Known unknown (see BUILD_BRIEF.md §3): the exact JSON field names emitted by
`termix hosts list --json` are assumed to be `id`, `name`, `ip`, `port`,
`username`, `tags`, and `folder`. That assumption is isolated to
`fetch_termix_hosts()` below and to `sync.drifted()`, which reads the same
fields off the dicts this function returns. If real-world output uses
different field names, update the `.get(...)` calls in those two places --
run with `--debug` to dump the raw `termix hosts list --json` output and
compare. See README.md's troubleshooting section.
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
    hosts = runner(
        [_termix_bin(), "hosts", "list", "--tag", config.managed_tag, "--json"],
        parse_json=True,
    )
    managed: Dict[str, TermixHost] = {}
    for h in hosts:
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
