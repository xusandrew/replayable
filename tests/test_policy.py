from __future__ import annotations

import json

import pytest

from replayable.cassette.events import EventChannel
from replayable.core.policy import (
    DEFAULT_POLICY,
    LEGACY_MODE,
    PolicyConfig,
    PolicyError,
    PolicyMode,
    PolicySource,
    ResolvedPolicy,
    ScopeRule,
    build_policy_manifest,
    load_policy,
    policy_hash,
    resolve_manifest_policy,
    resolve_policy,
    validate_policy_manifest,
)


def configured_policy() -> PolicyConfig:
    return PolicyConfig(
        channel_defaults=((EventChannel.NETWORK, PolicyMode.FREEZE),),
        scope_rules=(
            ScopeRule(
                EventChannel.NETWORK,
                "*.example.com",
                PolicyMode.PASSTHROUGH,
            ),
        ),
        scenario=PolicyMode.STRICT_OFFLINE,
    )


@pytest.mark.parametrize(
    ("cli", "scenario", "config", "channel", "scope", "mode", "source"),
    [
        (
            PolicyMode.PASSTHROUGH,
            PolicyMode.FREEZE,
            configured_policy(),
            EventChannel.NETWORK,
            "api.example.com",
            PolicyMode.PASSTHROUGH,
            PolicySource.CLI,
        ),
        (
            None,
            PolicyMode.FREEZE,
            configured_policy(),
            EventChannel.NETWORK,
            "api.example.com",
            PolicyMode.FREEZE,
            PolicySource.SCENARIO,
        ),
        (
            None,
            None,
            configured_policy(),
            EventChannel.NETWORK,
            "api.example.com",
            PolicyMode.STRICT_OFFLINE,
            PolicySource.SCENARIO,
        ),
        (
            None,
            None,
            PolicyConfig(
                channel_defaults=((EventChannel.NETWORK, PolicyMode.FREEZE),),
                scope_rules=(
                    ScopeRule(
                        EventChannel.NETWORK,
                        "*.example.com",
                        PolicyMode.PASSTHROUGH,
                    ),
                ),
            ),
            EventChannel.NETWORK,
            "api.example.com",
            PolicyMode.PASSTHROUGH,
            PolicySource.SCOPE_RULE,
        ),
        (
            None,
            None,
            DEFAULT_POLICY,
            EventChannel.NETWORK,
            "other.test",
            PolicyMode.FREEZE,
            PolicySource.CHANNEL_DEFAULT,
        ),
        (
            None,
            None,
            PolicyConfig(channel_defaults=()),
            EventChannel.TOOL,
            "search",
            LEGACY_MODE,
            PolicySource.LEGACY,
        ),
    ],
)
def test_policy_precedence(cli, scenario, config, channel, scope, mode, source):
    resolved = resolve_policy(
        config,
        channel=channel,
        scope=scope,
        cli_mode=cli,
        scenario_mode=scenario,
    )

    assert resolved.mode == mode
    assert resolved.source is source


def test_scope_rules_use_case_sensitive_globs_and_declaration_order():
    config = PolicyConfig(
        channel_defaults=((EventChannel.NETWORK, PolicyMode.FREEZE),),
        scope_rules=(
            ScopeRule(EventChannel.NETWORK, "*.example.com", PolicyMode.STRICT_OFFLINE),
            ScopeRule(EventChannel.NETWORK, "api.example.com", PolicyMode.PASSTHROUGH),
        ),
    )

    assert (
        resolve_policy(config, channel=EventChannel.NETWORK, scope="api.example.com").mode
        is PolicyMode.STRICT_OFFLINE
    )
    assert (
        resolve_policy(config, channel=EventChannel.NETWORK, scope="API.EXAMPLE.COM").mode
        is PolicyMode.FREEZE
    )


