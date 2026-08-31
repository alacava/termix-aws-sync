"""Configuration loading and validation.

Config is TOML (see README for the full reference). On Python 3.11+ this
uses the standard-library ``tomllib``. Python 3.9/3.10 don't ship it, so we
depend on ``tomli`` (the backport that became ``tomllib``) only on those
versions -- see pyproject.toml's environment marker. This is the one
deliberate deviation from "standard library only at runtime"; it's isolated
to this import and documented in the README.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_MANAGED_TAG = "aws-sync"
DEFAULT_FOLDER = "AWS"
DEFAULT_USERNAME = "ec2-user"
DEFAULT_PORT = 22
DEFAULT_IP_SOURCE = "private"

# Canonical internal representation is "private" / "public", mapped to the
# EC2 describe-instances response fields below.
IP_SOURCE_FIELD = {
    "private": "PrivateIpAddress",
    "public": "PublicIpAddress",
}
_IP_SOURCE_ALIASES = {
    "private": "private",
    "internal": "private",
    "public": "public",
    "external": "public",
}
_IP_FIELD_ALIASES = {
    "PrivateIpAddress": "private",
    "PublicIpAddress": "public",
}
IP_SOURCE_ACCEPTED_VALUES = (
    '"private"/"internal", "public"/"external" '
    '(or legacy ip_field values "PrivateIpAddress"/"PublicIpAddress")'
)


class ConfigError(Exception):
    """Raised for missing/invalid configuration. Callers should exit 2."""


def normalize_ip_source(value: str, *, where: str) -> str:
    """Normalize a user-supplied ip_source/ip_field value to "private"/"public"."""
    if value in _IP_FIELD_ALIASES:
        return _IP_FIELD_ALIASES[value]
    normalized = _IP_SOURCE_ALIASES.get(str(value).strip().lower())
    if normalized is None:
        raise ConfigError(
            f"invalid ip_source {value!r} in {where}; accepted values are "
            f"{IP_SOURCE_ACCEPTED_VALUES}"
        )
    return normalized


def _extract_ip_source(table: Dict[str, Any], where: str) -> Optional[str]:
    """Read ip_source (or legacy ip_field) from a config table, normalized."""
    if "ip_source" in table and "ip_field" in table:
        raise ConfigError(
            f"{where} sets both ip_source and ip_field; use only one"
        )
    if "ip_source" in table:
        return normalize_ip_source(table["ip_source"], where=where)
    if "ip_field" in table:
        return normalize_ip_source(table["ip_field"], where=where)
    return None


@dataclass(frozen=True)
class RequiredTag:
    key: str
    value: str


@dataclass(frozen=True)
class Target:
    """A resolved (already-defaulted) AWS environment to sync from."""

    profile: Optional[str]
    region: str
    name: Optional[str]
    folder: str
    credential_id: Optional[int]
    key_file: Optional[str]
    ip_source: str  # "private" or "public"
    default_username: str

    @property
    def label(self) -> str:
        """Human-readable identifier for error messages."""
        if self.name:
            return self.name
        return f"{self.profile or 'default'}@{self.region}"

    @property
    def tag(self) -> Optional[str]:
        """Tag applied to hosts from this target in place of the bare profile."""
        return self.name or self.profile


@dataclass(frozen=True)
class Config:
    folder: str
    managed_tag: str
    extra_tags: List[str]
    credential_id: Optional[int]
    key_file: Optional[str]
    ip_source: str
    default_username: str
    default_port: int
    required_tag: Optional[RequiredTag]
    targets: List[Target]
    termix_api_key: str = field(repr=False, default="")
    termix_url: Optional[str] = None


def default_config_search_paths() -> List[Path]:
    return [
        Path("/etc/termix-aws-sync.toml"),
        Path("~/.config/termix-aws-sync.toml").expanduser(),
    ]


def resolve_config_path(cli_path: Optional[str]) -> Path:
    if cli_path:
        return Path(cli_path)
    env_path = os.environ.get("TERMIX_AWS_SYNC_CONFIG")
    if env_path:
        return Path(env_path)
    for candidate in default_config_search_paths():
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(p) for p in default_config_search_paths())
    raise ConfigError(
        "no config file found; pass --config, set TERMIX_AWS_SYNC_CONFIG, "
        f"or place one at: {searched}"
    )


def _require_str(table: Dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {key!r} must be a non-empty string")
    return value


def _parse_required_tag(table: Dict[str, Any], where: str) -> Optional[RequiredTag]:
    raw = table.get("required_tag")
    if raw is None:
        return None
    if not isinstance(raw, dict) or "key" not in raw or "value" not in raw:
        raise ConfigError(
            f"{where}: required_tag must be a table like "
            '{ key = "termix", value = "true" }'
        )
    return RequiredTag(key=str(raw["key"]), value=str(raw["value"]))


def load_config(cli_path: Optional[str] = None) -> Config:
    path = resolve_config_path(cli_path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    termix_table = data.get("termix", {})
    aws_table = data.get("aws", {})
    if not isinstance(termix_table, dict) or not isinstance(aws_table, dict):
        raise ConfigError("[termix] and [aws] must be tables")

    global_folder = termix_table.get("folder", DEFAULT_FOLDER)
    if not isinstance(global_folder, str) or not global_folder:
        raise ConfigError("[termix]: 'folder' must be a non-empty string")

    managed_tag = termix_table.get("managed_tag", DEFAULT_MANAGED_TAG)
    extra_tags = termix_table.get("extra_tags", [])
    if not isinstance(extra_tags, list) or not all(isinstance(t, str) for t in extra_tags):
        raise ConfigError("[termix]: 'extra_tags' must be a list of strings")

    global_credential_id = termix_table.get("credential_id")
    global_key_file = termix_table.get("key_file")

    global_ip_source = _extract_ip_source(aws_table, "[aws]") or DEFAULT_IP_SOURCE
    env_ip_source = os.environ.get("IP_SOURCE")
    if env_ip_source:
        global_ip_source = normalize_ip_source(env_ip_source, where="IP_SOURCE env var")

    default_username = aws_table.get("default_username", DEFAULT_USERNAME)
    if not isinstance(default_username, str) or not default_username:
        raise ConfigError("[aws]: 'default_username' must be a non-empty string")
    default_port = aws_table.get("default_port", DEFAULT_PORT)
    if not isinstance(default_port, int) or isinstance(default_port, bool):
        raise ConfigError("[aws]: 'default_port' must be an integer")
    required_tag = _parse_required_tag(aws_table, "[aws]")

    raw_targets = aws_table.get("targets", [])
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("[[aws.targets]]: at least one target must be configured")

    targets: List[Target] = []
    for i, raw in enumerate(raw_targets):
        where = f"[[aws.targets]] #{i + 1}"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: must be a table")
        region = _require_str(raw, "region", where)
        profile = raw.get("profile")
        if profile is not None and not isinstance(profile, str):
            raise ConfigError(f"{where}: 'profile' must be a string")
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise ConfigError(f"{where}: 'name' must be a string")

        if "folder" in raw:
            folder = raw["folder"]
            if not isinstance(folder, str) or not folder:
                raise ConfigError(f"{where}: 'folder' must be a non-empty string")
        elif name:
            folder = f"{global_folder}/{name}"
        else:
            folder = global_folder

        credential_id = raw.get("credential_id", global_credential_id)
        key_file = raw.get("key_file", global_key_file)
        if credential_id is not None and not isinstance(credential_id, int):
            raise ConfigError(f"{where}: 'credential_id' must be an integer")
        if key_file is not None and not isinstance(key_file, str):
            raise ConfigError(f"{where}: 'key_file' must be a string")
        if credential_id is None and not key_file:
            raise ConfigError(
                f"{where}: no SSH auth method resolved; set 'credential_id' or "
                "'key_file' (per-target or in [termix])"
            )

        target_ip_source = _extract_ip_source(raw, where) or global_ip_source

        target_default_username = raw.get("default_username", default_username)
        if not isinstance(target_default_username, str) or not target_default_username:
            raise ConfigError(f"{where}: 'default_username' must be a non-empty string")

        targets.append(
            Target(
                profile=profile,
                region=region,
                name=name,
                folder=folder,
                credential_id=credential_id,
                key_file=key_file,
                ip_source=target_ip_source,
                default_username=target_default_username,
            )
        )

    termix_api_key = os.environ.get("TERMIX_API_KEY", "")
    termix_url = os.environ.get("TERMIX_URL")

    return Config(
        folder=global_folder,
        managed_tag=managed_tag,
        extra_tags=list(extra_tags),
        credential_id=global_credential_id,
        key_file=global_key_file,
        ip_source=global_ip_source,
        default_username=default_username,
        default_port=default_port,
        required_tag=required_tag,
        targets=targets,
        termix_api_key=termix_api_key,
        termix_url=termix_url,
    )
