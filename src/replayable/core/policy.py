"""Deterministic policy resolution and cassette pinning."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from replayable.cassette.events import EventChannel

POLICY_VERSION = 1
LEGACY_MODE = "legacy"


class PolicyError(ValueError):
    """A policy configuration or pinned policy manifest is invalid."""


class PolicyMode(StrEnum):
    """The policy modes supported by the functional demo."""

    FREEZE = "freeze"
    STRICT_OFFLINE = "strict-offline"
    PASSTHROUGH = "passthrough"


class PolicySource(StrEnum):
    """The precedence level that selected a resolved policy."""

    CLI = "cli"
    SCENARIO = "scenario"
    SCOPE_RULE = "scope-rule"
    CHANNEL_DEFAULT = "channel-default"
    LEGACY = "legacy"


@dataclass(frozen=True)
class ScopeRule:
    """A channel-specific glob rule, evaluated in declaration order."""

    channel: EventChannel
    scope: str
    mode: PolicyMode

    def __post_init__(self) -> None:
        if not self.scope:
            raise PolicyError("policy scope patterns must not be empty")

    def matches(self, channel: EventChannel, scope: str) -> bool:
        return self.channel is channel and fnmatch.fnmatchcase(scope, self.scope)

    def as_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel.value,
            "scope": self.scope,
            "mode": self.mode.value,
        }


@dataclass(frozen=True)
class PolicyConfig:
    """Validated policy inputs, including the built-in channel defaults."""

    channel_defaults: tuple[tuple[EventChannel, PolicyMode], ...]
    scope_rules: tuple[ScopeRule, ...] = ()
    scenario: PolicyMode | None = None

    def __post_init__(self) -> None:
        channels = [channel for channel, _mode in self.channel_defaults]
        if len(channels) != len(set(channels)):
            raise PolicyError("policy channel defaults must not contain duplicates")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "channels": {
                channel.value: mode.value
                for channel, mode in sorted(self.channel_defaults, key=lambda item: item[0].value)
            },
            "scopes": [rule.as_dict() for rule in self.scope_rules],
        }
        if self.scenario is not None:
            result["scenario"] = self.scenario.value
        return result


DEFAULT_POLICY = PolicyConfig(channel_defaults=((EventChannel.NETWORK, PolicyMode.FREEZE),))

# Resolution is fully implemented for all three demo modes, but the replay
# engine only *enforces* `freeze`: it serves the recorded prefix and reports a
# mismatch for anything else. `strict-offline` and `passthrough` currently have
# no distinct behaviour at replay time (a live segment is requested with
# `--fork-at`, which bypasses policy entirely). Pinning a mode nothing honours
# into a cassette manifest would be a silent lie about how that cassette
# replays, so recording refuses it instead.
ENFORCED_MODES: frozenset[PolicyMode] = frozenset({PolicyMode.FREEZE})


def unenforced_modes(config: PolicyConfig) -> tuple[PolicyMode, ...]:
    """Return the declared modes replay cannot honour yet, in stable order."""

    declared: list[PolicyMode] = [mode for _channel, mode in config.channel_defaults]
    declared.extend(rule.mode for rule in config.scope_rules)
    if config.scenario is not None:
        declared.append(config.scenario)
    return tuple(
        sorted(
            {mode for mode in declared if mode not in ENFORCED_MODES},
            key=lambda mode: mode.value,
        )
    )


def require_enforceable(config: PolicyConfig) -> None:
    """Reject a policy whose declared modes replay would silently ignore."""

    unenforced = unenforced_modes(config)
    if unenforced:
        raise PolicyError(
            "policy mode(s) "
            + ", ".join(mode.value for mode in unenforced)
            + " are parsed but not enforced by the replay engine yet; only "
            + ", ".join(sorted(mode.value for mode in ENFORCED_MODES))
            + " may be pinned into a cassette"
        )


@dataclass(frozen=True)
class ResolvedPolicy:
    """The effective mode for one concrete channel and scope."""

    channel: EventChannel
    scope: str
    mode: PolicyMode | str
    source: PolicySource

    def __post_init__(self) -> None:
        if not self.scope:
            raise PolicyError("resolved policy scope must not be empty")
        if self.mode != LEGACY_MODE and not isinstance(self.mode, PolicyMode):
            raise PolicyError(f"unsupported resolved policy mode {self.mode!r}")
        if self.mode == LEGACY_MODE and self.source is not PolicySource.LEGACY:
            raise PolicyError("legacy policy mode must have the legacy source")

    def as_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel.value,
            "scope": self.scope,
            "mode": str(self.mode),
            "source": self.source.value,
        }


def _parse_channel(value: object, location: str) -> EventChannel:
    try:
        return EventChannel(value)
    except (TypeError, ValueError) as exc:
        supported = ", ".join(channel.value for channel in EventChannel)
        raise PolicyError(f"{location} must be one of: {supported}") from exc


def _parse_mode(value: object, location: str) -> PolicyMode:
    try:
        return PolicyMode(value)
    except (TypeError, ValueError) as exc:
        supported = ", ".join(mode.value for mode in PolicyMode)
        raise PolicyError(f"{location} must be one of: {supported}") from exc


def _parse_policy_table(
    table: object,
    *,
    include_built_in_defaults: bool,
) -> PolicyConfig:
    if not isinstance(table, dict):
        raise PolicyError("policy must be a TOML table")
    unknown = set(table) - {"scenario", "channels", "scopes"}
    if unknown:
        raise PolicyError(f"unknown policy field(s): {', '.join(sorted(unknown))}")

    defaults = dict(DEFAULT_POLICY.channel_defaults) if include_built_in_defaults else {}
    raw_channels = table.get("channels", {})
    if not isinstance(raw_channels, dict):
        raise PolicyError("policy.channels must be a TOML table")
    for raw_channel, raw_mode in raw_channels.items():
        channel = _parse_channel(raw_channel, f"policy.channels.{raw_channel}")
        defaults[channel] = _parse_mode(raw_mode, f"policy.channels.{raw_channel}")

    raw_scopes = table.get("scopes", [])
    if not isinstance(raw_scopes, list):
        raise PolicyError("policy.scopes must be an array of tables")
    scope_rules: list[ScopeRule] = []
    for index, raw_rule in enumerate(raw_scopes):
        location = f"policy.scopes[{index}]"
        if not isinstance(raw_rule, dict):
            raise PolicyError(f"{location} must be a table")
        unknown_rule = set(raw_rule) - {"channel", "scope", "mode"}
        if unknown_rule:
            raise PolicyError(f"unknown {location} field(s): {', '.join(sorted(unknown_rule))}")
        missing = {"channel", "scope", "mode"} - set(raw_rule)
        if missing:
            raise PolicyError(f"{location} is missing: {', '.join(sorted(missing))}")
        raw_scope = raw_rule["scope"]
        if not isinstance(raw_scope, str):
            raise PolicyError(f"{location}.scope must be a string")
        scope_rules.append(
            ScopeRule(
                channel=_parse_channel(raw_rule["channel"], f"{location}.channel"),
                scope=raw_scope,
                mode=_parse_mode(raw_rule["mode"], f"{location}.mode"),
            )
        )

    scenario = table.get("scenario")
    return PolicyConfig(
        channel_defaults=tuple(sorted(defaults.items(), key=lambda item: item[0].value)),
        scope_rules=tuple(scope_rules),
        scenario=(_parse_mode(scenario, "policy.scenario") if scenario is not None else None),
    )


def load_policy(path: Path | None) -> PolicyConfig:
    """Load project policy, applying the safe network default for new records."""

    if path is None:
        return DEFAULT_POLICY
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"cannot load {path}: {exc}") from exc
    if "policy" not in document:
        return DEFAULT_POLICY
    return _parse_policy_table(document["policy"], include_built_in_defaults=True)


def resolve_policy(
    config: PolicyConfig,
    *,
    channel: EventChannel | str,
    scope: str,
    cli_mode: PolicyMode | str | None = None,
    scenario_mode: PolicyMode | str | None = None,
) -> ResolvedPolicy:
    """Resolve CLI → scenario → scope rule → channel default → legacy."""

    parsed_channel = _parse_channel(channel, "policy channel")
    if not isinstance(scope, str) or not scope:
        raise PolicyError("policy scope must be a non-empty string")
    if cli_mode is not None:
        return ResolvedPolicy(
            parsed_channel,
            scope,
            _parse_mode(cli_mode, "CLI policy mode"),
            PolicySource.CLI,
        )
    selected_scenario = scenario_mode if scenario_mode is not None else config.scenario
    if selected_scenario is not None:
        return ResolvedPolicy(
            parsed_channel,
            scope,
            _parse_mode(selected_scenario, "scenario policy mode"),
            PolicySource.SCENARIO,
        )
    for rule in config.scope_rules:
        if rule.matches(parsed_channel, scope):
            return ResolvedPolicy(parsed_channel, scope, rule.mode, PolicySource.SCOPE_RULE)
    for default_channel, mode in config.channel_defaults:
        if default_channel is parsed_channel:
            return ResolvedPolicy(parsed_channel, scope, mode, PolicySource.CHANNEL_DEFAULT)
    return ResolvedPolicy(parsed_channel, scope, LEGACY_MODE, PolicySource.LEGACY)


def _canonical_policy_payload(
    config: PolicyConfig,
    resolutions: Iterable[ResolvedPolicy],
) -> dict[str, Any]:
    ordered = sorted(
        (resolution.as_dict() for resolution in resolutions),
        key=lambda item: (item["channel"], item["scope"]),
    )
    return {"config": config.as_dict(), "resolved": ordered}


def policy_hash(
    config: PolicyConfig,
    resolutions: Iterable[ResolvedPolicy],
) -> str:
    """Hash policy semantics with stable ordering and canonical JSON."""

    payload = _canonical_policy_payload(config, resolutions)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_policy_manifest(
    config: PolicyConfig,
    resolutions: Iterable[ResolvedPolicy],
) -> dict[str, Any]:
    """Build the self-contained policy block pinned into a cassette."""

    materialized = tuple(resolutions)
    payload = _canonical_policy_payload(config, materialized)
    return {
        "version": POLICY_VERSION,
        "hash": policy_hash(config, materialized),
        **payload,
    }


def _resolved_from_dict(value: object, index: int) -> ResolvedPolicy:
    location = f"manifest policy.resolved[{index}]"
    if not isinstance(value, dict):
        raise PolicyError(f"{location} must be an object")
    if set(value) != {"channel", "scope", "mode", "source"}:
        raise PolicyError(f"{location} must contain exactly channel, scope, mode, and source")
    channel = _parse_channel(value["channel"], f"{location}.channel")
    scope = value["scope"]
    if not isinstance(scope, str):
        raise PolicyError(f"{location}.scope must be a string")
    raw_mode = value["mode"]
    if raw_mode == LEGACY_MODE:
        mode: PolicyMode | str = LEGACY_MODE
    else:
        mode = _parse_mode(raw_mode, f"{location}.mode")
    try:
        source = PolicySource(value["source"])
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{location}.source is invalid") from exc
    return ResolvedPolicy(channel, scope, mode, source)


def validate_policy_manifest(
    manifest: Mapping[str, Any],
) -> tuple[PolicyConfig, tuple[ResolvedPolicy, ...]] | None:
    """Validate pinned policy integrity; legacy manifests intentionally omit it."""

    raw_policy = manifest.get("policy")
    if raw_policy is None:
        return None
    if not isinstance(raw_policy, dict):
        raise PolicyError("manifest policy must be an object")
    if set(raw_policy) != {"version", "hash", "config", "resolved"}:
        raise PolicyError(
            "manifest policy must contain exactly version, hash, config, and resolved"
        )
    if isinstance(raw_policy["version"], bool) or raw_policy["version"] != POLICY_VERSION:
        raise PolicyError(f"unsupported manifest policy version {raw_policy['version']!r}")
    config = _parse_policy_table(raw_policy["config"], include_built_in_defaults=False)
    raw_resolved = raw_policy["resolved"]
    if not isinstance(raw_resolved, list):
        raise PolicyError("manifest policy.resolved must be an array")
    resolutions = tuple(
        _resolved_from_dict(value, index) for index, value in enumerate(raw_resolved)
    )
    keys = [(resolution.channel, resolution.scope) for resolution in resolutions]
    if len(keys) != len(set(keys)):
        raise PolicyError("manifest policy.resolved contains duplicate scopes")
    expected_hash = policy_hash(config, resolutions)
    if raw_policy["hash"] != expected_hash:
        raise PolicyError("manifest policy hash does not match its contents")
    for resolution in resolutions:
        expected = resolve_policy(config, channel=resolution.channel, scope=resolution.scope)
        if resolution != expected:
            raise PolicyError("manifest policy.resolved does not agree with its pinned config")
    return config, resolutions


def resolve_manifest_policy(
    manifest: Mapping[str, Any],
    *,
    channel: EventChannel | str,
    scope: str,
) -> ResolvedPolicy:
    """Resolve from pinned metadata, or legacy for an old policy-less cassette."""

    validated = validate_policy_manifest(manifest)
    parsed_channel = _parse_channel(channel, "policy channel")
    if validated is None:
        return ResolvedPolicy(parsed_channel, scope, LEGACY_MODE, PolicySource.LEGACY)
    config, resolutions = validated
    for resolution in resolutions:
        if resolution.channel is parsed_channel and resolution.scope == scope:
            return resolution
    return resolve_policy(config, channel=parsed_channel, scope=scope)
