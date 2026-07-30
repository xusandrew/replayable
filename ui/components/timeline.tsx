import {
  Ban,
  Check,
  CircleEllipsis,
  Cloud,
  MessageSquareText,
} from "lucide-react";
import type { TimelineEvent } from "@/lib/types";

export type EventState = "served" | "mismatch" | "not-reached";

export function eventState(sequence: number, mismatchAt: number | null): EventState {
  if (mismatchAt === null || sequence < mismatchAt) return "served";
  if (sequence === mismatchAt) return "mismatch";
  return "not-reached";
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
}: {
  events: TimelineEvent[];
  mismatchAt: number | null;
  selected: number | null;
  onSelect: (sequence: number) => void;
}) {
  return (
    <div className="timeline" aria-label="Run timeline">
      {events.map((event) => {
        const state = eventState(event.seq, mismatchAt);
        return (
          <button
            className={`timeline-row ${state} ${
              selected === event.seq ? "selected" : ""
            }`}
            key={event.seq}
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
                <span className={`event-state ${state}`}>
                  {state === "not-reached" ? "NOT REACHED" : state.toUpperCase()}
                </span>
              </span>
              <strong>{event.key.replace(/:\d+/, "")}</strong>
              <span className="event-detail">
                {event.kind.replace(".", " ")}
                <span>·</span>
                {event.duration_seconds.toFixed(2)}s
                {event.stream_chunk_count > 0 && (
                  <>
                    <span>·</span>
                    {event.stream_chunk_count} chunks
                  </>
                )}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
