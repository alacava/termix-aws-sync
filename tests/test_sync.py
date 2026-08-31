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
    """Routes calls to canned AWS/Termix responses by target region."""

    def __init__(self, instances_by_region=None, hosts=None):
        self.instances_by_region = instances_by_region or {}
        self.hosts = hosts if hosts is not None else []
        self.calls = []

    def __call__(self, cmd, parse_json=False):
        self.calls.append(cmd)
        if "describe-instances" in cmd:
            region = cmd[cmd.index("--region") + 1]
            return self.instances_by_region.get(region, [])
        if "list" in cmd:
            return self.hosts
        return ""


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
    runner = FakeRunner(hosts=[{"id": 999, "name": "manual-host", "tags": ["aws-sync"]}])
    config = _single_target_config()
    with caplog.at_level("WARNING"):
        managed = fetch_termix_hosts(config, runner)
    assert managed == {}
    assert "no aws-id-* tag" in caplog.text


def test_managed_tag_filter_is_passed_to_termix_list(tmp_path):
    config = make_config(tmp_path, SINGLE_TARGET_CONFIG)
    runner = FakeRunner(hosts=[])
    fetch_termix_hosts(config, runner)
    [call] = runner.calls
    assert "--tag" in call
    assert call[call.index("--tag") + 1] == "aws-sync"


def _single_target_config():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.toml"
        path.write_text(SINGLE_TARGET_CONFIG)
        return load_config(str(path))


def test_apply_plan_issues_exactly_create_update_delete(monkeypatch):
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

    calls = []

    def runner(cmd, parse_json=False):
        calls.append(cmd)
        return ""

    failures = apply_plan(plan, d, c, runner)
    assert failures == 0
    actions = [c[2] for c in calls]  # ["create", "update", "delete"]
    assert actions == ["create", "update", "delete"]
    # update targets the existing termix host id, not the instance id
    update_cmd = calls[1]
    assert "201" in update_cmd


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
    assert result["i-savage"].folder == "AWS/savage-prod"
    assert result["i-lacava"].folder == "AWS/lacava"


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
