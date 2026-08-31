import pytest

from termix_aws_sync.aws import DesiredHost, DuplicateInstanceError, fetch_instances
from termix_aws_sync.config import load_config
from termix_aws_sync.sync import apply_plan, build_plan, drifted
from termix_aws_sync.termix import TermixHost, fetch_termix_hosts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return load_config(str(path))


def desired(iid, **overrides):
    base = dict(
        instance_id=iid,
        name=iid,
        ip="10.0.0.1",
        port=22,
        username="ec2-user",
        tags=sorted(["aws", "aws-sync", f"aws-id-{iid}", "us-east-1"]),
        folder="AWS",
        credential_id=3,
        key_file=None,
    )
    base.update(overrides)
    return DesiredHost(**base)


def current(iid, host_id, **overrides):
    base = dict(
        id=host_id,
        instance_id=iid,
        name=iid,
        ip="10.0.0.1",
        port=22,
        username="ec2-user",
        tags=sorted(["aws", "aws-sync", f"aws-id-{iid}", "us-east-1"]),
        folder="AWS",
        raw={},
    )
    base.update(overrides)
    return TermixHost(**base)


def instance(iid, private_ip="10.0.0.1", public_ip=None, tags=None, state="running"):
    tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
    inst = {
        "InstanceId": iid,
        "State": {"Name": state},
        "PrivateIpAddress": private_ip,
        "Tags": tag_list,
    }
    if public_ip:
        inst["PublicIpAddress"] = public_ip
    return inst


class FakeRunner:
    """Routes calls to canned AWS responses by target region."""

    def __init__(self, instances_by_region=None):
        self.instances_by_region = instances_by_region or {}
        self.calls = []

    def __call__(self, cmd, parse_json=False):
        self.calls.append(cmd)
        if "describe-instances" in cmd:
            region = cmd[cmd.index("--region") + 1]
            return self.instances_by_region.get(region, [])
        return ""


class FakeListOnlyClient:
    """Returns a fixed `list_hosts()` payload -- for exercising
    fetch_termix_hosts's own per-host handling directly."""

    def __init__(self, hosts):
        self._hosts = hosts

    def list_hosts(self):
        return self._hosts


class FakeTermixClient:
    """In-memory fake matching the real API's confirmed semantics: PUT is a
    full replace, not a merge (see termix.py's module docstring)."""

    def __init__(self, hosts=None):
        self._next_id = 1000
        self.hosts = {}
        for h in hosts or []:
            hid = h.get("id")
            if hid is None:
                hid = self._alloc_id()
            self.hosts[hid] = dict(h)
        self.calls = []

    def _alloc_id(self):
        self._next_id += 1
        return self._next_id

    def list_hosts(self):
        self.calls.append(("list", None))
        return list(self.hosts.values())

    def create_host(self, body):
        hid = self._alloc_id()
        record = dict(body)
        record["id"] = hid
        self.hosts[hid] = record
        self.calls.append(("create", body))
        return record

    def update_host(self, host_id, body):
        record = dict(body)  # full replace, matching the real API
        record["id"] = host_id
        self.hosts[host_id] = record
        self.calls.append(("update", (host_id, body)))
        return record

    def delete_host(self, host_id):
        self.hosts.pop(host_id, None)
        self.calls.append(("delete", host_id))


SINGLE_TARGET_CONFIG = """
[termix]
folder = "AWS"
managed_tag = "aws-sync"
extra_tags = ["aws"]
credential_id = 3

[aws]
ip_source = "internal"

[[aws.targets]]
region = "us-east-1"
"""


# ---------------------------------------------------------------------------
# Diff logic: create / update / delete / no-op
# ---------------------------------------------------------------------------


def test_fresh_create():
    d = {"i-1": desired("i-1")}
    plan = build_plan(d, {})
    assert plan.create == ["i-1"]
    assert plan.update == []
    assert plan.delete == []


def test_ip_drift_update():
    d = {"i-1": desired("i-1", ip="10.0.0.2")}
    c = {"i-1": current("i-1", 101, ip="10.0.0.1")}
    assert drifted(c["i-1"], d["i-1"])
    plan = build_plan(d, c)
    assert plan.update == ["i-1"]


