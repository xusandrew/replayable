import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "./dashboard";
import { eventState } from "./timeline";
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
});

describe("Dashboard", () => {
  it("keeps a reviewable demo state when the local API is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<Dashboard />);

    expect(
      screen.getByRole("heading", { name: "Research Agent" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Behavior changed")).toBeInTheDocument();
    expect(screen.getByText("2/7")).toBeInTheDocument();
    expect(screen.getByText("request_id ignored")).toBeInTheDocument();
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/cassettes", undefined),
    );
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

  it("opens the full diff and safe fresh-baseline dialogs", () => {
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
      name: "Record fresh baseline",
    });
    expect(dialog).toHaveTextContent("never overwritten");
    expect(screen.getByLabelText("Destination name")).toHaveValue(
      "research-agent-fresh",
    );
  });
});
