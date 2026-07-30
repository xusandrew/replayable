import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ForkResult } from "@/lib/types";
import fixture from "../e2e/fixtures/fork-result.json";
import { DownstreamCheck } from "./downstream-check";

afterEach(cleanup);

describe("DownstreamCheck", () => {
  it("renders persisted components and dispatches both actions", () => {
    const save = vi.fn();
    const compare = vi.fn();
    render(
      <DownstreamCheck
        onCompare={compare}
        onSave={save}
        result={fixture as ForkResult}
      />,
    );

    expect(screen.getByText("92")).toBeInTheDocument();
    expect(screen.getByText("87%")).toBeInTheDocument();
    expect(screen.getByText("Same tool sequence")).toBeInTheDocument();
    expect(screen.getAllByText("MATCH")).toHaveLength(3);
    // The transcript is the only failing gate in this fixture; it must be shown.
    expect(screen.getByText("Agent transcript")).toBeInTheDocument();
    expect(screen.getByText("CHANGED")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Compare full run/ }));
    fireEvent.click(screen.getByRole("button", { name: /Save as new baseline/ }));
    expect(compare).toHaveBeenCalledOnce();
    expect(save).toHaveBeenCalledOnce();
  });

  it("renders a below-threshold result and failed structural checks", () => {
    const result = structuredClone(fixture) as ForkResult;
    result.downstream.similarity.score = 0.42;
    result.downstream.similarity.passes = false;
    result.downstream.tool_calls.matches = false;
    result.downstream.workspace.matches = false;
    result.downstream.workspace.diff.changed = ["report.md"];
    result.downstream.exit_code.matches = false;

    render(
      <DownstreamCheck
        onCompare={vi.fn()}
        onSave={vi.fn()}
        result={result}
      />,
    );

    expect(screen.getByText("BELOW THRESHOLD")).toBeInTheDocument();
    expect(screen.getAllByText("CHANGED")).toHaveLength(4);
    expect(screen.getByText("report.md")).toBeInTheDocument();
  });

  it("states the run's real verdict so a green pill cannot imply a green exit", () => {
    const result = structuredClone(fixture) as ForkResult;
    // The gate `replayable` actually exits on is byte-exact equality; the
    // similarity score is advisory and can pass while the run fails.
    result.exit_code = 2;
    result.downstream.matches = false;
    result.downstream.similarity.passes = true;

    render(
      <DownstreamCheck onCompare={vi.fn()} onSave={vi.fn()} result={result} />,
    );

    expect(screen.getByText("WITHIN THRESHOLD")).toBeInTheDocument();
    expect(
      screen.getByText(/Run verdict: exit 2 · not byte-identical/),
    ).toBeInTheDocument();
  });
});
