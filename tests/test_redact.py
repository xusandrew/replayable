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
