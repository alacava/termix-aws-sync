"""Diff AWS desired state against Termix current state, and apply the plan."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List

from .aws import DesiredHost
from .http_client import TermixClient
from .termix import TermixHost, create_host, delete_host, update_host

log = logging.getLogger("termix-aws-sync")


def drifted(existing: TermixHost, spec: DesiredHost) -> bool:
    """True if the Termix host no longer matches the instance's desired state.

    Reads the same assumed field names documented in termix.py's module
    docstring (name/ip/port/username/tags/folder).
    """
    checks = [
        (existing.name, spec.name),
        (existing.ip, spec.ip),
        (int(existing.port or 0), spec.port),
        (existing.username, spec.username),
        (sorted(existing.tags or []), spec.tags),
        (existing.folder, spec.folder),
    ]
    return any(a != b for a, b in checks)


@dataclass(frozen=True)
class Plan:
    create: List[str] = field(default_factory=list)
    update: List[str] = field(default_factory=list)
    delete: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.create or self.update or self.delete)


def build_plan(desired: Dict[str, DesiredHost], current: Dict[str, TermixHost]) -> Plan:
    to_create = [iid for iid in desired if iid not in current]
    to_update = [
        iid for iid in desired if iid in current and drifted(current[iid], desired[iid])
    ]
    to_delete = [iid for iid in current if iid not in desired]
    return Plan(create=to_create, update=to_update, delete=to_delete)


def log_plan(plan: Plan, desired: Dict[str, DesiredHost], current: Dict[str, TermixHost]) -> None:
    log.info(
        "plan: create %d, update %d, delete %d",
        len(plan.create),
        len(plan.update),
        len(plan.delete),
    )
    for iid in plan.create:
        log.info("would create %s -> %s (%s)", iid, desired[iid].name, desired[iid].folder)
    for iid in plan.update:
        log.info("would update %s -> %s (%s)", iid, desired[iid].name, desired[iid].folder)
    for iid in plan.delete:
        log.info("would delete %s (termix id %s)", iid, current[iid].id)


def apply_plan(
    plan: Plan,
    desired: Dict[str, DesiredHost],
    current: Dict[str, TermixHost],
    client: TermixClient,
) -> int:
    """Execute the plan. Returns the number of failed operations."""
    failures = 0
    for iid in plan.create:
        try:
            create_host(desired[iid], client)
        except Exception as e:  # broad on purpose: one bad host must not abort the rest
            failures += 1
            log.error("create failed for %s: %s", iid, e)
    for iid in plan.update:
        try:
            update_host(current[iid], desired[iid], client)
        except Exception as e:  # broad on purpose: one bad host must not abort the rest
            failures += 1
            log.error("update failed for %s: %s", iid, e)
    for iid in plan.delete:
        try:
            delete_host(current[iid], client)
        except Exception as e:  # broad on purpose: one bad host must not abort the rest
            failures += 1
            log.error("delete failed for %s: %s", iid, e)
    return failures
