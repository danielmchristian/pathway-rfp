"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { finalizeRfp, pollInbox } from "@/lib/api";

interface Props {
  rfpId: number;
}

// Footer triggers — the on-camera moment. Each click posts to the backend,
// shows progress feedback, then triggers a full page revalidation so stages
// 5 & 6 re-fetch and re-render in place (no jank, no flash).
export function PipelineTriggers({ rfpId }: Props) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [busy, setBusy] = useState<"poll" | "finalize" | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const handle = async (action: "poll" | "finalize") => {
    if (busy) return;
    setBusy(action);
    setFeedback("running…");
    try {
      if (action === "poll") {
        const result = await pollInbox(rfpId, false);
        const fb = [
          `inbound: ${result.inbound_count}`,
          `attributed: ${result.attributed_count}`,
          result.unattributed_count
            ? `unattributed: ${result.unattributed_count}`
            : null,
          result.followups.length
            ? `follow-ups: ${result.followups.length}`
            : null,
          result.poll_error ? `error: ${result.poll_error}` : null,
        ]
          .filter(Boolean)
          .join(" · ");
        setFeedback(fb || "completed");
      } else {
        const rec = await finalizeRfp(rfpId);
        const pick = rec.pick?.distributor_name ?? "no pick";
        setFeedback(
          `pick: ${pick}${rec.pick ? ` (${rec.pick.score.toFixed(2)})` : ""}`,
        );
      }
      startTransition(() => router.refresh());
    } catch (err) {
      setFeedback(
        err instanceof Error ? err.message.slice(0, 200) : "request failed",
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="stage-eyebrow text-ink-200">Pipeline</span>
      <button
        type="button"
        onClick={() => handle("poll")}
        disabled={busy !== null || pending}
        className="btn btn-primary"
      >
        {busy === "poll" ? "polling…" : "Poll inbox"}
      </button>
      <button
        type="button"
        onClick={() => handle("finalize")}
        disabled={busy !== null || pending}
        className="btn btn-ghost"
      >
        {busy === "finalize" ? "finalizing…" : "Finalize"}
      </button>
      {feedback ? (
        <span className="ml-1 text-xs text-ink-300 truncate max-w-md">
          {feedback}
        </span>
      ) : null}
    </div>
  );
}
