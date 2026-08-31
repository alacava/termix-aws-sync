"""End-to-end test using the fake `aws`/`termix` executables in tests/fakes/.

Reproduces the validated scenario from BUILD_BRIEF.md §7.2: 2 running
instances and 2 managed Termix hosts (one drifted, one orphaned) should
produce exactly one create, one update (of the correct host id), and one
delete -- and --dry-run should issue none of them.
"""

import json
import os
from pathlib import Path

from termix_aws_sync.cli import main

FAKES_DIR = Path(__file__).parent / "fakes"

CONFIG = """
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


def _run(tmp_path, monkeypatch, extra_args):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG)
    log_path = tmp_path / "calls.log"

    monkeypatch.setenv("TERMIX_API_KEY", "fake-key")
    monkeypatch.setenv("TAS_TEST_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{FAKES_DIR}{os.pathsep}{os.environ['PATH']}")

    code = main(["--config", str(config_path), *extra_args])

    calls = []
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            calls.append(json.loads(line))
    return code, calls


def test_e2e_apply_issues_exactly_one_create_update_delete(tmp_path, monkeypatch):
    code, calls = _run(tmp_path, monkeypatch, [])
    assert code == 0

    actions = [c["action"] for c in calls]
    assert actions.count("create") == 1
    assert actions.count("update") == 1
    assert actions.count("delete") == 1

    update_call = next(c for c in calls if c["action"] == "update")
    assert update_call["argv"][0] == "101"  # host id of the drifted host

    delete_call = next(c for c in calls if c["action"] == "delete")
    assert delete_call["argv"][0] == "102"  # host id of the orphaned host


def test_e2e_dry_run_issues_nothing(tmp_path, monkeypatch):
    code, calls = _run(tmp_path, monkeypatch, ["--dry-run"])
    assert code == 0
    assert calls == []
