"use client";

import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronDown,
  CircleStop,
  Clock3,
  Code2,
  Database,
  FileDiff,
  GitFork,
  Network,
  Play,
  Plus,
  Radio,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  WifiOff,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  listCassettes,
  loadExplain,
  loadFlow,
  loadForkResult,
  loadMismatch,
  loadTimeline,
  recordFreshBaseline,
  runFork,
  runReplay,
} from "@/lib/api";
import { demoCassettes, demoRun } from "@/lib/demo";
import { changedFields, diffPanes } from "@/lib/normalized-body";
import type {
  CassetteSummary,
  Explain,
  Mismatch,
  RunData,
} from "@/lib/types";
import { hybridTimeline, Timeline } from "./timeline";
import { TokenDiff } from "./token-diff";
import { DownstreamCheck } from "./downstream-check";

function displayName(name: string): string {
  return name
    .split(/[-_]/)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function mismatchSequence(mismatch: Mismatch | null): number | null {
  return mismatch?.nearest_candidates[0]?.seq ?? null;
}

async function optional<T>(operation: Promise<T>): Promise<T | null> {
  try {
    return await operation;
  } catch {
    return null;
  }
}

function emptyRun(): RunData {
  return {
    timeline: [],
    mismatch: null,
    flow: null,
    explain: null,
    forkResult: null,
  };
}

function Sidebar({
  cassettes,
  selected,
  onSelect,
}: {
  cassettes: CassetteSummary[];
  selected: string;
  onSelect: (name: string) => void;
}) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">
          <Radio size={18} />
        </span>
        <span>
          <strong>replayable</strong>
          <small>local run explorer</small>
        </span>
      </div>
      <div className="sidebar-section-label">
        <span>Cassettes</span>
        <button aria-label="Add cassette" className="icon-button" type="button">
          <Plus size={15} />
        </button>
      </div>
      <label className="search-box">
        <Search size={14} />
        <input aria-label="Filter cassettes" placeholder="Filter runs…" />
      </label>
      <nav className="cassette-list" aria-label="Cassettes">
        {cassettes.map((cassette) => (
          <button
            className={`cassette-item ${
              cassette.name === selected ? "active" : ""
            }`}
            key={cassette.name}
            onClick={() => onSelect(cassette.name)}
            type="button"
          >
            <span className="cassette-icon">
              <Database size={16} />
            </span>
            <span className="cassette-copy">
              <strong>{displayName(cassette.name)}</strong>
              <small>
                {cassette.flow_count} flows ·{" "}
                {new Date(cassette.created_at).toLocaleDateString("en-US", {
                  month: "short",
                  day: "numeric",
                  timeZone: "UTC",
                })}
              </small>
            </span>
            <span className={`status-dot ${cassette.status}`} />
          </button>
        ))}
      </nav>
      <div className="sidebar-legend">
        <span>
          <i className="status-dot replayable" /> Replayable
        </span>
        <span>
          <i className="status-dot mismatch" /> Mismatch
        </span>
      </div>
      <button className="sidebar-settings" type="button">
        <Settings2 size={15} />
        Workspace settings
      </button>
    </aside>
  );
}

function RunHeader({
  cassette,
  strict,
  setStrict,
  running,
  onReplay,
  onRecord,
  hybrid,
  onFork,
}: {
  cassette: CassetteSummary;
  strict: boolean;
  setStrict: (strict: boolean) => void;
  running: boolean;
  onReplay: () => void;
  onRecord: () => void;
  hybrid: boolean;
  onFork: () => void;
}) {
  return (
    <header className="run-header">
      <div>
        <div className="breadcrumb">
          Cassettes <span>/</span> {cassette.name}
        </div>
        <div className="run-title">
          <h1>{displayName(cassette.name)}</h1>
          <span className={`offline-pill ${hybrid ? "hybrid" : ""}`}>
            {hybrid ? <Network size={13} /> : <WifiOff size={13} />}
            {hybrid ? "HYBRID" : "OFFLINE"}
          </span>
        </div>
      </div>
      <div className="header-actions">
        <label className="strict-control">
          <span>
            Strict mode
            <small>Unconsumed flows fail</small>
          </span>
          <input
            checked={strict}
            onChange={(event) => setStrict(event.target.checked)}
            type="checkbox"
          />
          <i />
        </label>
        <button className="button secondary" onClick={onRecord} type="button">
          <RefreshCw size={15} />
          Re-record baseline
        </button>
        <button className="button fork-button" onClick={onFork} type="button">
          <GitFork size={15} />
          Replay fork
        </button>
        <button
          className="button primary"
          disabled={running}
          onClick={onReplay}
          type="button"
        >
          {running ? <CircleStop size={15} /> : <Play fill="currentColor" size={15} />}
          {running ? "Running…" : "Replay"}
        </button>
      </div>
    </header>
  );
}

