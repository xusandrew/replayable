import { expect, test } from "@playwright/test";
import { captureBrowserErrors } from "./browser-errors";

test("shows a real cassette mismatch as the offline Screen A dashboard", async ({
  page,
}) => {
  const browserErrors = captureBrowserErrors(
    page,
    new Set(["/api/cassettes/research-agent/fork-result"]),
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
            has_fork_result: false,
          },
        ],
      }),
    });
  });
  await page.route("**/api/cassettes/research-agent/mismatch", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        live_request: {
          method: "POST",
          host: "api.anthropic.com",
          path: "/v1/messages",
          canonical_body: JSON.stringify(
            {
              model: "claude-haiku-4-5",
              system:
                "You are a verbose research agent. Research the user's topic and prepare a fact-based report.",
              request_id: "§VOLATILE§",
            },
            null,
            2,
          ),
        },
        nearest_candidates: [{ seq: 3 }],
        diff: '- "concise"\n+ "verbose"',
      }),
    });
  });
  await page.route("**/api/cassettes/research-agent/timeline", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.events = payload.events.map((event: { seq: number; key: string }) =>
      event.seq === 3
        ? { ...event, key: "POST api.anthropic.com:443/v1/messages" }
        : event,
    );
    await route.fulfill({ response, json: payload });
  });
  await page.route("**/api/cassettes/research-agent/flows/3", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        seq: 3,
        key: {
          method: "POST",
          host: "api.anthropic.com",
          port: 443,
          path: "/v1/messages",
        },
        request: {
          query: "",
          headers: [["content-type", "application/json"]],
          body_decoded: JSON.stringify(
            {
              model: "claude-haiku-4-5",
              system:
                "You are a concise research agent. Research the user's topic and prepare a fact-based report.",
              request_id: "req_018fa2",
            },
            null,
            2,
          ),
        },
        response: { status: 200, headers: [], body_decoded: "" },
        timing: { started: 0.91, completed: 1.13 },
      }),
    });
  });

  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Research Agent" })).toBeVisible();
  await expect(page.getByText("Behavior changed")).toBeVisible();
  await expect(page.getByText("OFFLINE")).toBeVisible();
  await expect(page.getByText("MISMATCH", { exact: true })).toBeVisible();
  await expect(page.getByText("request_id ignored")).toBeVisible();
  await expect(page.getByText("system changed")).toBeVisible();
  await expect(page.getByText("Recorded request")).toBeVisible();
  await expect(page.getByText("Replay request")).toBeVisible();
  await page.getByRole("button", { name: "View full diff" }).click();
  await expect(
    page.getByRole("dialog", { name: "Full matcher diff" }),
  ).toContainText("concise");
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.locator(".strict-control").click();
  await expect(page.getByRole("checkbox", { name: /Strict mode/ })).not.toBeChecked();
  await page.locator(".strict-control").click();

  await page.screenshot({
    path: "../docs/screenshots/dashboard-screen-a.png",
    fullPage: false,
  });
  expect(browserErrors).toEqual([]);
});
