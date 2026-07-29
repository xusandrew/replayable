from __future__ import annotations

import pytest

from replayable.redact import (
    EnvFileError,
    SecretConfigError,
    load_secret_name_overrides,
    parse_env_file,
    redact_body,
    redact_headers,
    secret_names,
    secret_values,
)


def test_header_and_body_secrets_are_redacted_before_storage():
    environment = {
        "ANTHROPIC_API_KEY": "sk-real-secret",
        "DATABASE_PASSWORD": "hunter2",
        "NORMAL_SETTING": "visible",
    }

    headers = redact_headers(
        [
            ("authorization", "Bearer sk-real-secret"),
            ("set-cookie", "session=hunter2"),
            ("content-type", "application/json"),
        ],
        {"authorization", "set-cookie"},
    )
    body = redact_body(
        b'{"key":"sk-real-secret","password":"hunter2","mode":"visible"}',
        secret_values(environment),
    )

    assert headers == [
        ["authorization", "[REDACTED]"],
        ["set-cookie", "[REDACTED]"],
        ["content-type", "application/json"],
    ]
    assert body == (
        b'{"key":"[REDACTED:ANTHROPIC_API_KEY]",'
        b'"password":"[REDACTED:DATABASE_PASSWORD]","mode":"visible"}'
    )
    assert secret_names(environment) == {
        "ANTHROPIC_API_KEY",
        "DATABASE_PASSWORD",
    }


def test_url_embedded_credentials_are_classified_as_secrets():
    environment = {
        "DATABASE_URL": "postgres://user:hunter2@db.internal:5432/app",
        "PLAIN_URL": "https://example.test/path",
        "NORMAL_SETTING": "visible",
    }

    assert secret_names(environment) == {"DATABASE_URL"}
    assert secret_values(environment) == {
        "DATABASE_URL": "postgres://user:hunter2@db.internal:5432/app",
    }


def test_toml_secret_name_overrides_classify_extra_variables(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text(
        '[secrets]\nnames = ["DATABASE_URL", "CUSTOM_CRED"]\n',
        encoding="utf-8",
    )
    environment = {
        "DATABASE_URL": "postgres://localhost/app",
        "CUSTOM_CRED": "not-a-url-but-secret",
        "NORMAL_SETTING": "visible",
    }

    overrides = load_secret_name_overrides(path)
    assert overrides == ("DATABASE_URL", "CUSTOM_CRED")
    assert secret_names(environment, extra_names=overrides) == {
        "DATABASE_URL",
        "CUSTOM_CRED",
    }


def test_invalid_secret_overrides_raise(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text("[secrets]\nnames = 12\n", encoding="utf-8")

    with pytest.raises(SecretConfigError, match="names"):
        load_secret_name_overrides(path)


def test_parse_env_file_comments_bare_names_and_equals(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nAPI_TOKEN=value=with=equals\nFROM_HOST\nEMPTY=\n",
        encoding="utf-8",
    )

    assert parse_env_file(path, host_environment={"FROM_HOST": "host-value"}) == {
        "API_TOKEN": "value=with=equals",
        "FROM_HOST": "host-value",
        "EMPTY": "",
    }


def test_parse_env_file_rejects_invalid_names(tmp_path):
    path = tmp_path / ".env"
    path.write_text("NOT VALID=value\n", encoding="utf-8")

    with pytest.raises(EnvFileError, match="line 1"):
        parse_env_file(path)


# ---------------------------------------------------------------------------
# Secret-override config validation.
#
# A malformed [secrets] table must fail loudly. Silently ignoring it would
# mean a variable the user believed was declared secret gets recorded in
# plaintext, which is the one failure this module exists to prevent.
# ---------------------------------------------------------------------------


def test_missing_override_file_yields_no_extra_names(tmp_path):
    assert load_secret_name_overrides(None) == ()
    assert load_secret_name_overrides(tmp_path / "absent.toml") == ()


def test_override_file_without_secrets_table_yields_no_extra_names(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text('[normalization]\nfield_names = ["x"]\n', encoding="utf-8")

    assert load_secret_name_overrides(path) == ()


def test_override_names_are_deduplicated_in_order(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text(
        '[secrets]\nnames = ["ALPHA", "BETA", "ALPHA"]\n',
        encoding="utf-8",
    )

    assert load_secret_name_overrides(path) == ("ALPHA", "BETA")


def test_malformed_override_toml_is_rejected(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text("[secrets\nnames = ", encoding="utf-8")

    with pytest.raises(SecretConfigError, match="cannot load secret overrides"):
        load_secret_name_overrides(path)


def test_secrets_table_must_be_a_table(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text('secrets = "not-a-table"\n', encoding="utf-8")

    with pytest.raises(SecretConfigError, match=r"\[secrets\] must be a table"):
        load_secret_name_overrides(path)


@pytest.mark.parametrize(
    "names",
    ['names = "ANTHROPIC_API_KEY"', "names = [1, 2]", "names = [\"OK\", 3]"],
)
def test_secret_names_must_be_an_array_of_strings(tmp_path, names):
    path = tmp_path / "replayable.toml"
    path.write_text(f"[secrets]\n{names}\n", encoding="utf-8")

    with pytest.raises(SecretConfigError, match="must be an array of strings"):
        load_secret_name_overrides(path)


def test_unreadable_env_file_reports_the_path(tmp_path):
    """A directory where an env file was expected names the path, not errno."""

    path = tmp_path / "env-dir"
    path.mkdir()

    with pytest.raises(EnvFileError, match="cannot read environment file"):
        parse_env_file(path, host_environment={})