function BehaviorBanner({
  mismatchAt,
  served,
  total,
  onViewDiff,
}: {
  mismatchAt: number | null;
  served: number;
  total: number;
  onViewDiff: () => void;
}) {
  return (
    <section className="behavior-banner">
      <span className="banner-icon">
        <AlertTriangle size={21} />
      </span>
      <span className="banner-copy">
        <strong>Behavior changed</strong>
        <span>
          {mismatchAt === null
            ? "Replay emitted an outgoing request, but this cassette has no recorded candidate to compare."
            : `Replay stopped at flow ${mismatchAt}. The outgoing model request no longer matches the recorded baseline.`}
        </span>
      </span>
      <span className="banner-stats">
        <b>exit 2</b>
        <span>
          {mismatchAt === null
            ? "no recorded candidate"
            : `mismatch at flow ${mismatchAt}`}
        </span>
        <span>
          {served}/{total} served
        </span>
      </span>
      <button
        className="button banner-button"
        onClick={onViewDiff}
        type="button"
      >
        <FileDiff size={15} />
        View full diff
      </button>
    </section>
  );
}

function HybridSummary({
  result,
}: {
  result: NonNullable<RunData["forkResult"]>;
}) {
  const live = result.segments.live;
  return (
    <section className="hybrid-summary">
      <span className="hybrid-title">
        <GitFork size={18} />
        <span>
          <strong>Hybrid replay complete</strong>
          <small>Network resumed after flow {result.fork_at}</small>
        </span>
      </span>
      <span className="segment-stat pinned">
        <small>PINNED</small>
        <strong>{result.segments.pinned.served_flow_count} flows</strong>
        <b>$0.00</b>
      </span>
      <span className="segment-arrow">
        <ArrowRight size={17} />
      </span>
      <span className="segment-stat live">
        <small>LIVE · FRESH CALLS</small>
        <strong>
          {live.flow_count} flows
          {live.error_count > 0 ? ` · ${live.error_count} failed` : ""}
        </strong>
        <b>
          {live.estimated_cost_usd === null
            ? "cost unavailable"
            : `$${live.estimated_cost_usd.toFixed(4)}`}
        </b>
      </span>
      <span className="segment-stat timing">
        <small>WALL TIME</small>
        <strong>{result.timing.wall_time_seconds.toFixed(1)}s</strong>
        <b>{live.model_calls} model call{live.model_calls === 1 ? "" : "s"}</b>
      </span>
    </section>
  );
}

