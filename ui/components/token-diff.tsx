"use client";

type TokenDiffProps = {
  recorded: string;
  live: string;
  /** Both sides are the matcher's normalized view, so the diff is meaningful. */
  normalized?: boolean;
  /**
   * When false the two panes are in different representations. Highlighting
   * would mark nearly every token as changed and claim a behavioural
   * difference the data does not support, so it is suppressed.
   */
  comparable?: boolean;
  /**
   * Render the recorded request alone. Used when the replay matched: there is
   * no captured replay body to show, and duplicating the recorded one under a
   * "Replay request" label would assert something never observed.
   */
  single?: boolean;
};

function tokens(value: string): string[] {
  return value.split(/(\s+|[{}[\],:"])/).filter(Boolean);
}

function changedTokens(left: string[], right: string[]): Set<number> {
  const changed = new Set<number>();
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    if (left[index] !== right[index]) changed.add(index);
  }
  return changed;
}

function CodePane({
  value,
  changed,
  tone,
}: {
  value: string[];
  changed: Set<number>;
  tone: "removed" | "added";
}) {
  return (
    <pre className="code-pane" data-testid={`${tone}-pane`}>
      {/* A GET has no request body. An entirely blank panel reads as a
          rendering failure, so say what it means. */}
      {value.length === 0 ? (
        <span className="code-pane-empty">This request has no body.</span>
      ) : (
        value.map((token, index) => (
          <span
            className={changed.has(index) ? `token-${tone}` : undefined}
            key={`${index}-${token}`}
          >
            {token}
          </span>
        ))
      )}
    </pre>
  );
}

export function TokenDiff({
  recorded,
  live,
  normalized = true,
  comparable = true,
  single = false,
}: TokenDiffProps) {
  const baseline = tokens(recorded);
  const candidate = tokens(live);
  const empty = new Set<number>();

  if (single) {
    return (
      <div className="diff-grid single">
        <section className="diff-column">
          <div className="pane-label">
            <span className="pane-dot recorded" />
            Recorded request — served on replay
          </div>
          <CodePane value={baseline} changed={empty} tone="removed" />
        </section>
      </div>
    );
  }

  const baselineChanged = comparable ? changedTokens(baseline, candidate) : empty;
  const candidateChanged = comparable ? changedTokens(candidate, baseline) : empty;
  // Name the representation of each pane. When they differ, say that no
  // comparison is being made rather than showing an unhighlighted diff the
  // reader will assume means "identical".
  const suffix = normalized ? " (normalized)" : "";
  const recordedSuffix = comparable ? suffix : " (raw)";
  const liveSuffix = comparable ? suffix : " (normalized)";

  return (
    <div className="diff-grid">
      {!comparable && (
        <p className="diff-incomparable">
          Normalization is unavailable for the recorded request, so these panes
          are shown side by side without a comparison.
        </p>
      )}
      <section className="diff-column">
        <div className="pane-label">
          <span className="pane-dot recorded" />
          Recorded request{recordedSuffix}
        </div>
        <CodePane value={baseline} changed={baselineChanged} tone="removed" />
      </section>
      <section className="diff-column">
        <div className="pane-label">
          <span className="pane-dot live" />
          Replay request{liveSuffix}
        </div>
        <CodePane value={candidate} changed={candidateChanged} tone="added" />
      </section>
    </div>
  );
}
