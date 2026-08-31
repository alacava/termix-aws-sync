import pytest

from termix_aws_sync.config import ConfigError, load_config, normalize_ip_source

BASE = """
[termix]
folder = "AWS"
managed_tag = "aws-sync"
extra_tags = ["aws"]
credential_id = 3

[aws]
ip_source = "internal"
default_username = "ec2-user"
default_port = 22

[[aws.targets]]
name = "savage-prod"
profile = "savage"
region = "us-east-1"

[[aws.targets]]
name = "lacava"
profile = "lacava"
region = "us-east-2"
credential_id = 7
ip_source = "external"
default_username = "ubuntu"

[[aws.targets]]
region = "us-east-1"
"""


def write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return str(path)


def test_load_valid_config(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    assert config.folder == "AWS"
    assert config.managed_tag == "aws-sync"
    assert config.ip_source == "private"  # "internal" normalizes to "private"
    assert len(config.targets) == 3


def test_folder_resolution_precedence(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    by_name = {t.label: t for t in config.targets}
    # name-derived default
    assert by_name["savage-prod"].folder == "AWS/savage-prod"
    assert by_name["lacava"].folder == "AWS/lacava"
    # unnamed target falls back to the global folder
    unnamed = [t for t in config.targets if t.name is None][0]
    assert unnamed.folder == "AWS"


def test_explicit_folder_beats_name_derived_default(tmp_path):
    text = BASE.replace(
        'name = "savage-prod"\nprofile = "savage"\nregion = "us-east-1"',
        'name = "savage-prod"\nprofile = "savage"\nregion = "us-east-1"\n'
        'folder = "Savage/Production"',
    )
    config = load_config(write_config(tmp_path, text))
    by_name = {t.label: t for t in config.targets}
    assert by_name["savage-prod"].folder == "Savage/Production"


def test_credential_id_resolution_per_target_overrides_global(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    by_name = {t.label: t for t in config.targets}
    assert by_name["savage-prod"].credential_id == 3  # inherits global
    assert by_name["lacava"].credential_id == 7  # per-target override


def test_ip_source_resolution_global_and_per_target(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    by_name = {t.label: t for t in config.targets}
    assert config.ip_source == "private"
    assert by_name["savage-prod"].ip_source == "private"  # inherits global
    assert by_name["lacava"].ip_source == "public"  # per-target override


def test_default_username_resolution_global_and_per_target(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    by_name = {t.label: t for t in config.targets}
    assert config.default_username == "ec2-user"
    assert by_name["savage-prod"].default_username == "ec2-user"  # inherits global
    assert by_name["lacava"].default_username == "ubuntu"  # per-target override


def test_invalid_global_default_username_rejected(tmp_path):
    text = BASE.replace('default_username = "ec2-user"', 'default_username = ""')
    with pytest.raises(ConfigError, match="default_username"):
        load_config(write_config(tmp_path, text))


def test_invalid_per_target_default_username_rejected(tmp_path):
    text = BASE.replace('default_username = "ubuntu"', "default_username = 7")
    with pytest.raises(ConfigError, match="default_username"):
        load_config(write_config(tmp_path, text))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("private", "private"),
        ("internal", "private"),
        ("public", "public"),
        ("external", "public"),
        ("PrivateIpAddress", "private"),
        ("PublicIpAddress", "public"),
    ],
)
def test_normalize_ip_source_accepts_aliases(value, expected):
    assert normalize_ip_source(value, where="test") == expected


def test_normalize_ip_source_rejects_invalid_value():
    with pytest.raises(ConfigError, match="invalid ip_source"):
        normalize_ip_source("sideways", where="test")


def test_legacy_ip_field_key_is_synonym(tmp_path):
    text = BASE.replace('ip_source = "internal"', 'ip_field = "PublicIpAddress"')
    config = load_config(write_config(tmp_path, text))
    assert config.ip_source == "public"


def test_ip_source_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("IP_SOURCE", "external")
    config = load_config(write_config(tmp_path, BASE))
    assert config.ip_source == "public"
    by_name = {t.label: t for t in config.targets}
    # target-level override still wins over the env-overridden global
    assert by_name["lacava"].ip_source == "public"


def test_invalid_ip_source_in_file_is_rejected_at_load(tmp_path):
    text = BASE.replace('ip_source = "internal"', 'ip_source = "sideways"')
    with pytest.raises(ConfigError, match="invalid ip_source"):
        load_config(write_config(tmp_path, text))


def test_no_auth_method_resolved_is_an_error(tmp_path):
    # Confirmed against a real server: `termix hosts create` hard-requires
    # --password/--key-file/--credential-id and has no folder-level
    # credential fallback, so catch this at config load, not per-host at
    # sync time.
    text = """
[termix]
folder = "AWS"

[aws]

[[aws.targets]]
region = "us-east-1"
"""
    with pytest.raises(ConfigError, match="no SSH auth method resolved"):
        load_config(write_config(tmp_path, text))


def test_key_file_auth_is_accepted(tmp_path):
    text = """
[termix]
folder = "AWS"
key_file = "/home/app/.ssh/aws-fleet.pem"

[aws]

[[aws.targets]]
region = "us-east-1"
"""
    config = load_config(write_config(tmp_path, text))
    assert config.targets[0].key_file == "/home/app/.ssh/aws-fleet.pem"


def test_required_tag_parsed(tmp_path):
    text = BASE.replace(
        "[aws]\nip_source", '[aws]\nrequired_tag = { key = "termix", value = "true" }\nip_source'
    )
    config = load_config(write_config(tmp_path, text))
    assert config.required_tag.key == "termix"
    assert config.required_tag.value == "true"


def test_explicit_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="could not read config file"):
        load_config(str(tmp_path / "does-not-exist.toml"))


def test_no_config_file_found_via_search_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("TERMIX_AWS_SYNC_CONFIG", raising=False)
    monkeypatch.setattr(
        "termix_aws_sync.config.default_config_search_paths",
        lambda: [tmp_path / "etc.toml", tmp_path / "home.toml"],
    )
    with pytest.raises(ConfigError, match="no config file found"):
        load_config(None)


def test_missing_targets_raises(tmp_path):
    text = """
[termix]
folder = "AWS"
credential_id = 1

[aws]
"""
    with pytest.raises(ConfigError, match="at least one target"):
        load_config(write_config(tmp_path, text))


def test_target_missing_region_raises(tmp_path):
    text = """
[termix]
folder = "AWS"
credential_id = 1

[aws]

[[aws.targets]]
name = "no-region"
"""
    with pytest.raises(ConfigError, match="region"):
        load_config(write_config(tmp_path, text))


def test_duplicate_ip_source_keys_rejected(tmp_path):
    text = BASE.replace(
        'ip_source = "internal"', 'ip_source = "internal"\nip_field = "PublicIpAddress"'
    )
    with pytest.raises(ConfigError, match="both ip_source and ip_field"):
        load_config(write_config(tmp_path, text))
