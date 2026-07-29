from __future__ import annotations

import json

import pytest

from replayable.matcher import (
    RawRequest,
    RecordedEntry,
    ReplayMismatch,
    RequestMatcher,
    canonicalize_json,
    normalize_request,
)
from replayable.normalize_rules import (
    VOLATILE_SENTINEL,
    NormalizationRules,
    load_rules,
)


def request(
    body: object = None,
    *,
    method: str = "POST",
    host: str = "API.EXAMPLE.COM",
    port: int = 443,
    path: str = "/v1/messages",
    query: str = "",
    headers: list[list[str]] | None = None,
    scheme: str = "https",
) -> RawRequest:
    if isinstance(body, bytes):
        encoded = body
    else:
        encoded = json.dumps(body if body is not None else {}).encode()
    return RawRequest(
        method=method,
        host=host,
        port=port,
        path=path,
        query=query,
        headers=headers or [["content-type", "application/json"]],
        body=encoded,
        scheme=scheme,
    )


def entry(sequence: int, raw: RawRequest, response: str) -> RecordedEntry:
    return RecordedEntry(
        sequence=sequence,
        flow={"seq": sequence, "response_name": response},
        normalized=normalize_request(raw),
    )


def test_method_host_and_default_port_are_normalized():
    first = normalize_request(request(method="post", host="API.EXAMPLE.COM"))
    second = normalize_request(request(method="POST", host="api.example.com"))
    assert first.match_key == second.match_key
    assert first.host == "api.example.com"


def test_nondefault_port_remains_behavioral():
    default = normalize_request(request(port=443))
    custom = normalize_request(request(port=8443))
    assert default.match_key != custom.match_key
    assert custom.host.endswith(":8443")


def test_query_parameters_are_key_order_insensitive():
    first = normalize_request(request(query="z=last&a=first&a=second"))
    second = normalize_request(request(query="a=first&a=second&z=last"))
    assert first.match_key == second.match_key
    assert first.query == "a=first&a=second&z=last"


def test_json_object_key_order_is_irrelevant():
    first = normalize_request(request({"temperature": 0.7, "prompt": "hello"}))
    second = normalize_request(request({"prompt": "hello", "temperature": 0.7}))
    assert first.match_key == second.match_key


def test_tool_call_uuid_values_match_across_runs():
    first = normalize_request(
        request({"tool_call_id": "93f56ea8-7f39-4cce-a231-e52f32160c2e"})
    )
    second = normalize_request(
        request({"tool_call_id": "59a8ef47-33f0-4529-8431-1a925c41c166"})
    )
    assert first.match_key == second.match_key
    assert VOLATILE_SENTINEL in first.canonical_body


def test_iso_datetime_values_are_volatile_even_under_other_field_names():
    first = normalize_request(request({"metadata": "2026-07-14T02:00:00Z"}))
    second = normalize_request(request({"metadata": "2026-08-20T03:04:05+00:00"}))
    assert first.match_key == second.match_key


def test_epoch_strings_only_normalize_for_time_like_keys():
    normalized, _ = canonicalize_json(
        {"event_time": "1753020202", "count": "1753020202"},
        NormalizationRules(),
    )
    parsed = json.loads(normalized)
    assert parsed["event_time"] == VOLATILE_SENTINEL
    assert parsed["count"] == "1753020202"


def test_user_visible_prompt_change_does_not_match():
    matcher = RequestMatcher([entry(1, request({"prompt": "write a poem"}), "one")], NormalizationRules())
    with pytest.raises(ReplayMismatch) as captured:
        matcher.match(request({"prompt": "write a story"}))
    assert "poem" in captured.value.payload["diff"]
    assert "story" in captured.value.payload["diff"]


def test_identical_requests_pop_distinct_responses_fifo():
    raw = request({"prompt": "same"})
    matcher = RequestMatcher(
        [entry(1, raw, "first"), entry(2, raw, "second")],
        NormalizationRules(),
    )
    assert matcher.match(raw)["response_name"] == "first"
    assert matcher.match(raw)["response_name"] == "second"


def test_unconsumed_sequences_update_after_each_match():
    raw = request({"prompt": "same"})
    matcher = RequestMatcher(
        [entry(1, raw, "first"), entry(2, raw, "second")],
        NormalizationRules(),
    )
    assert matcher.unconsumed_sequences() == [1, 2]
    matcher.match(raw)
    assert matcher.unconsumed_sequences() == [2]


def test_preserve_override_exempts_default_volatile_field():
    rules = NormalizationRules(preserve=("id",))
    first = normalize_request(request({"id": "first"}), rules)
    second = normalize_request(request({"id": "second"}), rules)
    assert first.match_key != second.match_key


