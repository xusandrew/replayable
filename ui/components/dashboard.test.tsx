import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "./dashboard";
import { demoCassettes, demoRun } from "@/lib/demo";
import { eventState, hybridTimeline, Timeline } from "./timeline";
import { TokenDiff } from "./token-diff";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("TokenDiff", () => {
  it("highlights the changed token on each side", () => {
    render(
      <TokenDiff
        live={'{"system":"verbose"}'}
        recorded={'{"system":"concise"}'}
      />,
    );

    expect(screen.getByTestId("removed-pane").querySelector(".token-removed")).toHaveTextContent(
      "concise",
    );
    expect(screen.getByTestId("added-pane").querySelector(".token-added")).toHaveTextContent(
      "verbose",
    );
  });
});

describe("timeline state", () => {
  it("marks the boundary and every unexecuted event distinctly", () => {
    expect(eventState(2, 3)).toBe("served");
    expect(eventState(3, 3)).toBe("mismatch");
    expect(eventState(4, 3)).toBe("not-reached");
    expect(eventState(4, null)).toBe("served");
  });

  it("never presents an uncaptured baseline suffix as live", () => {
    const events = hybridTimeline(demoRun.timeline, 3, []);

    expect(events.map((event) => event.seq)).toEqual([1, 2, 3]);
  });

  it("renders forward-compatible events without a duration", () => {
    render(
      <Timeline
        events={[{ ...demoRun.timeline[0], duration_seconds: null }]}
        mismatchAt={null}
        onSelect={vi.fn()}
        selected={null}
      />,
    );

    expect(screen.getByRole("button", { name: /Flow 1/ })).toHaveTextContent(
      "duration unavailable",
    );
  });
});

/**
 * Responses shaped exactly like the ones `replayable ui` serves.
 *
 * `canonical_body` is compact, key-sorted, sentinel-substituted JSON — *not*
 * the pretty-printed raw request. Fixtures that quietly pretty-print it hide
 * the very defect this suite is supposed to catch.
 */
function apiFetch(overrides: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    "/api/cassettes": {
      cassettes: [
        {
          name: "research-agent",
          flow_count: 20,
          created_at: "2026-07-29T08:11:54Z",
          image: { ref: "replayable/research-agent:local", digest: "sha256:cd398ef53ea7" },
          status: "mismatch",
          last_exit_code: 2,
          has_observation: true,
          has_fork_result: false,
        },
      ],
    },
    "/api/cassettes/research-agent/timeline": {
      events: [1, 2, 3].map((seq) => ({
        seq,
        lamport: seq,
        t_rel: seq * 0.4,
        channel: "network",
        kind: "http.exchange",
        scope: "api.anthropic.com",
        key: "POST api.anthropic.com:443/v1/messages",
        duration_seconds: 0.3,
        stream_chunk_count: 0,
      })),
    },
    "/api/cassettes/research-agent/mismatch": {
      live_request: {
        method: "POST",
        host: "api.anthropic.com",
        path: "/v1/messages",
        canonical_body:
          '{"max_tokens":700,"request_id":"§VOLATILE§","system":"You are a verbose research agent."}',
        match_key: "5903c87f24b6d3dc",
      },
      nearest_candidates: [{ seq: 3 }],
      diff: '-  "concise"\n+  "verbose"',
    },
    "/api/cassettes/research-agent/flows/3": {
      seq: 3,
      key: { method: "POST", host: "api.anthropic.com", port: 443, path: "/v1/messages" },
      request: {
        query: "",
        headers: [["content-type", "application/json"]],
        body_decoded:
          '{"system":"You are a concise research agent.","request_id":"req_018fa2","max_tokens":700}',
      },
      response: { status: 200, headers: [], body_decoded: "" },
      timing: { started: 0.9, completed: 1.1 },
    },
    "/api/cassettes/research-agent/explain?flow=3": {
      flow: 3,
      match_key: "5903c87f24b6d3dc",
      pre_hash: "POST\napi.anthropic.com\n/v1/messages\n\n{}",
      canonical_body:
        '{"max_tokens":700,"request_id":"§VOLATILE§","system":"You are a concise research agent."}',
      diff_body: "{}",
      rules: {
        version: "sha256:1041e721aabbccdd",
        field_names: ["request_id"],
        value_patterns: [],
        preserve: [],
      },
    },
  };
  // An `undefined` override removes the route, so it 404s like a cassette
  // artifact the server does not have.
  for (const [path, value] of Object.entries(overrides)) {
    if (value === undefined) delete routes[path];
    else routes[path] = value;
  }
  return vi.fn(async (path: string, _init?: RequestInit) => {
    if (!(path in routes)) {
      return { ok: false, status: 404, json: async () => ({ error: "not found" }) };
    }
    return { ok: true, status: 200, json: async () => routes[path] };
  });
}

