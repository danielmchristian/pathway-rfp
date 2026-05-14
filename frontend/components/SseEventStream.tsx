"use client";

import { useEffect, useRef, useState } from "react";

interface Props {
  url: string;
  /** Visible cap on event rows. */
  maxRows?: number;
}

interface RawEvent {
  ts: string;
  name: string;
  payload: Record<string, unknown>;
}

// Live tail of the backend's SSE bus. The component owns the EventSource
// lifecycle so re-mounting (after a trigger fires) doesn't double-subscribe.
export function SseEventStream({ url, maxRows = 8 }: Props) {
  const [events, setEvents] = useState<RawEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const es = new EventSource(url);
    esRef.current = es;
    setConnected(false);

    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);

    // The backend SSE emits per stage:status events. EventSource exposes a
    // single onmessage stream when events use the default `message` name,
    // but our backend uses named events (e.g. `inbox_poll:start`). We
    // listen via addEventListener with a small known prefix list — and
    // also catch the default message channel as a safety net.
    const KNOWN = [
      "menu_parse",
      "ingredient_match",
      "distributor_discovery",
      "distributor_filter",
      "rfp_send",
      "rfp_compose",
      "rfp_resend",
      "quote_collection",
      "inbox_poll",
      "quote_parse",
      "followup",
      "recommendation",
    ];
    const statuses = ["start", "progress", "complete", "error"];

    const handler = (e: MessageEvent) => {
      try {
        const parsed = JSON.parse(e.data);
        setEvents((prev) =>
          [
            {
              ts: parsed.ts ?? new Date().toISOString(),
              name: parsed.name ?? e.type,
              payload: parsed.payload ?? {},
            },
            ...prev,
          ].slice(0, maxRows),
        );
      } catch {
        // ignore malformed
      }
    };

    KNOWN.forEach((stage) =>
      statuses.forEach((s) =>
        es.addEventListener(`${stage}:${s}`, handler as EventListener),
      ),
    );
    es.onmessage = handler;

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [url, maxRows]);

  return (
    <div className="flex items-center gap-2 text-xs">
      <span
        className={`inline-block h-2 w-2 rounded-full ${
          connected ? "bg-good animate-pulse" : "bg-ink-500"
        }`}
        aria-hidden
      />
      <span className="text-ink-300">
        {connected ? "live events" : "disconnected"}
      </span>
      {events.length > 0 ? (
        <ul className="ml-2 flex max-w-3xl gap-1.5 overflow-x-auto">
          {events.slice(0, 4).map((evt, idx) => (
            <li
              key={`${evt.ts}-${idx}`}
              className="pill border border-ink-500/40 bg-ink-700/40 text-ink-100 whitespace-nowrap"
              title={JSON.stringify(evt.payload).slice(0, 200)}
            >
              <span className="num text-ink-400">
                {new Date(evt.ts).toLocaleTimeString().slice(0, 8)}
              </span>
              <span>{evt.name}</span>
            </li>
          ))}
        </ul>
      ) : (
        <span className="text-ink-500 italic">no recent events</span>
      )}
    </div>
  );
}
