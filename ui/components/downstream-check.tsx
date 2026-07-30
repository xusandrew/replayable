import {
  ArrowRight,
  Check,
  Files,
  GitCompareArrows,
  ListChecks,
  Save,
  Sparkles,
  X,
} from "lucide-react";
import type { ForkResult } from "@/lib/types";

function CheckRow({
  label,
  detail,
  matches,
}: {
  label: string;
  detail: string;
  matches: boolean;
}) {
  return (
    <div className="check-row">
      <span className={`check-icon ${matches ? "pass" : "fail"}`}>
        {matches ? <Check size={13} /> : <X size={13} />}
      </span>
      <span>
        <strong>{label}</strong>
        <small>{detail}</small>
      </span>
      <b className={matches ? "pass" : "fail"}>
        {matches ? "MATCH" : "CHANGED"}
      </b>
    </div>
  );
}

export function DownstreamCheck({
  result,
  onSave,
  onCompare,
}: {
  result: ForkResult;
  onSave: () => void;
  onCompare: () => void;
}) {
  const similarity = result.downstream.similarity;
  const toolCalls = result.downstream.tool_calls;
  const workspace = result.downstream.workspace;
  const changedFiles = [
    ...workspace.diff.added,
    ...workspace.diff.removed,
    ...workspace.diff.changed,
  ];

  return (
    <section className="downstream-panel">
      <div className="downstream-heading">
        <span>
          <GitCompareArrows size={16} />
          Downstream check
          <small>baseline vs. hybrid result</small>
        </span>
        <span
          className={`verdict-pill ${similarity.passes ? "pass" : "fail"}`}
        >
          {similarity.passes ? <Check size={12} /> : <X size={12} />}
          {similarity.passes ? "WITHIN THRESHOLD" : "BELOW THRESHOLD"}
        </span>
      </div>
      <div className="score-hero">
        <div
          className="score-ring"
          style={
            {
              "--score": `${Math.round(similarity.score * 360)}deg`,
            } as React.CSSProperties
          }
        >
          <span aria-label="Similarity score">
            {Math.round(similarity.score * 100)}
          </span>
          <small>%</small>
        </div>
        <div className="score-copy">
          <span>
            <Sparkles size={14} />
            Lexical + structural similarity
          </span>
          <strong>
            Hybrid transcript is{" "}
            {similarity.passes ? "behaviorally close" : "materially different"}
          </strong>
          <p>
            Threshold {Math.round(similarity.threshold * 100)}% · deterministic,
            local, and judge-free
          </p>
          {/* The similarity pill is advisory. `replayable replay --fork-at`
              exits on the byte-exact `downstream.matches` gate, so say which
              verdict the run actually carries instead of letting a green
              pill imply a green exit code. */}
          <p className={`gate-note ${result.downstream.matches ? "pass" : "fail"}`}>
            Run verdict: exit {result.exit_code} ·{" "}
            {result.downstream.matches
              ? "byte-identical to the baseline"
              : "not byte-identical to the baseline (similarity is advisory)"}
          </p>
        </div>
      </div>
      <div className="score-components">
        {Object.entries(similarity.components).map(([name, value]) => (
          <div key={name}>
            <span>{name.replaceAll("_", " ")}</span>
            <strong>{Math.round(value * 100)}%</strong>
            <i>
              <b style={{ width: `${value * 100}%` }} />
            </i>
          </div>
        ))}
      </div>
      <div className="check-list">
        <CheckRow
          detail={`${toolCalls.baseline_count} baseline · ${toolCalls.candidate_count} hybrid`}
          label="Same tool sequence"
          matches={toolCalls.matches}
        />
        <CheckRow
          detail={
            changedFiles.length
              ? changedFiles.slice(0, 3).join(", ")
              : "All output paths and hashes are identical"
          }
          label="Output files"
          matches={workspace.matches}
        />
        {/* stdout is one of the four gates behind `downstream.matches`.
            Omitting it let a run whose only divergence was the transcript
            render as three green rows. */}
        <CheckRow
          detail={`${result.downstream.stdout.baseline_sha256.slice(0, 12)}… vs ${result.downstream.stdout.candidate_sha256.slice(0, 12)}…`}
          label="Agent transcript"
          matches={result.downstream.stdout.matches}
        />
        <CheckRow
          detail={`baseline ${result.downstream.exit_code.baseline} · hybrid ${result.downstream.exit_code.candidate}`}
          label="Process exit"
          matches={result.downstream.exit_code.matches}
        />
      </div>
      <div className="downstream-actions">
        <button className="button secondary" onClick={onCompare} type="button">
          <ListChecks size={14} />
          Compare full run
          <ArrowRight size={12} />
        </button>
        <button className="button primary" onClick={onSave} type="button">
          <Save size={14} />
          Save as new baseline
        </button>
      </div>
      <div className="method-note">
        <Files size={12} />
        Score weights: transcript 60% · tool sequence 25% · output files 15%
      </div>
    </section>
  );
}