def test_name_drift_update():
    d = {"i-1": desired("i-1", name="new-name")}
    c = {"i-1": current("i-1", 101, name="old-name")}
    assert drifted(c["i-1"], d["i-1"])
    assert build_plan(d, c).update == ["i-1"]


def test_tag_drift_update():
    d = {"i-1": desired("i-1", tags=["aws", "aws-sync", "aws-id-i-1", "us-east-1", "extra"])}
    c = {"i-1": current("i-1", 101, tags=["aws", "aws-sync", "aws-id-i-1", "us-east-1"])}
    assert drifted(c["i-1"], d["i-1"])
    assert build_plan(d, c).update == ["i-1"]


def test_folder_drift_update():
    d = {"i-1": desired("i-1", folder="AWS/new-env")}
    c = {"i-1": current("i-1", 101, folder="AWS/old-env")}
    assert drifted(c["i-1"], d["i-1"])
    assert build_plan(d, c).update == ["i-1"]


def test_terminated_instance_delete():
    c = {"i-1": current("i-1", 101)}
    plan = build_plan({}, c)
    assert plan.delete == ["i-1"]


def test_no_op():
    d = {"i-1": desired("i-1")}
    c = {"i-1": current("i-1", 101)}
    assert not drifted(c["i-1"], d["i-1"])
    plan = build_plan(d, c)
    assert plan.is_empty


def test_host_with_managed_tag_but_no_aws_id_tag_is_warned_and_skipped(caplog):
    client = FakeListOnlyClient([{"id": 999, "name": "manual-host", "tags": ["aws-sync"]}])
    config = _single_target_config()
    with caplog.at_level("WARNING"):
        managed = fetch_termix_hosts(config, client)
    assert managed == {}
    assert "no aws-id-* tag" in caplog.text


def test_unmanaged_hosts_without_managed_tag_are_ignored(tmp_path):
    # The API has no server-side tag filter (confirmed against a live
    # server): GET /host/db/host returns every host, so filtering by
    # managed_tag must happen here -- this is what keeps hand-created
    # hosts untouched.
    config = make_config(tmp_path, SINGLE_TARGET_CONFIG)
    client = FakeListOnlyClient(
        [
            {"id": 1, "name": "hand-created", "tags": ["some-other-tag"]},
            {"id": 2, "name": "managed", "tags": ["aws-sync", "aws-id-i-1"]},
        ]
    )
    managed = fetch_termix_hosts(config, client)
    assert list(managed) == ["i-1"]


def test_fetch_termix_hosts_skips_non_dict_entries(tmp_path, caplog):
    config = make_config(tmp_path, SINGLE_TARGET_CONFIG)
    client = FakeListOnlyClient(["not-a-host-object"])
    with caplog.at_level("WARNING"):
        managed = fetch_termix_hosts(config, client)
    assert managed == {}
    assert "unexpected non-object entry" in caplog.text


def _single_target_config():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.toml"
        path.write_text(SINGLE_TARGET_CONFIG)
        return load_config(str(path))


def test_apply_plan_issues_exactly_create_update_delete():
    d = {
        "i-new": desired("i-new"),
        "i-drift": desired("i-drift", ip="10.0.0.9"),
    }
    c = {
        "i-drift": current("i-drift", 201, ip="10.0.0.1"),
        "i-gone": current("i-gone", 202),
    }
    plan = build_plan(d, c)
    assert plan.create == ["i-new"]
    assert plan.update == ["i-drift"]
    assert plan.delete == ["i-gone"]

    client = FakeTermixClient()
    failures = apply_plan(plan, d, c, client)
    assert failures == 0

    actions = [action for action, _ in client.calls]
    assert actions == ["create", "update", "delete"]
    # update targets the existing termix host id, not the instance id
    update_host_id, update_body = client.calls[1][1]
    assert update_host_id == 201
    assert update_body["ip"] == "10.0.0.9"
    # delete targets the existing termix host id too
    assert client.calls[2][1] == 202


