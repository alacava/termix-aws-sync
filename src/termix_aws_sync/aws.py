"""AWS side: fetch running EC2 instances and translate them into desired
Termix host specs, per the configured targets and IP-source resolution.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import IP_SOURCE_FIELD, Config, Target, normalize_ip_source
from .runner import Runner, default_runner

log = logging.getLogger("termix-aws-sync")


def _aws_bin() -> str:
    """Resolved lazily (not at import time) so tests can point PATH at fakes."""
    return shutil.which("aws") or "aws"


class DuplicateInstanceError(Exception):
    """Raised when the same instance ID is discovered under two targets."""

    def __init__(self, instance_id: str, first_target: str, second_target: str):
        self.instance_id = instance_id
        self.first_target = first_target
        self.second_target = second_target
        super().__init__(
            f"instance {instance_id} appears in both target "
            f"{first_target!r} and target {second_target!r}; "
            "an instance must be reachable from only one configured target"
        )


@dataclass(frozen=True)
class DesiredHost:
    """The Termix host state an EC2 instance should have."""

    instance_id: str
    name: str
    ip: str
    port: int
    username: str
    tags: List[str]
    folder: str
    credential_id: Optional[int]
    key_file: Optional[str]


def _resolve_ip_source(tags: Dict[str, str], target: Target) -> str:
    """Instance tag > target > global (already folded into target.ip_source)."""
    override = tags.get("termix:ip")
    if override:
        return normalize_ip_source(override, where=f"termix:ip tag on {tags.get('Name', '?')}")
    return target.ip_source


def fetch_instances(
    config: Config, runner: Runner = default_runner
) -> Dict[str, DesiredHost]:
    """Return {instance_id: DesiredHost} across all configured AWS targets."""
    desired: Dict[str, DesiredHost] = {}
    origin: Dict[str, str] = {}

    for target in config.targets:
        cmd = [
            _aws_bin(),
            "ec2",
            "describe-instances",
            "--region",
            target.region,
            "--filters",
            "Name=instance-state-name,Values=running",
            "--query",
            "Reservations[].Instances[]",
            "--output",
            "json",
        ]
        if target.profile:
            cmd += ["--profile", target.profile]

        instances: List[Dict[str, Any]] = runner(cmd, parse_json=True)
        for inst in instances:
            iid = inst["InstanceId"]
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}

            required = config.required_tag
            if required and tags.get(required.key) != required.value:
                continue

            ip_source = _resolve_ip_source(tags, target)
            field = IP_SOURCE_FIELD[ip_source]
            ip = inst.get(field)
            if not ip:
                log.warning(
                    "skipping %s (target %s): no %s (ip_source=%s)",
                    iid,
                    target.label,
                    field,
                    ip_source,
                )
                continue

            if iid in origin:
                raise DuplicateInstanceError(iid, origin[iid], target.label)
            origin[iid] = target.label

            tag_list = sorted(
                set(
                    config.extra_tags
                    + [config.managed_tag, f"aws-id-{iid}", target.region]
                    + ([target.tag] if target.tag else [])
                )
            )

            desired[iid] = DesiredHost(
                instance_id=iid,
                name=tags.get("Name", iid),
                ip=ip,
                port=int(tags.get("termix:port", config.default_port)),
                username=tags.get("termix:user", config.default_username),
                tags=tag_list,
                folder=target.folder,
                credential_id=target.credential_id,
                key_file=target.key_file,
            )

    return desired