def test_nested_volatile_fields_at_depth_four_are_normalized():
    first = request(
        {"messages": [{"content": [{"tool": {"tool_use_id": "dynamic-one"}}]}]}
    )
    second = request(
        {"messages": [{"content": [{"tool": {"tool_use_id": "dynamic-two"}}]}]}
    )
    assert normalize_request(first).match_key == normalize_request(second).match_key


def test_override_adds_field_name_and_regex(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text(
        """
[normalization]
field_names = ["session_marker"]
regexes = ["^run-[0-9]+$"]
""",
        encoding="utf-8",
    )
    rules = load_rules(path)
    first = normalize_request(
        request({"session_marker": "one", "run": "run-100"}),
        rules,
    )
    second = normalize_request(
        request({"session_marker": "two", "run": "run-200"}),
        rules,
    )
    assert first.match_key == second.match_key


def test_toml_preserve_override_keeps_default_field(tmp_path):
    path = tmp_path / "replayable.toml"
    path.write_text('[normalization]\npreserve = ["id"]\n', encoding="utf-8")
    rules = load_rules(path)
    first = normalize_request(request({"id": "first"}), rules)
    second = normalize_request(request({"id": "second"}), rules)
    assert first.match_key != second.match_key


def test_ruleset_version_changes_with_overrides():
    defaults = NormalizationRules()
    custom = NormalizationRules(field_names=(*defaults.field_names, "custom"))
    assert defaults.version != custom.version


def test_invalid_json_uses_body_hash_without_guessing():
    first = normalize_request(request(b"{bad json"))
    second = normalize_request(request(b"{different bad json"))
    assert first.match_key != second.match_key
    assert first.canonical_body == first.diff_body.removeprefix("body_sha256: ")


def test_non_json_body_uses_hash():
    headers = [["content-type", "text/plain"]]
    first = normalize_request(request(b"hello", headers=headers))
    second = normalize_request(request(b"goodbye", headers=headers))
    assert first.match_key != second.match_key


def test_headers_are_excluded_from_matching():
    first = request(
        {"prompt": "same"},
        headers=[
            ["content-type", "application/json"],
            ["authorization", "Bearer first"],
        ],
    )
    second = request(
        {"prompt": "same"},
        headers=[
            ["content-type", "application/json"],
            ["authorization", "Bearer second"],
            ["user-agent", "different"],
        ],
    )
    assert normalize_request(first).match_key == normalize_request(second).match_key


def test_float_canonicalization_is_stable():
    normalized = normalize_request(request({"temperature": 0.7}))
    assert '"temperature":0.7' in normalized.canonical_body
    canonical_again = normalize_request(
        request(json.loads(normalized.canonical_body))
    )
    assert canonical_again.match_key == normalized.match_key


def test_nearest_candidates_are_limited_to_three_and_sorted_by_diff_size():
    entries = [
        entry(index, request({"prompt": prompt}), prompt)
        for index, prompt in enumerate(
            ["almost same", "very different words here", "other", "fourth"],
            start=1,
        )
    ]
    matcher = RequestMatcher(entries, NormalizationRules())
    with pytest.raises(ReplayMismatch) as captured:
        matcher.match(request({"prompt": "almost same!"}))
    candidates = captured.value.payload["nearest_candidates"]
    assert len(candidates) == 3
    assert candidates[0]["seq"] == 1


def test_nearest_candidates_prefer_unconsumed_flows():
    entries = [
        entry(1, request({"prompt": "first"}), "first"),
        entry(2, request({"prompt": "second"}), "second"),
        entry(3, request({"prompt": "third"}), "third"),
    ]
    matcher = RequestMatcher(entries, NormalizationRules())
    assert matcher.match(request({"prompt": "first"}))["response_name"] == "first"
    with pytest.raises(ReplayMismatch) as captured:
        matcher.match(request({"prompt": "missing"}))
    sequences = [candidate["seq"] for candidate in captured.value.payload["nearest_candidates"]]
    assert 1 not in sequences
    assert sequences == [2, 3]


def test_preserved_parent_still_normalizes_nested_volatile_fields():
    rules = NormalizationRules(preserve=("metadata",))
    first = normalize_request(
        request({"metadata": {"id": "first", "label": "same"}}),
        rules,
    )
    second = normalize_request(
        request({"metadata": {"id": "second", "label": "same"}}),
        rules,
    )
    assert first.match_key == second.match_key


@pytest.mark.parametrize(
    "value",
    [
        {"id": "dynamic", "prompt": "stable"},
        {"nested": [{"created_at": "2026-07-14T02:00:00Z"}]},
        {"temperature": 0.7, "enabled": True, "nothing": None},
        {"event_time": "1753020202", "items": [3, 2, 1]},
    ],
)
def test_json_normalization_is_idempotent_and_canonical(value):
    first, _ = canonicalize_json(value, NormalizationRules())
    second, _ = canonicalize_json(json.loads(first), NormalizationRules())
    assert second == first
