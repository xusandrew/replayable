export type CassetteSummary = {
  name: string;
  flow_count: number;
  created_at: string;
  image: { ref: string; digest: string };
  status: "mismatch" | "replayable";
  last_exit_code: number | null;
  has_observation: boolean;
  has_fork_result: boolean;
};

export type TimelineEvent = {
  seq: number;
  lamport: number;
  t_rel: number;
  channel: string;
  kind: string;
  scope: string;
  key: string;
  duration_seconds: number;
  stream_chunk_count: number;
  metrics?: {
    model?: string;
    estimated_cost_usd?: number;
    tokens?: Record<string, number>;
  };
};

export type FlowDetail = {
  seq: number;
  key: { method: string; host: string; port: number; path: string };
  request: {
    query: string;
    headers: string[][];
    body_decoded: string;
  };
  response: {
    status: number;
    headers: string[][];
    body_decoded: string;
    sse_chunks?: Array<Record<string, string>>;
  };
  timing: { started: number; completed: number };
};

export type Mismatch = {
  live_request: {
    method: string;
    host?: string;
    path: string;
    query?: string;
    canonical_body?: string;
    match_key?: string;
  };
  nearest_candidates: Array<{ seq: number }>;
  diff: string;
};

export type Explain = {
  flow: number;
  match_key: string;
  pre_hash: string;
  canonical_body: string;
  diff_body: string;
  rules: {
    version: string;
    field_names: string[];
    value_patterns: string[];
    preserve: string[];
  };
};

export type RunData = {
  timeline: TimelineEvent[];
  flow: FlowDetail | null;
  mismatch: Mismatch | null;
  explain: Explain | null;
};
