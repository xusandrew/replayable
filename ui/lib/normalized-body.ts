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

export const NO_RECORDED_REQUEST = "No recorded request is available.";

export type DiffPanes = {
  recorded: string;
  live: string;
  /** True when both sides are the matcher's normalized view of the request. */
  normalized: boolean;
  /**
   * False when the two panes are in different representations and a
   * token-level diff between them would be meaningless. Defaults to true.
   */
  comparable?: boolean;
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
  if (liveCanonical !== undefined) {
    const live = prettyCanonicalBody(liveCanonical);
    // Two very different situations reach here, and saying the wrong one is
    // the failure mode this module exists to avoid:
    //
    //  - there genuinely is no recorded candidate (empty cassette), or
    //  - the recorded flow loaded but /explain did not, so we have the raw
    //    request and simply cannot normalize it.
    //
    // Only the first may claim the request does not exist. `/explain` fails
    // independently of `/flows/N` — a cassette pinning a malformed
    // replayable.toml is enough — so this is a reachable state, not a
    // theoretical one.
    if (flow === null) {
      return { recorded: NO_RECORDED_REQUEST, live, normalized: false };
    }
    return {
      recorded: flow.request.body_decoded,
      live,
      // The recorded side is raw and the live side is canonical, so the panes
      // are not comparable; the caller must not present this as a diff.
      normalized: false,
      comparable: false,
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
