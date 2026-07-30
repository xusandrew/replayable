import type { Explain, FlowDetail, Mismatch } from "./types";

/**
 * The matcher compares *normalized* requests, never raw wire bytes.
 *
 * `mismatch.live_request.canonical_body` is what the live request normalized
 * to; `explain.canonical_body` is what the recorded flow normalized to. Both
 * are compact, key-sorted JSON with volatile fields replaced by the sentinel.
 * The raw `flow.request.body_decoded` is none of those things — diffing it
 * against a canonical body reports every re-ordered key and every ignored
 * field as a behavioural change, which is exactly the opposite of what the
 * panel claims to show.
 *
 * So: render both sides through the same pipeline, and say so in the labels.
 */

export type DiffPanes = {
  recorded: string;
  live: string;
  /** True when both sides are the matcher's normalized view of the request. */
  normalized: boolean;
};

/** Re-indent canonical JSON so a human can read it; pass anything else through. */
export function prettyCanonicalBody(value: string): string {
  const trimmed = value.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return value;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return value;
  }
}

export function diffPanes(
  flow: FlowDetail | null,
  mismatch: Mismatch | null,
  explain: Explain | null,
): DiffPanes {
  const liveCanonical = mismatch?.live_request.canonical_body;
  const recordedCanonical = explain?.canonical_body;
  if (liveCanonical !== undefined && recordedCanonical !== undefined) {
    return {
      recorded: prettyCanonicalBody(recordedCanonical),
      live: prettyCanonicalBody(liveCanonical),
      normalized: true,
    };
  }
  // No mismatch to show: display the recorded request on both sides rather
  // than inventing a comparison the API did not provide.
  const raw = flow?.request.body_decoded ?? "";
  return { recorded: raw, live: raw, normalized: false };
}

/**
 * Top-level JSON fields whose normalized values differ between the two sides.
 *
 * Drives the `changed` badges next to the `ignored` ones, so both come from
 * the real ruleset output instead of a hard-coded example.
 */
export function changedFields(recorded: string, live: string): string[] {
  let left: unknown;
  let right: unknown;
  try {
    left = JSON.parse(recorded);
    right = JSON.parse(live);
  } catch {
    return [];
  }
  if (
    typeof left !== "object" ||
    left === null ||
    Array.isArray(left) ||
    typeof right !== "object" ||
    right === null ||
    Array.isArray(right)
  ) {
    return [];
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const names = new Set([...Object.keys(leftRecord), ...Object.keys(rightRecord)]);
  return [...names]
    .filter(
      (name) => JSON.stringify(leftRecord[name]) !== JSON.stringify(rightRecord[name]),
    )
    .sort();
}
