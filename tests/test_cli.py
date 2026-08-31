from termix_aws_sync.cli import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    main,
    parse_args,
    resolve_interval,
    run_cycle,
)
from termix_aws_sync.config import load_config


def write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return str(path)


VALID_CONFIG = """
[termix]
folder = "AWS"
credential_id = 3

[aws]
ip_source = "internal"

[[aws.targets]]
region = "us-east-1"
"""


def test_missing_config_file_exits_2(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv("TERMIX_API_KEY", "fake-key")
    code = main(["--config", str(tmp_path / "nope.toml"), "--dry-run"])
    assert code == EXIT_CONFIG_ERROR
    assert "could not read config file" in caplog.text
    assert "Traceback" not in capsys.readouterr().err


def test_missing_termix_api_key_exits_2_no_traceback(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.delenv("TERMIX_API_KEY", raising=False)
    config_path = write_config(tmp_path, VALID_CONFIG)
    code = main(["--config", config_path, "--dry-run"])
    captured = capsys.readouterr()
    assert code == EXIT_CONFIG_ERROR
    assert "TERMIX_API_KEY" in caplog.text
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_missing_termix_url_exits_2_no_traceback(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv("TERMIX_API_KEY", "fake-key")
    monkeypatch.delenv("TERMIX_URL", raising=False)
    config_path = write_config(tmp_path, VALID_CONFIG)
    code = main(["--config", config_path, "--dry-run"])
    captured = capsys.readouterr()
    assert code == EXIT_CONFIG_ERROR
    assert "TERMIX_URL" in caplog.text
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_dry_run_with_valid_config_and_no_network_exits_2_no_traceback(
    tmp_path, monkeypatch, capsys, caplog
):
    monkeypatch.setenv("TERMIX_API_KEY", "fake-key")
    monkeypatch.setenv("TERMIX_URL", "http://termix.invalid")
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
    config_path = write_config(tmp_path, VALID_CONFIG)
    code = main(["--config", config_path, "--dry-run"])
    captured = capsys.readouterr()
    assert code == EXIT_CONFIG_ERROR
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert "failed to fetch state" in caplog.text


def test_invalid_sync_interval_env_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMIX_API_KEY", "fake-key")
    monkeypatch.setenv("SYNC_INTERVAL", "not-a-number")
    config_path = write_config(tmp_path, VALID_CONFIG)
    code = main(["--config", config_path, "--dry-run"])
    assert code == EXIT_CONFIG_ERROR


def test_dry_run_ignores_sync_interval_env(monkeypatch):
    # Regression: `docker compose run --rm sync --dry-run` must run once,
    # even though the same .env sets SYNC_INTERVAL for the long-running
    # `docker compose up` service.
    monkeypatch.setenv("SYNC_INTERVAL", "900")
    args = parse_args(["--dry-run"])
    assert resolve_interval(args) is None


def test_explicit_interval_flag_beats_dry_run(monkeypatch):
    monkeypatch.setenv("SYNC_INTERVAL", "900")
    args = parse_args(["--dry-run", "--interval", "30"])
    assert resolve_interval(args) == 30


def test_sync_interval_env_used_without_dry_run(monkeypatch):
    monkeypatch.setenv("SYNC_INTERVAL", "42")
    args = parse_args([])
    assert resolve_interval(args) == 42


def test_run_cycle_never_raises_on_unexpected_termix_client_failure(tmp_path, caplog):
    # Regression: talking to Termix can fail in ways that raise something
    # other than RuntimeError (e.g. a bug, or a genuinely unexpected
    # response). run_cycle's exception handling around the fetch phase
    # must catch any exception, not just RuntimeError -- otherwise it
    # crashes the process and, under `restart: unless-stopped`,
    # crash-loops the container.
    config = load_config(write_config(tmp_path, VALID_CONFIG))

    def runner(cmd, parse_json=False):
        if "describe-instances" in cmd:
            return []
        return ""

    class BrokenClient:
        def list_hosts(self):
            raise AttributeError("simulated unexpected failure")

    with caplog.at_level("ERROR"):
        code = run_cycle(config, dry_run=True, runner=runner, client=BrokenClient())
    assert code == EXIT_CONFIG_ERROR
    assert "failed to fetch state" in caplog.text


def test_version_flag_exits_0(capsys):
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == EXIT_OK
    else:
        raise AssertionError("--version should exit")
