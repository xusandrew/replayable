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
    expect(screen.getAllByText("CHANGED")).toHaveLength(3);
    expect(screen.getByText("report.md")).toBeInTheDocument();
  });
});