def test_load_policy_merges_explicit_channels_with_safe_network_default(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text(
        """
[policy]
scenario = "strict-offline"

[policy.channels]
tool = "passthrough"

[[policy.scopes]]
channel = "network"
scope = "api.example.com"
mode = "freeze"
""".strip(),
        encoding="utf-8",
    )

    loaded = load_policy(path)

    assert dict(loaded.channel_defaults) == {
        EventChannel.NETWORK: PolicyMode.FREEZE,
        EventChannel.TOOL: PolicyMode.PASSTHROUGH,
    }
    assert loaded.scenario is PolicyMode.STRICT_OFFLINE
    assert loaded.scope_rules[0].scope == "api.example.com"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ('[policy]\nscenario = "legacy"\n', "must be one of"),
        ('[policy.channels]\nnetwrok = "freeze"\n', "must be one of"),
        ('[policy.channels]\nnetwork = "record-if-missing"\n', "must be one of"),
        ("[policy]\nunknown = true\n", "unknown policy field"),
        ('[policy]\nscopes = "network"\n', "array of tables"),
        (
            '[[policy.scopes]]\nchannel = "network"\nscope = ""\nmode = "freeze"\n',
            "must not be empty",
        ),
    ],
)
def test_invalid_policy_configuration_is_rejected(tmp_path, document, message):
    path = tmp_path / "replayable.toml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(PolicyError, match=message):
        load_policy(path)


def test_policy_hash_is_stable_across_resolution_order_and_changes_with_behavior():
    first = ResolvedPolicy(
        EventChannel.NETWORK,
        "a.example",
        PolicyMode.FREEZE,
        PolicySource.CHANNEL_DEFAULT,
    )
    second = ResolvedPolicy(
        EventChannel.NETWORK,
        "b.example",
        PolicyMode.PASSTHROUGH,
        PolicySource.SCOPE_RULE,
    )

    assert policy_hash(DEFAULT_POLICY, [first, second]) == policy_hash(
        DEFAULT_POLICY, [second, first]
    )
    changed = ResolvedPolicy(
        EventChannel.NETWORK,
        "b.example",
        PolicyMode.STRICT_OFFLINE,
        PolicySource.SCOPE_RULE,
    )
    assert policy_hash(DEFAULT_POLICY, [first, second]) != policy_hash(
        DEFAULT_POLICY, [first, changed]
    )


def test_policy_manifest_round_trip_and_legacy_fallback():
    resolution = resolve_policy(
        DEFAULT_POLICY, channel=EventChannel.NETWORK, scope="api.example.com"
    )
    manifest = {"policy": build_policy_manifest(DEFAULT_POLICY, [resolution])}

    config, resolutions = validate_policy_manifest(manifest) or (None, ())

    assert config == DEFAULT_POLICY
    assert resolutions == (resolution,)
    assert (
        resolve_manifest_policy(manifest, channel=EventChannel.NETWORK, scope="api.example.com")
        == resolution
    )
    legacy = resolve_manifest_policy({}, channel=EventChannel.NETWORK, scope="api.example.com")
    assert legacy.mode == LEGACY_MODE
    assert legacy.source is PolicySource.LEGACY


def test_policy_manifest_resolves_unobserved_scope_from_pinned_config():
    config = PolicyConfig(channel_defaults=((EventChannel.NETWORK, PolicyMode.STRICT_OFFLINE),))
    manifest = {"policy": build_policy_manifest(config, [])}

    resolved = resolve_manifest_policy(manifest, channel=EventChannel.NETWORK, scope="new.example")

    assert resolved.mode is PolicyMode.STRICT_OFFLINE
    assert resolved.source is PolicySource.CHANNEL_DEFAULT


def test_policy_manifest_detects_tampering():
    resolution = resolve_policy(
        DEFAULT_POLICY, channel=EventChannel.NETWORK, scope="api.example.com"
    )
    manifest = {"policy": build_policy_manifest(DEFAULT_POLICY, [resolution])}
    tampered = json.loads(json.dumps(manifest))
    tampered["policy"]["resolved"][0]["mode"] = "passthrough"

    with pytest.raises(PolicyError, match="hash"):
        validate_policy_manifest(tampered)


def test_policy_manifest_rejects_a_self_consistent_but_incorrect_resolution():
    resolution = ResolvedPolicy(
        EventChannel.NETWORK,
        "api.example.com",
        PolicyMode.PASSTHROUGH,
        PolicySource.SCOPE_RULE,
    )
    manifest = {"policy": build_policy_manifest(DEFAULT_POLICY, [resolution])}

    with pytest.raises(PolicyError, match="does not agree"):
        validate_policy_manifest(manifest)


def test_policy_manifest_rejects_duplicate_scope_entries():
    resolution = resolve_policy(
        DEFAULT_POLICY, channel=EventChannel.NETWORK, scope="api.example.com"
    )
    policy = build_policy_manifest(DEFAULT_POLICY, [resolution, resolution])

    with pytest.raises(PolicyError, match="duplicate"):
        validate_policy_manifest({"policy": policy})
