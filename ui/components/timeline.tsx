import {
  Ban,
  Check,
  CircleEllipsis,
  Cloud,
  MessageSquareText,
} from "lucide-react";
import { Fragment } from "react";
import type { TimelineEvent } from "@/lib/types";

export type EventState = "served" | "mismatch" | "not-reached";

export function eventState(sequence: number, mismatchAt: number | null): EventState {
  if (mismatchAt === null || sequence < mismatchAt) return "served";
  if (sequence === mismatchAt) return "mismatch";
  return "not-reached";
}

export function hybridTimeline(
  baseline: TimelineEvent[],
  forkAt: number,
  live: TimelineEvent[],
): TimelineEvent[] {
  return [
    ...baseline.filter((event) => event.seq <= forkAt),
    ...live.map((event, index) => ({
      ...event,
      seq: forkAt + index + 1,
      lamport: forkAt + index + 1,
    })),
  ];
}

function eventKind(event: TimelineEvent): string {
  if (event.metrics?.model) return "MODEL CALL";
  if (event.kind === "tool.call") return "TOOL CALL";
  return event.kind.replace(".", " ").toUpperCase();
}

function EventIcon({
  event,
  state,
}: {
  event: TimelineEvent;
  state: EventState;
}) {
  if (state === "mismatch") return <Ban size={15} strokeWidth={2.4} />;
  if (state === "not-reached") return <CircleEllipsis size={15} />;
  if (event.channel === "model") return <MessageSquareText size={15} />;
  if (event.channel === "network") return <Cloud size={15} />;
  return <Check size={15} />;
}

export function Timeline({
  events,
  mismatchAt,
  selected,
  onSelect,
  forkAt = null,
}: {
  events: TimelineEvent[];
  mismatchAt: number | null;
  selected: number | null;
  onSelect: (sequence: number) => void;
  forkAt?: number | null;
}) {
  return (
    <div className="timeline" aria-label="Run timeline">
      {events.map((event) => {
        const state = forkAt === null ? eventState(event.seq, mismatchAt) : "served";
        const segment =
          forkAt === null ? null : event.seq <= forkAt ? "pinned" : "live";
        return (
          <Fragment key={event.seq}>
            {forkAt !== null && event.seq === forkAt + 1 && (
              <div className="fork-divider">
                <span>FORK POINT</span>
                <i />
                <strong>network resumes here</strong>
              </div>
            )}
            <button
              className={`timeline-row ${state} ${segment ?? ""} ${
                selected === event.seq ? "selected" : ""
              }`}
              onClick={() => onSelect(event.seq)}
              type="button"
            >
              <span className="timeline-rail">
                <span className="timeline-icon">
                  <EventIcon event={event} state={state} />
                </span>
              </span>
              <span className="event-main">
                <span className="event-meta">
                  <span>Flow {event.seq}</span>
                  <span className={`event-state ${segment ?? state}`}>
                    {segment
                      ? segment.toUpperCase()
                      : state === "not-reached"
                        ? "NOT REACHED"
                        : state.toUpperCase()}
                  </span>
                </span>
                <strong>{event.key.replace(/:\d+/, "")}</strong>
                <span className="event-detail">
                  {eventKind(event)}
                  <span>·</span>
                  {event.duration_seconds.toFixed(2)}s
                  {event.metrics?.estimated_cost_usd !== undefined && (
                    <>
                      <span>·</span>$
                      {event.metrics.estimated_cost_usd.toFixed(4)}
                    </>
                  )}
                  {event.stream_chunk_count > 0 && (
                    <>
                      <span>·</span>
                      {event.stream_chunk_count} chunks
                    </>
                  )}
                </span>
              </span>
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}
