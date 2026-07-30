import type { CassetteSummary, RunData } from "./types";

export const demoCassettes: CassetteSummary[] = [
  {
    name: "research-agent",
    flow_count: 7,
    created_at: "2026-07-29T08:11:54Z",
    image: {
      ref: "replayable/research-agent:local",
      digest: "sha256:cd398ef53ea7",
    },
    status: "mismatch",
    last_exit_code: 2,
    has_observation: true,
    has_fork_result: false,
  },
  {
    name: "support-triage",
    flow_count: 12,
    created_at: "2026-07-28T19:44:02Z",
    image: {
      ref: "replayable/support-agent:local",
      digest: "sha256:776eaacb902",
    },
    status: "replayable",
    last_exit_code: 0,
    has_observation: true,
    has_fork_result: false,
  },
  {
    name: "invoice-review",
    flow_count: 5,
    created_at: "2026-07-26T14:23:09Z",
    image: {
      ref: "replayable/invoice-agent:local",
      digest: "sha256:1bb40cd0f88",
    },
    status: "replayable",
    last_exit_code: 0,
    has_observation: true,
    has_fork_result: false,
  },
];

const baseEvents = Array.from({ length: 7 }, (_, index) => ({
  seq: index + 1,
  lamport: index + 1,
  t_rel: [0, 0.36, 0.91, 1.18, 1.54, 1.86, 2.12][index],
  channel: index === 0 || index === 3 ? "model" : "network",
  kind: index === 0 || index === 3 ? "model.call" : "http.exchange",
  scope: index === 0 || index === 3 ? "api.anthropic.com" : "hn.algolia.com",
  key:
    index === 0 || index === 3
      ? `POST api.anthropic.com:443/v1/messages`
      : `GET hn.algolia.com:443/api/v1/search`,
  duration_seconds: [0.31, 0.48, 0.22, 0.34, 0.27, 0.31, 0.29][index],
  stream_chunk_count: index === 0 || index === 3 ? 28 : 0,
}));

export const demoRun: RunData = {
  timeline: baseEvents,
  flow: {
    seq: 3,
    key: {
      method: "POST",
      host: "api.anthropic.com",
      port: 443,
      path: "/v1/messages",
    },
    request: {
      query: "",
      headers: [["content-type", "application/json"]],
      body_decoded: JSON.stringify(
        {
          model: "claude-haiku-4-5",
          system:
            "You are a concise research agent. Research the user's topic and prepare a fact-based report.",
          request_id: "req_018fa2",
          max_tokens: 700,
        },
        null,
        2,
      ),
    },
    response: {
      status: 200,
      headers: [["content-type", "text/event-stream"]],
      body_decoded: "",
    },
    timing: { started: 0.91, completed: 1.13 },
  },
  mismatch: {
    live_request: {
      method: "POST",
      host: "api.anthropic.com",
      path: "/v1/messages",
      canonical_body: JSON.stringify(
        {
          model: "claude-haiku-4-5",
          system:
            "You are a verbose research agent. Research the user's topic and prepare a fact-based report.",
          request_id: "§VOLATILE§",
          max_tokens: 700,
        },
        null,
        2,
      ),
    },
    nearest_candidates: [{ seq: 3 }],
    diff:
      '-  "system": "You are a concise research agent."\n+  "system": "You are a verbose research agent."',
  },
  explain: {
    flow: 3,
    match_key: "5903c87f24b6d3dc",
    pre_hash: "POST\napi.anthropic.com\n/v1/messages\n\n{...}",
    canonical_body: '{"request_id":"§VOLATILE§"}',
    diff_body: '{\n  "request_id": "§VOLATILE§"\n}',
    rules: {
      version: "sha256:1041e721",
      field_names: ["request_id", "tool_call_id", "timestamp", "nonce"],
      value_patterns: [],
      preserve: [],
    },
  },
  forkResult: null,
};
