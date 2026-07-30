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
  forkResult: ForkResult | null;
};

export type ForkResult = {
  version: number;
  mode: "hybrid";
  fork_at: number;
  exit_code: number;
  segments: {
    pinned: {
      target_flow_count: number;
      served_flow_count: number;
      estimated_cost_usd: number;
    };
    live: {
      request_count: number;
      response_count: number;
      error_count: number;
      flow_count: number;
      model_calls: number;
      models: string[];
      tokens: Record<string, number> | null;
      estimated_cost_usd: number | null;
      wall_time_seconds: number;
    };
  };
  timing: { wall_time_seconds: number };
  downstream: {
    matches: boolean;
    exit_code: { matches: boolean; baseline: number; candidate: number };
    stdout: { matches: boolean };
    workspace: {
      matches: boolean;
      diff: { added: string[]; removed: string[]; changed: string[] };
    };
    tool_calls: {
      matches: boolean;
      baseline_count: number;
      candidate_count: number;
      summary: { insert: number; delete: number; substitute: number };
    };
    similarity: {
      kind: "lexical_structural";
      score: number;
      threshold: number;
      passes: boolean;
      components: {
        transcript_lexical: number;
        tool_sequence: number;
        output_files: number;
      };
      weights: Record<string, number>;
    };
  };
  events: TimelineEvent[];
};
