import { expect, test } from "@playwright/test";
import forkResult from "./fixtures/fork-result.json" with { type: "json" };
import { captureBrowserErrors } from "./browser-errors";

test("renders the hybrid timeline and evidence-based downstream check", async ({
  page,
}) => {
  const browserErrors = captureBrowserErrors(
    page,
    new Set(["/api/cassettes/research-agent/mismatch"]),
  );

  await page.route("**/api/cassettes", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        cassettes: [
          {
            name: "research-agent",
            flow_count: 20,
            created_at: "2026-07-29T08:11:54Z",
            image: {
              ref: "replayable/research-agent:local",
              digest: "sha256:cd398ef53ea7",
            },
            status: "mismatch",
            last_exit_code: 2,
            has_observation: true,
            has_fork_result: true,
          },
        ],
      }),
    });
  });
  await page.route(
    "**/api/cassettes/research-agent/fork-result",
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(forkResult),
      });
    },
  );

  await page.goto("/");

  await expect(page.getByText("HYBRID", { exact: true })).toBeVisible();
  await expect(page.getByText("Hybrid replay complete")).toBeVisible();
  await expect(page.getByText("FORK POINT")).toBeVisible();
  await expect(page.getByText("network resumes here")).toBeVisible();
  await expect(page.getByText("7 FLOWS")).toBeVisible();
  await expect(page.getByText("MODEL CALL").first()).toBeVisible();
  await expect(page.getByText("$0.0410")).toBeVisible();
  await expect(page.getByText("Downstream check")).toBeVisible();
  await expect(page.getByLabel("Similarity score")).toHaveText("92");
  await expect(page.getByText("WITHIN THRESHOLD")).toBeVisible();
  await expect(page.getByText("Same tool sequence")).toBeVisible();
  await expect(page.getByText("Output files", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Compare full run" }).click();
  await expect(
    page.getByRole("dialog", { name: "Full downstream comparison" }),
  ).toContainText("lexical_structural");
  await page.getByRole("button", { name: "Close dialog" }).click();

  await page.screenshot({
    path: "../docs/screenshots/dashboard-screen-b.png",
    fullPage: false,
  });

  let forkPayload: Record<string, unknown> | null = null;
  await page.route(
    "**/api/cassettes/research-agent/fork",
    async (route) => {
      forkPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ exit_code: 2 }),
      });
    },
  );
  await page.getByRole("button", { name: "Replay fork" }).click();
  await page.getByLabel("Fork after flow").fill("2");
  await page.getByRole("button", { name: "Run hybrid replay" }).click();
  await expect(page.getByRole("status")).toContainText("Hybrid replay exited 2");
  expect(forkPayload).toEqual({ fork_at: 2, env_file: null });

  let acceptPayload: Record<string, unknown> | null = null;
  await page.route(
    "**/api/cassettes/research-agent/accept",
    async (route) => {
      acceptPayload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          action: "accept",
          exit_code: 0,
          cassette: "research-agent-hybrid",
        }),
      });
    },
  );
  await page.getByRole("button", { name: "Save as new baseline" }).click();
  await page
    .getByRole("button", { name: "Record baseline", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Saved fresh baseline as research-agent-hybrid",
  );
  expect(acceptPayload).toEqual({
    destination: "research-agent-hybrid",
    env_file: null,
    replace: false,
  });
  expect(browserErrors).toEqual([]);
});