function Modal({
  title,
  children,
  onClose,
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        aria-label={title}
        aria-modal="true"
        className="modal-card"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="modal-heading">
          <strong>{title}</strong>
          <button
            aria-label="Close dialog"
            className="icon-button"
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

function NormalizationPanel({
  explain,
  changed,
}: {
  explain: Explain | null;
  changed: string[];
}) {
  if (!explain) return null;
  const fields = explain.rules.field_names.slice(0, 5);
  return (
    <div className="normalization-panel">
      <div className="normalization-title">
        <ShieldCheck size={15} />
        <span>
          Normalization applied
          <small>{explain.rules.version.slice(0, 18)}…</small>
        </span>
        <ChevronDown size={14} />
      </div>
      <div className="rule-badges">
        {fields.map((field) => (
          <span className="rule-badge ignored" key={field}>
            <Check size={11} />
            {field} ignored
          </span>
        ))}
        {/* Derived from the two normalized bodies, so the badge names the field
            that actually survived normalization and still differs. */}
        {changed.slice(0, 5).map((field) => (
          <span className="rule-badge changed" key={field}>
            <X size={11} />
            {field} changed
          </span>
        ))}
      </div>
    </div>
  );
}

export function Dashboard() {
  const [cassettes, setCassettes] = useState<CassetteSummary[]>(demoCassettes);
  const [selected, setSelected] = useState(demoCassettes[0].name);
  const [run, setRun] = useState<RunData>(demoRun);
  // Everything on screen is fabricated until the local API answers. Saying so
  // is not optional: the whole point of this dashboard is that the badges
  // reflect a real matcher decision.
  const [live, setLive] = useState(false);
  const [strict, setStrict] = useState(true);
  const [running, setRunning] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [modal, setModal] = useState<
    "record" | "diff" | "fork" | "compare" | null
  >(null);
  const [destination, setDestination] = useState("research-agent-fresh");
  const [replaceBaseline, setReplaceBaseline] = useState(false);
  const [envFile, setEnvFile] = useState("");
  const [forkAt, setForkAt] = useState(3);
  // Two independent generations. Sharing one counter meant a timeline click
  // (`selectFlow`) superseded an in-flight `loadRun`, so the run refresh after
  // a replay was silently discarded and the panel kept showing the old result.
  // `loadRun` bumps both, so a stale flow response can never land on a newer
  // cassette's run.
  const runRequest = useRef(0);
  const flowRequest = useRef(0);

  const loadRun = useCallback(async (name: string) => {
    const request = ++runRequest.current;
    const flowGeneration = ++flowRequest.current;
    const timeline = await optional(loadTimeline(name));
    if (!timeline) {
      // Never dress a real cassette in another run's fabricated mismatch.
      if (request === runRequest.current) setRun(emptyRun());
      return;
    }
    const [mismatch, forkResult] = await Promise.all([
      optional(loadMismatch(name)),
      optional(loadForkResult(name)),
    ]);
    const sequence = mismatchSequence(mismatch) ?? timeline[0]?.seq ?? 1;
    const [flow, explain] = await Promise.all([
      optional(loadFlow(name, sequence)),
      optional(loadExplain(name, sequence)),
    ]);
    const effectiveTimeline = forkResult
      ? hybridTimeline(timeline, forkResult.fork_at, forkResult.events)
      : timeline;
    // A slower response for a previously selected cassette must not overwrite
    // the run that is currently on screen.
    if (request !== runRequest.current) return;
    setRun((current) => ({
      timeline: effectiveTimeline,
      mismatch,
      // A flow click that started after this refresh is the newer selection.
      // Preserve it while still applying the refreshed run-level artifacts.
      flow:
        flowGeneration === flowRequest.current
          ? flow
          : current.flow,
      explain:
        flowGeneration === flowRequest.current
          ? explain
          : current.explain,
      forkResult,
    }));
  }, []);

  useEffect(() => {
    let active = true;
    listCassettes()
      .then((items) => {
        if (!active || items.length === 0) return;
        setLive(true);
        setCassettes(items);
        setSelected(items[0].name);
        return loadRun(items[0].name);
      })
      .catch(() => {
        // The checked-in demo state keeps the static export reviewable even
        // before the local API process has started.
      });
    return () => {
      active = false;
    };
  }, [loadRun]);

  const cassette =
    cassettes.find((item) => item.name === selected) ?? cassettes[0];
  const mismatchAt = mismatchSequence(run.mismatch);
  const hasMismatch = run.mismatch !== null;
  const hybrid = run.forkResult;
  const served =
    mismatchAt === null
      ? hasMismatch
        ? 0
        : run.timeline.length
      : mismatchAt - 1;
  const progress = Math.round((served / Math.max(run.timeline.length, 1)) * 100);
  const selectedSequence = run.flow?.seq ?? mismatchAt;

  // Both panes must be the matcher's own view of the request; see lib/normalized-body.
  const panes = diffPanes(run.flow, run.mismatch, run.explain);
  const changed = panes.normalized
    ? changedFields(panes.recorded, panes.live)
    : [];
  // No mismatch report means the matcher served this flow: a pass, not an
  // absence of information.
  const matched = !hasMismatch;

  const selectCassette = useCallback(
    (name: string) => {
      setSelected(name);
      setNotice(null);
      setRun(emptyRun());
      void loadRun(name);
    },
    [loadRun],
  );

  const selectFlow = useCallback(
    async (sequence: number) => {
      // Only the flow generation: selecting a flow must not cancel a run that
      // is still loading.
      const request = ++flowRequest.current;
      const [flow, explain] = await Promise.all([
        optional(loadFlow(selected, sequence)),
        optional(loadExplain(selected, sequence)),
      ]);
      if (request !== flowRequest.current) return;
      setRun((current) => ({
        ...current,
        flow: flow ?? current.flow,
        explain: explain ?? current.explain,
      }));
    },
    [selected],
  );

  const replay = useCallback(async () => {
    setRunning(true);
    setNotice(null);
    try {
      const code = await runReplay(selected, strict);
      setNotice(code === 0 ? "Replay matched the baseline." : `Replay exited ${code}.`);
      await loadRun(selected);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Replay failed.");
    } finally {
      setRunning(false);
    }
  }, [loadRun, selected, strict]);

  const recordBaseline = useCallback(async () => {
    setRunning(true);
    setNotice(null);
    try {
      const code = await recordFreshBaseline(
        selected,
        destination,
        envFile,
        replaceBaseline,
      );
      setNotice(
        code === 0
          ? replaceBaseline
            ? `Replaced baseline ${destination}.`
            : `Saved fresh baseline as ${destination}.`
          : `Baseline recording exited ${code}.`,
      );
      setModal(null);
      const items = await listCassettes();
      setCassettes(items);
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "Baseline recording failed.",
      );
    } finally {
      setRunning(false);
    }
  }, [destination, envFile, replaceBaseline, selected]);

  const replayFork = useCallback(async () => {
    setRunning(true);
    setNotice(null);
    try {
      const code = await runFork(selected, forkAt, envFile);
      setNotice(
        code === 0
          ? "Hybrid replay matched the baseline."
          : `Hybrid replay exited ${code}; inspect the downstream check.`,
      );
      setModal(null);
      await loadRun(selected);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Hybrid replay failed.");
    } finally {
      setRunning(false);
    }
  }, [envFile, forkAt, loadRun, selected]);

  const runTimestamp = useMemo(
    () =>
      new Date(cassette.created_at).toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZone: "UTC",
        timeZoneName: "short",
      }),
    [cassette.created_at],
  );

  return (
    <div className="app-shell">
      <Sidebar
        cassettes={cassettes}
        onSelect={selectCassette}
        selected={selected}
      />
      <main className="main-area">
        <RunHeader
          cassette={cassette}
          hybrid={hybrid !== null}
          onFork={() => {
            setForkAt(hybrid?.fork_at ?? Math.min(3, cassette.flow_count));
            setModal("fork");
          }}
          onRecord={() => {
            setDestination(selected);
            setReplaceBaseline(true);
            setModal("record");
          }}
          onReplay={replay}
          running={running}
          setStrict={setStrict}
          strict={strict}
        />
        {!live && (
          <div className="notice demo-notice" role="status">
            <AlertTriangle size={14} />
            Sample data — the local API is not reachable. Start it with{" "}
            <code>replayable ui --allow-write</code> to inspect real cassettes.
          </div>
        )}
        {notice && (
          <div className="notice" role="status">
            <Activity size={14} />
            {notice}
          </div>
        )}
        <div className="run-meta-bar">
          <span>
            <Clock3 size={13} />
            Recorded {runTimestamp}
          </span>
          <span>
            <Code2 size={13} />
            {cassette.image.digest.slice(0, 19)}…
          </span>
          <span>
            <ShieldCheck size={13} />
            {cassette.image.ref}
          </span>
        </div>
        {hybrid && <HybridSummary result={hybrid} />}
        {!hybrid && hasMismatch && (
          <BehaviorBanner
            mismatchAt={mismatchAt}
            onViewDiff={() => setModal("diff")}
            served={served}
            total={run.timeline.length}
          />
        )}
        {!hybrid && (
          <section className="progress-card">
            <div className="progress-copy">
              <span>
                <strong>
                  {served}/{run.timeline.length}
                </strong>{" "}
                flows served
              </span>
              {/* "stopped" only makes sense when the replay actually stopped;
                  a clean run reached the end of the cassette. */}
              <span>
                {!hasMismatch
                  ? "completed the cassette"
                  : mismatchAt === null
                    ? "stopped on an unmatched request"
                  : `stopped ${
                      run.timeline[mismatchAt - 1]?.t_rel.toFixed(1) ?? "0.0"
                    }s in`}
              </span>
            </div>
            <div className={`progress-track ${matched ? "matched" : ""}`}>
              <i style={{ width: `${progress}%` }} />
            </div>
          </section>
        )}
        <div className="workspace-grid">
          <section className="panel timeline-panel">
            <div className="panel-heading">
              <span>
                <Activity size={15} />
                Run timeline
              </span>
              <b>{run.timeline.length} FLOWS</b>
            </div>
            <Timeline
              events={run.timeline}
              forkAt={hybrid?.fork_at}
              mismatchAt={mismatchAt}
              onSelect={selectFlow}
              selected={selectedSequence}
            />
          </section>
          {hybrid ? (
            <section className="panel inspection-panel hybrid-inspection">
              <DownstreamCheck
                onCompare={() => setModal("compare")}
                onSave={() => {
                  setDestination(`${selected}-hybrid`);
                  setReplaceBaseline(false);
                  setModal("record");
                }}
                result={hybrid}
              />
            </section>
          ) : (
            <section className="panel inspection-panel">
              <div className="panel-heading">
                <span>
                  <FileDiff size={15} />
                  Request comparison
                </span>
                <b>FLOW {selectedSequence ?? "—"}</b>
              </div>
              {/* A run with no mismatch is a *pass*. Alert-red styling and a
                  warning triangle would invert the demo's whole signal in the
                  one panel that explains the matcher's decision. */}
              <div className={`mismatch-callout ${matched ? "matched" : ""}`}>
                <span>
                  {matched ? <Check size={14} /> : <AlertTriangle size={14} />}
                  {matched
                    ? "Request matched the recording"
                    : run.explain
                      ? "Match key changed"
                      : run.flow
                        ? // The recorded request exists; only its normalization
                          // is missing. Do not report it as absent.
                          "Normalization unavailable"
                        : "No recorded candidate"}
                </span>
                <code>
                  {run.explain?.match_key.slice(0, 12) ?? "unavailable"}…
                </code>
              </div>
              {/* On a pass the harness never captured a replay request body —
                  the matcher only proved the normalized keys were equal. Show
                  the recorded request once instead of labelling recorded bytes
                  "Replay request". */}
              <TokenDiff
                comparable={panes.comparable ?? true}
                live={panes.live}
                normalized={panes.normalized}
                recorded={panes.recorded}
                single={matched}
              />
              <NormalizationPanel changed={changed} explain={run.explain} />
              <div className="diff-footer">
                {!matched && (
                  <>
                    <span>
                      <i className="legend removed" /> recorded only
                    </span>
                    <span>
                      <i className="legend added" /> replay only
                    </span>
                  </>
                )}
                <button className="text-button" type="button">
                  Inspect raw request
                  <ArrowRight size={13} />
                </button>
              </div>
            </section>
          )}
        </div>
      </main>
      {modal === "record" && (
        <Modal
          onClose={() => setModal(null)}
          title={replaceBaseline ? "Re-record baseline" : "Record fresh baseline"}
        >
          <div className="modal-body">
            <p>
              {replaceBaseline
                ? "This records a complete candidate first, then atomically replaces the selected baseline."
                : "This records a new sibling cassette. The current baseline is never overwritten."}
            </p>
            {!replaceBaseline && (
              <label className="form-field">
                <span>Destination name</span>
                <input
                  onChange={(event) => setDestination(event.target.value)}
                  value={destination}
                />
              </label>
            )}
            <label className="form-field">
              <span>Environment file (optional)</span>
              <input
                onChange={(event) => setEnvFile(event.target.value)}
                placeholder="/absolute/path/to/.env"
                value={envFile}
              />
            </label>
          </div>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setModal(null)}
              type="button"
            >
              Cancel
            </button>
            <button
              className="button primary"
              disabled={running || !destination}
              onClick={recordBaseline}
              type="button"
            >
              <RefreshCw size={14} />
              {replaceBaseline ? "Replace baseline" : "Record baseline"}
            </button>
          </div>
        </Modal>
      )}
      {modal === "diff" && (
        <Modal onClose={() => setModal(null)} title="Full matcher diff">
          <pre className="full-diff">
            {run.mismatch?.diff || "No textual matcher diff is available."}
          </pre>
        </Modal>
      )}
      {modal === "fork" && (
        <Modal onClose={() => setModal(null)} title="Replay from fork point">
          <div className="modal-body">
            <p>
              Flows before the boundary stay pinned. Every request after it
              reaches the live network and is redacted before capture.
            </p>
            <label className="form-field">
              <span>Fork after flow</span>
              <input
                max={cassette.flow_count}
                min={0}
                onChange={(event) => setForkAt(event.target.valueAsNumber)}
                type="number"
                value={forkAt}
              />
            </label>
            <label className="form-field">
              <span>Environment file</span>
              <input
                onChange={(event) => setEnvFile(event.target.value)}
                placeholder="/absolute/path/to/.env"
                value={envFile}
              />
            </label>
          </div>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => setModal(null)}
              type="button"
            >
              Cancel
            </button>
            <button
              className="button primary"
              disabled={
                running ||
                !Number.isInteger(forkAt) ||
                forkAt < 0 ||
                forkAt > cassette.flow_count
              }
              onClick={replayFork}
              type="button"
            >
              <GitFork size={14} />
              Run hybrid replay
            </button>
          </div>
        </Modal>
      )}
      {modal === "compare" && hybrid && (
        <Modal onClose={() => setModal(null)} title="Full downstream comparison">
          <pre className="full-diff">
            {JSON.stringify(hybrid.downstream, null, 2)}
          </pre>
        </Modal>
      )}
    </div>
  );
}