describe("Dashboard", () => {
  it("labels the fabricated fallback when the local API is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<Dashboard />);

    expect(
      screen.getByRole("heading", { name: "Research Agent" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Behavior changed")).toBeInTheDocument();
    expect(screen.getByText("2/7")).toBeInTheDocument();
    expect(screen.getByText("request_id ignored")).toBeInTheDocument();
    // Sample data must announce itself; an unlabelled fake baseline is worse
    // than an empty screen.
    expect(screen.getByText(/Sample data/)).toBeInTheDocument();
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/cassettes", undefined),
    );
  });

  it("diffs the matcher's normalized view of both requests, not raw vs canonical", async () => {
    vi.stubGlobal("fetch", apiFetch());
    render(<Dashboard />);

    await waitFor(() =>
      expect(screen.queryByText(/Sample data/)).not.toBeInTheDocument(),
    );
    const recorded = await screen.findByTestId("removed-pane");
    const live = screen.getByTestId("added-pane");

    // Both panes are normalized: the real request_id never appears, and the
    // sentinel appears on both sides rather than as a spurious change.
    expect(recorded).not.toHaveTextContent("req_018fa2");
    expect(recorded).toHaveTextContent("§VOLATILE§");
    expect(live).toHaveTextContent("§VOLATILE§");
    expect(recorded).toHaveTextContent("concise");
    expect(live).toHaveTextContent("verbose");
    // Exactly one token differs on each side.
    expect(recorded.querySelectorAll(".token-removed")).toHaveLength(1);
    expect(live.querySelectorAll(".token-added")).toHaveLength(1);
  });

  it("derives the changed badge and the served count from the real run", async () => {
    vi.stubGlobal("fetch", apiFetch());
    render(<Dashboard />);

    // The badge names the field that actually differs after normalization.
    expect(await screen.findByText("system changed")).toBeInTheDocument();
    expect(screen.queryByText("model changed")).not.toBeInTheDocument();
    // Three timeline events, mismatch at flow 3 — not a hard-coded 7.
    expect(screen.getByText("2/3 served")).toBeInTheDocument();
    // The image chip reports the recorded reference instead of asserting
    // "exact image" for a run that may have used --allow-image-mismatch.
    expect(
      screen.getByText("replayable/research-agent:local"),
    ).toBeInTheDocument();
  });

  it("never dresses a real cassette in another run's fabricated mismatch", async () => {
    vi.stubGlobal(
      "fetch",
      apiFetch({ "/api/cassettes/research-agent/timeline": undefined }),
    );
    render(<Dashboard />);

    await waitFor(() =>
      expect(screen.queryByText(/Sample data/)).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("Behavior changed")).not.toBeInTheDocument();
    expect(screen.getByText("0 FLOWS")).toBeInTheDocument();
  });

  it("shows an empty-cassette mismatch even without a nearest candidate", async () => {
    vi.stubGlobal(
      "fetch",
      apiFetch({
        "/api/cassettes/research-agent/timeline": { events: [] },
        "/api/cassettes/research-agent/mismatch": {
          live_request: {
            method: "POST",
            host: "api.anthropic.com",
            path: "/v1/messages",
            canonical_body: '{"system":"unrecorded"}',
          },
          nearest_candidates: [],
          diff: "",
        },
      }),
    );
    render(<Dashboard />);

    expect(await screen.findByText("Behavior changed")).toBeInTheDocument();
    expect(screen.getAllByText("no recorded candidate").length).toBeGreaterThan(0);
    expect(screen.getByText("0/0 served")).toBeInTheDocument();
    expect(screen.getByTestId("added-pane")).toHaveTextContent("unrecorded");
  });

  it("does not report a recorded request as absent when only /explain fails", async () => {
    // `/explain` can fail while `/flows/N` succeeds — a cassette pinning a
    // malformed replayable.toml is enough. The recorded body is in hand, so
    // claiming it does not exist would be a plain falsehood.
    vi.stubGlobal(
      "fetch",
      apiFetch({ "/api/cassettes/research-agent/explain?flow=3": undefined }),
    );
    render(<Dashboard />);

    const recorded = await screen.findByTestId("removed-pane");
    expect(recorded).toHaveTextContent("concise");
    expect(recorded).not.toHaveTextContent("No recorded request is available");
    expect(screen.queryByText("No recorded candidate")).not.toBeInTheDocument();
    expect(screen.getByText("Normalization unavailable")).toBeInTheDocument();
    // Raw against canonical is not a comparison; nothing may be highlighted as
    // a behavioural change, and the panel has to say why.
    expect(recorded.querySelectorAll(".token-removed")).toHaveLength(0);
    expect(
      screen.getByTestId("added-pane").querySelectorAll(".token-added"),
    ).toHaveLength(0);
    expect(screen.getByText(/shown side by side without a comparison/)).toBeInTheDocument();
  });

  it("keeps a run refresh that a flow selection overlaps", async () => {
    // `loadRun` and `selectFlow` are separate generations. While they shared
    // one counter, clicking a timeline row *after* the post-replay refresh had
    // started superseded it, and the entire refresh — timeline, mismatch, fork
    // result — was silently discarded while the notice still said the replay
    // had run.
    let releaseTimeline!: () => void;
    const timelineGate = new Promise<void>((resolve) => {
      releaseTimeline = resolve;
    });
    const event = (seq: number) => ({
      seq,
      lamport: seq,
      t_rel: seq * 0.4,
      channel: "network",
      kind: "http.exchange",
      scope: "api.anthropic.com",
      key: "POST api.anthropic.com:443/v1/messages",
      duration_seconds: 0.3,
      stream_chunk_count: 0,
    });
    const base = apiFetch();
    let timelineCalls = 0;
    const fetchMock = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/cassettes/research-agent/replay") {
        return { ok: true, status: 200, json: async () => ({ exit_code: 2 }) };
      }
      if (path === "/api/cassettes/research-agent/timeline") {
        timelineCalls += 1;
        if (timelineCalls > 1) {
          // The refresh the replay kicked off: hold it open so a flow click
          // can land in the middle of it.
          await timelineGate;
          return {
            ok: true,
            status: 200,
            json: async () => ({ events: [event(1), event(2)] }),
          };
        }
      }
      return base(path, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Dashboard />);

    await screen.findByText("3 FLOWS");

    fireEvent.click(screen.getByRole("button", { name: "Replay" }));
    await waitFor(() => expect(timelineCalls).toBe(2));
    fireEvent.click(screen.getByRole("button", { name: /Flow 1/ }));
    releaseTimeline();

    // The refreshed two-event timeline replaced the stale three-event one.
    await waitFor(() => expect(screen.getByText("2 FLOWS")).toBeInTheDocument());
  });

  it("renders a timeline event whose cost metric is null", async () => {
    vi.stubGlobal(
      "fetch",
      apiFetch({
        "/api/cassettes/research-agent/timeline": {
          events: [
            {
              seq: 1,
              lamport: 1,
              t_rel: 0.1,
              channel: "model",
              kind: "http.exchange",
              scope: "api.anthropic.com",
              key: "POST api.anthropic.com:443/v1/messages",
              duration_seconds: 0.3,
              stream_chunk_count: 0,
              // `_summary_event` copies `metrics` through verbatim, so a null
              // reaches the client as a null, not an absent key.
              metrics: { model: "claude-haiku-4-5", estimated_cost_usd: null },
            },
          ],
        },
      }),
    );
    render(<Dashboard />);

    expect(
      await screen.findByRole("button", { name: /Flow 1/ }),
    ).toHaveTextContent("MODEL CALL");
    expect(screen.getByText("1 FLOWS")).toBeInTheDocument();
  });

  it("does not let a slower prior cassette overwrite the current selection", async () => {
    let resolveResearchTimeline!: (value: {
      ok: boolean;
      status: number;
      json: () => Promise<{ events: unknown[] }>;
    }) => void;
    const researchTimeline = new Promise<{
      ok: boolean;
      status: number;
      json: () => Promise<{ events: unknown[] }>;
    }>((resolve) => {
      resolveResearchTimeline = resolve;
    });
    const event = (key: string) => ({
      seq: 1,
      lamport: 1,
      t_rel: 0.1,
      channel: "network",
      kind: "http.exchange",
      scope: "example.test",
      key,
      duration_seconds: 0.1,
      stream_chunk_count: 0,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) => {
        if (path === "/api/cassettes") {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              cassettes: [
                demoCassettes[0],
                demoCassettes[1],
              ],
            }),
          };
        }
        if (path === "/api/cassettes/research-agent/timeline") {
          return researchTimeline;
        }
        if (path === "/api/cassettes/support-triage/timeline") {
          return {
            ok: true,
            status: 200,
            json: async () => ({ events: [event("GET support.example.test/ticket")] }),
          };
        }
        return {
          ok: false,
          status: 404,
          json: async () => ({ error: "not found" }),
        };
      }),
    );
    render(<Dashboard />);

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/cassettes/research-agent/timeline",
        undefined,
      ),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /Support Triage/ }),
    );
    expect(
      await screen.findByText("GET support.example.test/ticket"),
    ).toBeInTheDocument();

    resolveResearchTimeline({
      ok: true,
      status: 200,
      json: async () => ({
        events: [event("GET stale-research.example.test/result")],
      }),
    });

    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/cassettes/research-agent/mismatch",
        undefined,
      ),
    );
    expect(screen.getByText("GET support.example.test/ticket")).toBeInTheDocument();
    expect(
      screen.queryByText("GET stale-research.example.test/result"),
    ).not.toBeInTheDocument();
  });

  it("sends the current strict-mode value when replay is requested", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("use demo"))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ exit_code: 2 }),
      });
    vi.stubGlobal("fetch", fetchMock);
    render(<Dashboard />);

    fireEvent.click(screen.getByRole("checkbox", { name: /Strict mode/ }));
    fireEvent.click(screen.getByRole("button", { name: "Replay" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/cassettes/research-agent/replay",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ strict: false }),
        }),
      ),
    );
    expect(await screen.findByText("Replay exited 2.")).toBeInTheDocument();
  });

  it("distinguishes atomic replacement from a fresh sibling baseline", () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<Dashboard />);

    fireEvent.click(screen.getByRole("button", { name: "View full diff" }));
    expect(
      screen.getByRole("dialog", { name: "Full matcher diff" }),
    ).toHaveTextContent("concise");
    fireEvent.click(screen.getByRole("button", { name: "Close dialog" }));

    fireEvent.click(
      screen.getByRole("button", { name: "Re-record baseline" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Re-record baseline",
    });
    expect(dialog).toHaveTextContent("atomically replaces");
    expect(screen.queryByLabelText("Destination name")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Replace baseline" }),
    ).toBeInTheDocument();
  });
});
