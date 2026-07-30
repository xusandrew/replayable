import type {
  CassetteSummary,
  Explain,
  FlowDetail,
  ForkResult,
  Mismatch,
  TimelineEvent,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      typeof payload.error === "string"
        ? payload.error
        : `Request failed with HTTP ${response.status}`,
    );
  }
  return payload as T;
}

export async function listCassettes(): Promise<CassetteSummary[]> {
  const result = await request<{ cassettes: CassetteSummary[] }>("/api/cassettes");
  return result.cassettes;
}

export async function loadTimeline(name: string): Promise<TimelineEvent[]> {
  const result = await request<{ events: TimelineEvent[] }>(
    `/api/cassettes/${encodeURIComponent(name)}/timeline`,
  );
  return result.events;
}

export function loadFlow(name: string, sequence: number): Promise<FlowDetail> {
  return request(`/api/cassettes/${encodeURIComponent(name)}/flows/${sequence}`);
}

export function loadMismatch(name: string): Promise<Mismatch> {
  return request(`/api/cassettes/${encodeURIComponent(name)}/mismatch`);
}

export function loadExplain(name: string, sequence: number): Promise<Explain> {
  return request(
    `/api/cassettes/${encodeURIComponent(name)}/explain?flow=${sequence}`,
  );
}

export function loadForkResult(name: string): Promise<ForkResult> {
  return request(`/api/cassettes/${encodeURIComponent(name)}/fork-result`);
}

export async function runReplay(name: string, strict: boolean): Promise<number> {
  const result = await request<{ exit_code: number }>(
    `/api/cassettes/${encodeURIComponent(name)}/replay`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ strict }),
    },
  );
  return result.exit_code;
}

export async function recordFreshBaseline(
  name: string,
  destination: string,
  envFile: string,
): Promise<number> {
  const result = await request<{ exit_code: number }>(
    `/api/cassettes/${encodeURIComponent(name)}/accept`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        destination,
        env_file: envFile || null,
      }),
    },
  );
  return result.exit_code;
}

export async function runFork(
  name: string,
  forkAt: number,
  envFile: string,
): Promise<number> {
  const result = await request<{ exit_code: number }>(
    `/api/cassettes/${encodeURIComponent(name)}/fork`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        fork_at: forkAt,
        env_file: envFile || null,
      }),
    },
  );
  return result.exit_code;
}
