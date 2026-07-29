"""Secret classification and write-time cassette redaction."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Iterable, Mapping

SECRET_NAME_PATTERN = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
# userinfo credentials inside a URL, e.g. postgres://user:hunter2@host/db
URL_CREDENTIAL_PATTERN = re.compile(r"://[^/\s@:]+:[^/\s@]+@")
HEADER_REDACTION_VALUE = "[REDACTED]"


class EnvFileError(ValueError):
    """An invalid Docker-style environment file."""


class SecretConfigError(ValueError):
    """An invalid ``[secrets]`` table in replayable.toml."""


def redacted_placeholder(name: str) -> str:
    """Token written into cassettes and injected as the replay-time secret value."""

    return f"[REDACTED:{name}]"


def is_secret_name(name: str) -> bool:
    return SECRET_NAME_PATTERN.search(name) is not None


def is_secret_variable(
    name: str,
    value: str,
    *,
    extra_names: Iterable[str] = (),
) -> bool:
    """Classify by name convention, URL-embedded credentials, or an override list."""

    if is_secret_name(name) or name in extra_names:
        return True
    return URL_CREDENTIAL_PATTERN.search(value) is not None


def secret_names(
    environment: Mapping[str, str],
    *,
    extra_names: Iterable[str] = (),
) -> set[str]:
    extras = set(extra_names)
    return {
        name
        for name, value in environment.items()
        if is_secret_variable(name, value, extra_names=extras)
    }


def secret_values(
    environment: Mapping[str, str],
    *,
    extra_names: Iterable[str] = (),
) -> dict[str, str]:
    """Return non-empty secret-classified values for body replacement."""

    extras = set(extra_names)
    return {
        name: value
        for name, value in environment.items()
        if is_secret_variable(name, value, extra_names=extras) and value
    }


def load_secret_name_overrides(path: Path | None = None) -> tuple[str, ...]:
    """Load additional secret variable names from ``[secrets] names`` in toml."""

    if path is None or not path.is_file():
        return ()
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SecretConfigError(
            f"cannot load secret overrides from {path}: {exc}"
        ) from exc
    table = document.get("secrets")
    if table is None:
        return ()
    if not isinstance(table, dict):
        raise SecretConfigError("[secrets] must be a table")
    names = table.get("names", [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise SecretConfigError("[secrets].names must be an array of strings")
    return tuple(dict.fromkeys(names))


def redact_headers(
    headers: Iterable[tuple[str, str]],
    redacted_names: Iterable[str],
) -> list[list[str]]:
    """Preserve ordered duplicate headers while replacing sensitive values."""

    redacted = {name.lower() for name in redacted_names}
    return [
        [name.lower(), HEADER_REDACTION_VALUE if name.lower() in redacted else value]
        for name, value in headers
    ]


def redact_body(body: bytes, secrets: Mapping[str, str]) -> bytes:
    """Replace literal secret values in arbitrary request/response bytes."""

    redacted = body
    replacements = sorted(
        (
            (value.encode("utf-8"), redacted_placeholder(name).encode("utf-8"))
            for name, value in secrets.items()
            if value
        ),
        key=lambda item: (-len(item[0]), item[1]),
    )
    for value, replacement in replacements:
        redacted = redacted.replace(value, replacement)
    return redacted


def parse_env_file(
    path: Path,
    *,
    host_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Parse the KEY=VALUE subset accepted by Docker's ``--env-file``."""

    host_environment = host_environment if host_environment is not None else os.environ
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EnvFileError(f"cannot read environment file {path}: {exc}") from exc

    environment: dict[str, str] = {}
    for line_number, original_line in enumerate(lines, start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, value = line.split("=", maxsplit=1)
            name = name.strip()
        else:
            name = line
            value = host_environment.get(name, "")
        if not name or any(character.isspace() for character in name):
            raise EnvFileError(
                f"invalid variable name in {path} on line {line_number}"
            )
        environment[name] = value
    return environment
