import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TokenDiff } from "./token-diff";

afterEach(cleanup);

describe("TokenDiff", () => {
  it("shows one pane and no highlighting when the replay matched", () => {
    render(<TokenDiff live="ignored" recorded='{"a":1}' single />);

    expect(screen.getByText("Recorded request — served on replay")).toBeInTheDocument();
    expect(screen.queryByTestId("added-pane")).not.toBeInTheDocument();
    expect(
      screen.getByTestId("removed-pane").querySelectorAll(".token-removed"),
    ).toHaveLength(0);
  });

  it("explains an empty body instead of rendering a blank panel", () => {
    render(<TokenDiff live="" recorded="" single />);

    expect(screen.getByText("This request has no body.")).toBeInTheDocument();
  });

  it("suppresses highlighting when the panes are not comparable", () => {
    render(
      <TokenDiff comparable={false} live='{"a":2}' recorded='{"a":1}' />,
    );

    expect(
      screen.getByTestId("removed-pane").querySelectorAll(".token-removed"),
    ).toHaveLength(0);
    expect(screen.getByText("Recorded request (raw)")).toBeInTheDocument();
    expect(screen.getByText("Replay request (normalized)")).toBeInTheDocument();
  });
});
