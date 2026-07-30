"use client";

type TokenDiffProps = {
  recorded: string;
  live: string;
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
      {value.map((token, index) => (
        <span
          className={changed.has(index) ? `token-${tone}` : undefined}
          key={`${index}-${token}`}
        >
          {token}
        </span>
      ))}
    </pre>
  );
}

export function TokenDiff({ recorded, live }: TokenDiffProps) {
  const baseline = tokens(recorded);
  const candidate = tokens(live);
  const baselineChanged = changedTokens(baseline, candidate);
  const candidateChanged = changedTokens(candidate, baseline);

  return (
    <div className="diff-grid">
      <section className="diff-column">
        <div className="pane-label">
          <span className="pane-dot recorded" />
          Recorded request
        </div>
        <CodePane value={baseline} changed={baselineChanged} tone="removed" />
      </section>
      <section className="diff-column">
        <div className="pane-label">
          <span className="pane-dot live" />
          Replay request
        </div>
        <CodePane value={candidate} changed={candidateChanged} tone="added" />
      </section>
    </div>
  );
}