def test_create_and_update_bodies_always_enable_ssh():
    # Regression: enableSsh does not default to true from connectionType
    # "ssh" server-side (confirmed live) -- hosts created without it
    # explicitly set had SSH disabled entirely.
    d_create = {"i-1": desired("i-1")}
    client = FakeTermixClient()
    apply_plan(build_plan(d_create, {}), d_create, {}, client)

    create_body = next(body for action, body in client.calls if action == "create")
    assert create_body["enableSsh"] is True
    assert create_body["connectionType"] == "ssh"

    d_update = {"i-1": desired("i-1", ip="10.0.0.99")}
    c = {"i-1": current("i-1", 101, raw={"id": 101})}
    apply_plan(build_plan(d_update, c), d_update, c, client)

    _, update_body = next(payload for action, payload in client.calls if action == "update")
    assert update_body["enableSsh"] is True
    assert update_body["connectionType"] == "ssh"


def test_update_host_merges_diff_onto_existing_record_not_a_replace():
    # PUT is a confirmed full replace against the real API: update_host
    # must merge onto the host's full existing record (TermixHost.raw),
    # or fields the CLI/UI previously set (e.g. enableFileManager) would
    # silently reset to their defaults on every drift-triggered update.
    d = {"i-1": desired("i-1", ip="10.0.0.9")}
    c = {
        "i-1": current(
            "i-1",
            101,
            ip="10.0.0.1",
            raw={
                "id": 101,
                "name": "i-1",
                "ip": "10.0.0.1",
                "enableFileManager": True,
                "enableDocker": True,
                "notes": "do not touch",
            },
        )
    }
    client = FakeTermixClient()
    apply_plan(build_plan(d, c), d, c, client)

    [(_, (host_id, body))] = [c for c in client.calls if c[0] == "update"]
    assert host_id == 101
    assert body["ip"] == "10.0.0.9"  # the actual diff was applied
    # fields not part of the diff survive from the existing record
    assert body["enableFileManager"] is True
    assert body["enableDocker"] is True
    assert body["notes"] == "do not touch"


def test_apply_plan_survives_non_runtimeerror_failure(caplog):
    # A single bad operation raising something other than RuntimeError (e.g.
    # a bug, or an unexpected response shape) must still be counted as a
    # failure and must not stop the remaining operations from running.
    d = {"i-bad": desired("i-bad"), "i-ok": desired("i-ok")}
    c = {}
    plan = build_plan(d, c)
    assert sorted(plan.create) == ["i-bad", "i-ok"]

    class FlakyClient(FakeTermixClient):
        def create_host(self, body):
            if body["name"] == "i-bad":
                raise KeyError("boom")
            return super().create_host(body)

    with caplog.at_level("ERROR"):
        failures = apply_plan(plan, d, c, FlakyClient())
    assert failures == 1
    assert "create failed for i-bad" in caplog.text


# ---------------------------------------------------------------------------
# Multi-environment: distinct folders, per-target credentials, duplicate IDs
# ---------------------------------------------------------------------------

TWO_TARGET_CONFIG = """
[termix]
folder = "AWS"
managed_tag = "aws-sync"
credential_id = 3

[aws]
ip_source = "internal"

[[aws.targets]]
name = "savage-prod"
profile = "savage"
region = "us-east-1"

[[aws.targets]]
name = "lacava"
profile = "lacava"
region = "us-east-2"
credential_id = 7
"""


def test_two_targets_sync_into_two_distinct_folders(tmp_path):
    config = make_config(tmp_path, TWO_TARGET_CONFIG)
    runner = FakeRunner(
        instances_by_region={
            "us-east-1": [instance("i-savage")],
            "us-east-2": [instance("i-lacava")],
        }
    )
    result = fetch_instances(config, runner)
    assert result["i-savage"].folder == "AWS / savage-prod"
    assert result["i-lacava"].folder == "AWS / lacava"


def test_per_target_credential_selection(tmp_path):
    config = make_config(tmp_path, TWO_TARGET_CONFIG)
    runner = FakeRunner(
        instances_by_region={
            "us-east-1": [instance("i-savage")],
            "us-east-2": [instance("i-lacava")],
        }
    )
    result = fetch_instances(config, runner)
    assert result["i-savage"].credential_id == 3  # inherits global
    assert result["i-lacava"].credential_id == 7  # per-target override


