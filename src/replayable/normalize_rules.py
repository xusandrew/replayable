"""Data-driven request normalization rules and project overrides."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

VOLATILE_SENTINEL = "§VOLATILE§"

DEFAULT_FIELD_NAMES = (
    "id",
    "request_id",
    "tool_call_id",
    "tool_use_id",
    "call_id",
    "trace_id",
    "span_id",
    "idempotency_key",
    "nonce",
    "created",
    "created_at",
    "timestamp",
)

DEFAULT_VALUE_PATTERNS = (
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    (
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
    ),
)


class RulesError(RuntimeError):
    """A malformed replayable.toml normalization configuration."""


@dataclass(frozen=True)
class NormalizationRules:
    """The effective defaults plus project-specific additions/exemptions."""

    field_names: tuple[str, ...] = DEFAULT_FIELD_NAMES
    value_patterns: tuple[str, ...] = DEFAULT_VALUE_PATTERNS
    preserve: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        rendered = json.dumps(
            {
                "field_names": sorted(self.field_names),
                "value_patterns": sorted(self.value_patterns),
                "preserve": sorted(self.preserve),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"sha256:{hashlib.sha256(rendered).hexdigest()}"

    @cached_property
    def compiled_value_patterns(self) -> tuple[re.Pattern[str], ...]:
        try:
            return tuple(re.compile(pattern) for pattern in self.value_patterns)
        except re.error as exc:
            raise RulesError(f"invalid normalization regex: {exc}") from exc

    @cached_property
    def lowered_field_names(self) -> frozenset[str]:
        return frozenset(name.lower() for name in self.field_names)

    @cached_property
    def lowered_preserve(self) -> frozenset[str]:
        return frozenset(name.lower() for name in self.preserve)


def _string_list(table: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for name in names:
        if name not in table:
            continue
        candidate = table[name]
        if not isinstance(candidate, list) or not all(
            isinstance(value, str) for value in candidate
        ):
            raise RulesError(f"{name} must be an array of strings")
        values.extend(candidate)
    return values


def load_rules(path: Path | None = None) -> NormalizationRules:
    """Load defaults, optionally extended by a replayable.toml file."""

    if path is None:
        return NormalizationRules()
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NormalizationRules()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RulesError(f"cannot load normalization rules from {path}: {exc}") from exc

    table = document.get("normalization", document)
    if not isinstance(table, dict):
        raise RulesError("[normalization] must be a table")
    added_fields = _string_list(
        table,
        ("field_names", "fields", "add_field_names"),
    )
    added_regexes = _string_list(table, ("regexes", "add_regexes"))
    preserved = _string_list(table, ("preserve",))
    rules = NormalizationRules(
        field_names=tuple(dict.fromkeys((*DEFAULT_FIELD_NAMES, *added_fields))),
        value_patterns=tuple(
            dict.fromkeys((*DEFAULT_VALUE_PATTERNS, *added_regexes))
        ),
        preserve=tuple(dict.fromkeys(preserved)),
    )
    # Compile eagerly. An invalid regex in replayable.toml then fails here with
    # a clear RulesError, rather than inside the proxy's request hook partway
    # through a replay, where the failure would surface as a mismatch instead.
    _ = rules.compiled_value_patterns
    return rules


def discover_rules_path(cassette: Path | None = None, cwd: Path | None = None) -> Path | None:
    """Resolve rules the same way record/replay/explain do.

    With a cassette, only ``cassette/replayable.toml`` applies (or defaults).
    Without a cassette, the working-directory override is used for record and
    cassette-less explain.
    """

    if cassette is not None:
        cassette_override = cassette / "replayable.toml"
        return cassette_override if cassette_override.is_file() else None
    cwd_override = (cwd or Path.cwd()) / "replayable.toml"
    return cwd_override if cwd_override.is_file() else None
