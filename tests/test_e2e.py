"""End-to-end test: real `aws` subprocess (fake executable on PATH) plus a
real Termix HTTP server (a tiny in-process fake), driven through cli.main().

Reproduces the validated scenario from BUILD_BRIEF.md §7.2: 2 running
instances and 2 managed Termix hosts (one drifted, one orphaned) should
produce exactly one create, one update (of the correct host id), and one
delete -- and --dry-run should issue none of them.
"""

import os
import sys
from pathlib import Path

import pytest

from termix_aws_sync.cli import main

FAKES_DIR = Path(__file__).parent / "fakes"
sys.path.insert(0, str(FAKES_DIR))
from termix_server import FakeTermixServer  # noqa: E402

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

# id 101 -> i-0000000000000aaa1 (ip will drift vs. the fake aws output)
# id 102 -> i-0000000000000bbb2 (not present in the fake aws output -> delete)
INITIAL_HOSTS = [
    {
        "id": 101,
        "name": "existing-drifted",
        "ip": "10.0.0.1",
        "port": 22,
        "username": "ec2-user",
        "folder": "AWS",
        "tags": ["aws", "aws-sync", "aws-id-i-0000000000000aaa1", "us-east-1"],
        "authType": "credential",
        "credentialId": 3,
        "enableTerminal": True,
    },
    {
        "id": 102,
        "name": "orphaned",
        "ip": "10.0.0.2",
        "port": 22,
        "username": "ec2-user",
        "folder": "AWS",
        "tags": ["aws", "aws-sync", "aws-id-i-0000000000000bbb2", "us-east-1"],
        "authType": "credential",
        "credentialId": 3,
        "enableTerminal": True,
    },
]


@pytest.fixture
def fake_termix_server():
    server = FakeTermixServer(hosts=[dict(h) for h in INITIAL_HOSTS])
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _run(tmp_path, monkeypatch, fake_termix_server, extra_args):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG)

    monkeypatch.setenv("TERMIX_API_KEY", "fake-key")
    monkeypatch.setenv("TERMIX_URL", fake_termix_server.base_url)
    monkeypatch.setenv("PATH", f"{FAKES_DIR}{os.pathsep}{os.environ['PATH']}")

    return main(["--config", str(config_path), *extra_args])


def test_e2e_apply_issues_exactly_one_create_update_delete(
    tmp_path, monkeypatch, fake_termix_server
):
    code = _run(tmp_path, monkeypatch, fake_termix_server, [])
    assert code == 0

    write_calls = [c for c in fake_termix_server.calls if c[0] != "GET"]
    actions = [c[0] for c in write_calls]
    assert actions.count("POST") == 1
    assert actions.count("PUT") == 1
    assert actions.count("DELETE") == 1

    put_call = next(c for c in write_calls if c[0] == "PUT")
    assert put_call[1] == "/host/db/host/101"  # the drifted host's id
    assert put_call[2]["ip"] != "10.0.0.1"  # ip drift was applied
    assert put_call[2]["enableTerminal"] is True  # merged, not lost

    delete_call = next(c for c in write_calls if c[0] == "DELETE")
    assert delete_call[1] == "/host/db/host/102"  # the orphaned host's id

    # the orphaned host is actually gone, the drifted one survives updated
    assert 102 not in fake_termix_server.hosts
    assert 101 in fake_termix_server.hosts


def test_e2e_dry_run_issues_nothing(tmp_path, monkeypatch, fake_termix_server):
    code = _run(tmp_path, monkeypatch, fake_termix_server, ["--dry-run"])
    assert code == 0
    write_calls = [c for c in fake_termix_server.calls if c[0] != "GET"]
    assert write_calls == []