def test_per_target_default_username(tmp_path):
    text = TWO_TARGET_CONFIG.replace(
        'credential_id = 7', 'credential_id = 7\ndefault_username = "ubuntu"'
    )
    config = make_config(tmp_path, text)
    runner = FakeRunner(
        instances_by_region={
            "us-east-1": [instance("i-savage")],
            "us-east-2": [instance("i-lacava")],
        }
    )
    result = fetch_instances(config, runner)
    assert result["i-savage"].username == "ec2-user"  # inherits global default
    assert result["i-lacava"].username == "ubuntu"  # per-target override


def test_instance_tag_overrides_per_target_default_username(tmp_path):
    text = TWO_TARGET_CONFIG.replace(
        'credential_id = 7', 'credential_id = 7\ndefault_username = "ubuntu"'
    )
    config = make_config(tmp_path, text)
    runner = FakeRunner(
        instances_by_region={
            "us-east-2": [instance("i-lacava", tags={"termix:user": "admin"})],
        }
    )
    result = fetch_instances(config, runner)
    assert result["i-lacava"].username == "admin"


def test_duplicate_instance_id_across_targets_rejected(tmp_path):
    config = make_config(tmp_path, TWO_TARGET_CONFIG)
    runner = FakeRunner(
        instances_by_region={
            "us-east-1": [instance("i-dup")],
            "us-east-2": [instance("i-dup")],
        }
    )
    with pytest.raises(DuplicateInstanceError) as exc_info:
        fetch_instances(config, runner)
    message = str(exc_info.value)
    assert "savage-prod" in message
    assert "lacava" in message


# ---------------------------------------------------------------------------
# IP source resolution
# ---------------------------------------------------------------------------


def test_ip_source_global_internal(tmp_path):
    config = make_config(tmp_path, SINGLE_TARGET_CONFIG)  # ip_source = internal
    inst = instance("i-1", private_ip="10.0.0.1", public_ip="1.2.3.4")
    runner = FakeRunner(instances_by_region={"us-east-1": [inst]})
    result = fetch_instances(config, runner)
    assert result["i-1"].ip == "10.0.0.1"


def test_ip_source_global_external(tmp_path):
    text = SINGLE_TARGET_CONFIG.replace('ip_source = "internal"', 'ip_source = "external"')
    config = make_config(tmp_path, text)
    inst = instance("i-1", private_ip="10.0.0.1", public_ip="1.2.3.4")
    runner = FakeRunner(instances_by_region={"us-east-1": [inst]})
    result = fetch_instances(config, runner)
    assert result["i-1"].ip == "1.2.3.4"


def test_ip_source_per_target_override(tmp_path):
    # apply the override only to the second target block
    text = TWO_TARGET_CONFIG.replace(
        'credential_id = 7', 'credential_id = 7\nip_source = "external"'
    )
    config = make_config(tmp_path, text)
    runner = FakeRunner(
        instances_by_region={
            "us-east-1": [instance("i-savage", private_ip="10.0.0.1", public_ip="1.2.3.4")],
            "us-east-2": [instance("i-lacava", private_ip="10.0.0.2", public_ip="5.6.7.8")],
        }
    )
    result = fetch_instances(config, runner)
    assert result["i-savage"].ip == "10.0.0.1"  # inherits global internal
    assert result["i-lacava"].ip == "5.6.7.8"  # per-target external override


def test_ip_source_per_instance_tag_override(tmp_path):
    config = make_config(tmp_path, SINGLE_TARGET_CONFIG)  # global internal
    runner = FakeRunner(
        instances_by_region={
            "us-east-1": [
                instance(
                    "i-1",
                    private_ip="10.0.0.1",
                    public_ip="1.2.3.4",
                    tags={"termix:ip": "external"},
                )
            ]
        }
    )
    result = fetch_instances(config, runner)
    assert result["i-1"].ip == "1.2.3.4"


def test_instance_skipped_with_warning_when_external_selected_but_no_public_ip(tmp_path, caplog):
    text = SINGLE_TARGET_CONFIG.replace('ip_source = "internal"', 'ip_source = "external"')
    config = make_config(tmp_path, text)
    runner = FakeRunner(
        instances_by_region={"us-east-1": [instance("i-1", private_ip="10.0.0.1", public_ip=None)]}
    )
    with caplog.at_level("WARNING"):
        result = fetch_instances(config, runner)
    assert result == {}
    assert "no PublicIpAddress" in caplog.text or "PublicIpAddress" in caplog.text
