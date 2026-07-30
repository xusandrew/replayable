import type { Page } from "@playwright/test";

export function captureBrowserErrors(
  page: Page,
  expectedNotFoundPaths: ReadonlySet<string>,
): string[] {
  const errors: string[] = [];

  page.on("console", (message) => {
    if (
      message.type() === "error" &&
      !message.text().startsWith("Failed to load resource:")
    ) {
      errors.push(message.text());
    }
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() < 400) return;

    const path = new URL(response.url()).pathname;
    if (response.status() === 404 && expectedNotFoundPaths.has(path)) return;

    errors.push(`${response.status()} ${path}`);
  });

  return errors;
}
