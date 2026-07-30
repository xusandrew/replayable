import { describe, expect, it } from "vitest";
import { changedFields, diffPanes, prettyCanonicalBody } from "./normalized-body";
import type { Explain, FlowDetail, Mismatch } from "./types";

const RECORDED_CANONICAL =
  '{"max_tokens":700,"messages":[{"content":"hi","role":"user"}],"request_id":"§VOLATILE§","system":"concise"}';
const LIVE_CANONICAL =
  '{"max_tokens":700,"messages":[{"content":"hi","role":"user"}],"request_id":"§VOLATILE§","system":"verbose"}';

// The raw recorded body: same request, but original key order and the real
// volatile value. Diffing this against a canonical body is the bug this
// module exists to prevent.
const RAW_BODY = JSON.stringify(
  {
    model: "claude-haiku-4-5",
    request_id: "req_018fa2",
    messages: [{ role: "user", content: "hi" }],
    max_tokens: 700,
    system: "concise",
  },
  null,
  2,
);

const flow = {
  seq: 3,
  key: { method: "POST", host: "api.anthropic.com", port: 443, path: "/v1/messages" },
  request: { query: "", headers: [], body_decoded: RAW_BODY },
  response: { status: 200, headers: [], body_decoded: "" },
  timing: { started: 0.9, completed: 1.1 },
} satisfies FlowDetail;

const explain = {
  flow: 3,
  match_key: "abc",
  pre_hash: "POST\napi.anthropic.com\n/v1/messages\n\n{}",
  canonical_body: RECORDED_CANONICAL,
  diff_body: "{}",
  rules: { version: "sha256:1", field_names: ["request_id"], value_patterns: [], preserve: [] },
} satisfies Explain;

const mismatch = {
  live_request: {
    method: "POST",
    host: "api.anthropic.com",
    path: "/v1/messages",
    canonical_body: LIVE_CANONICAL,
  },
  nearest_candidates: [{ seq: 3 }],
  diff: "",
} satisfies Mismatch;

describe("prettyCanonicalBody", () => {
  it("re-indents canonical JSON and leaves a body digest alone", () => {
    expect(prettyCanonicalBody('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(prettyCanonicalBody("e3b0c44298fc1c14")).toBe("e3b0c44298fc1c14");
    expect(prettyCanonicalBody("{not json")).toBe("{not json");
  });
});

describe("diffPanes", () => {
  it("compares the matcher's view of both sides, never raw against canonical", () => {
    const panes = diffPanes(flow, mismatch, explain);

    expect(panes.normalized).toBe(true);
    // The raw recorded body must not leak into the comparison: its key order
    // and its real request_id would show up as behavioural changes.
    expect(panes.recorded).not.toContain("req_018fa2");
    expect(panes.recorded).toContain("§VOLATILE§");
    expect(panes.recorded).toContain("concise");
    expect(panes.live).toContain("verbose");
    // Only the system line differs once both sides are normalized.
    const left = panes.recorded.split("\n");
    const right = panes.live.split("\n");
    expect(left.length).toBe(right.length);
    expect(left.filter((line, index) => line !== right[index])).toHaveLength(1);
  });

  it("shows the recorded request on both sides when there is no mismatch", () => {
    const panes = diffPanes(flow, null, explain);

    expect(panes.normalized).toBe(false);
    expect(panes.recorded).toBe(RAW_BODY);
    expect(panes.live).toBe(RAW_BODY);
  });

  it("shows an unmatched live request when no recorded candidate exists", () => {
    const panes = diffPanes(flow, mismatch, null);

    expect(panes.normalized).toBe(false);
    expect(panes.recorded).toBe("No recorded request is available.");
    expect(panes.live).toContain("verbose");
  });

  it("survives an empty cassette selection", () => {
    expect(diffPanes(null, null, null)).toEqual({
      recorded: "",
      live: "",
      normalized: false,
    });
  });
});

describe("changedFields", () => {
  it("names only the fields that survived normalization and still differ", () => {
    expect(
      changedFields(
        prettyCanonicalBody(RECORDED_CANONICAL),
        prettyCanonicalBody(LIVE_CANONICAL),
      ),
    ).toEqual(["system"]);
  });

  it("reports added and removed fields as changed", () => {
    expect(changedFields('{"a":1}', '{"b":2}')).toEqual(["a", "b"]);
  });

  it("returns nothing for identical or non-object bodies", () => {
    expect(changedFields('{"a":1}', '{"a":1}')).toEqual([]);
    expect(changedFields("body_sha256: abc", "body_sha256: def")).toEqual([]);
    expect(changedFields("[1]", "[2]")).toEqual([]);
  });
});
