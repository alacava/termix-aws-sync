"""Termix side: list/create/update/delete hosts via the Termix REST API.

BUILD_BRIEF.md §2.2 originally fixed "CLI only, never the REST API" as a
design decision, specifically because the CLI is versioned/documented and
the API isn't. That was deliberately overridden after hitting a real
production bug: `termix hosts create/update --credential-id` fails
server-side with a Postgres NOT NULL violation on `auth_type`.

Root cause, confirmed by reading the real Termix server source
(github.com/Termix-SSH/Termix, Apache-2.0,
src/backend/database/routes/host.ts): the create/update routes compute
`effectiveAuthType = authType || authMethod || ...`, with **no
auto-derivation of authType from credentialId** (that inference only
exists in a separate helper used by the unrelated bulk-import endpoint).
The `termix` CLI apparently never sends `authType` when using
`--credential-id`, which is what crashes. `_auth_fields()` below always
sends it explicitly -- confirmed as the actual fix via a live curl test
against a real server before this module was written.

Also confirmed live: `PUT /host/db/host/{id}` is a full replace, not a
merge -- a field omitted from the request body reverts to its default
(a real host's `enableTerminal: true` flipped to `false` after a PUT that
didn't repeat it). `update_host()` therefore merges the diff onto the
host's full existing record (`TermixHost.raw`, captured at list time)
rather than PUTting a diff alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .aws import DesiredHost
from .config import Config
from .http_client import TermixClient

ID_TAG_RE = re.compile(r"^aws-id-(i-[0-9a-f]+)$")

log = logging.getLogger("termix-aws-sync")


@dataclass(frozen=True)
class TermixHost:
    """A managed Termix host, as reported by GET /host/db/host."""

    id: Any
    instance_id: str
    name: Optional[str]
    ip: Optional[str]
    port: Optional[int]
    username: Optional[str]
    tags: List[str]
    folder: Optional[str]
    raw: Dict[str, Any]


def fetch_termix_hosts(config: Config, client: TermixClient) -> Dict[str, TermixHost]:
    """Return {instance_id: TermixHost} for hosts carrying the managed tag.

    The API has no server-side tag filter (confirmed: GET /host/db/host
    returns every host visible to the user), so filtering by managed_tag
    happens here instead of via a CLI flag.
    """
    hosts = client.list_hosts()
    managed: Dict[str, TermixHost] = {}
    for h in hosts:
        if not isinstance(h, dict):
            log.warning("skipping unexpected non-object entry in host list: %r", h)
            continue

        tags = h.get("tags") or []
        if config.managed_tag not in tags:
            continue

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


def _auth_fields(spec: DesiredHost) -> Dict[str, Any]:
    """Explicit authType is the confirmed fix -- see module docstring."""
    if spec.credential_id is not None:
        return {"authType": "credential", "credentialId": spec.credential_id}
    if spec.key_file:
        with open(spec.key_file, encoding="utf-8") as fh:
            key_contents = fh.read()
        return {"authType": "key", "key": key_contents}
    raise RuntimeError(
        f"no auth method resolved for {spec.name}; this should have been "
        "caught at config load"
    )


def _host_body(spec: DesiredHost) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "name": spec.name,
        "ip": spec.ip,
        "port": spec.port,
        "username": spec.username,
        "folder": spec.folder,
        "tags": list(spec.tags),
        "enableTerminal": True,
    }
    body.update(_auth_fields(spec))
    return body


def create_host(spec: DesiredHost, client: TermixClient) -> None:
    client.create_host(_host_body(spec))
    log.info("created %s (%s) in %s", spec.name, spec.ip, spec.folder)


def update_host(host: TermixHost, spec: DesiredHost, client: TermixClient) -> None:
    """Merge the diff onto the host's full existing record before PUTting
    it -- PUT is a confirmed full replace, not a partial update."""
    merged = dict(host.raw)
    merged.update(_host_body(spec))
    client.update_host(host.id, merged)
    log.info("updated %s (%s) in %s", spec.name, spec.ip, spec.folder)


def delete_host(host: TermixHost, client: TermixClient) -> None:
    client.delete_host(host.id)
    log.info("deleted %s (id %s)", host.name, host.id)
