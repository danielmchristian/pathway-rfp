"use client";

import { useMemo, useState } from "react";

import { Panel, StageSection } from "@/components/Card";
import { EmailBodyModal } from "@/components/EmailBodyModal";
import type { RfpRequestOut } from "@/lib/types";

interface Props {
  rfp: RfpRequestOut;
  unassignedIngredients: string[];
}

export function StageRfpEmails({ rfp, unassignedIngredients }: Props) {
  const [openEmailId, setOpenEmailId] = useState<number | null>(null);

  const outbound = useMemo(
    () => rfp.emails.filter((e) => e.direction === "out" && !e.in_reply_to),
    [rfp.emails],
  );
  const followups = useMemo(
    () => rfp.emails.filter((e) => e.direction === "out" && e.in_reply_to),
    [rfp.emails],
  );

  const open = openEmailId === null ? null : rfp.emails.find((e) => e.id === openEmailId);

  return (
    <StageSection
      id="stage-rfp-emails"
      eyebrow="Stage 4"
      title="RFP Emails"
      meta={
        <span className="num">
          {outbound.length} sent
          {followups.length > 0 ? ` · ${followups.length} follow-ups` : ""}
          {unassignedIngredients.length > 0
            ? ` · ${unassignedIngredients.length} unassigned`
            : ""}
        </span>
      }
    >
      <p className="text-sm text-ink-300">
        Each RFP scoped to the distributor&rsquo;s specialty-matched
        ingredients. The actual <span className="num text-ink-100">To:</span>{" "}
        uses the demo recipient override (plus-addressed); the nominal
        distributor address is stored alongside for audit. Click any email to
        view the body Claude composed.
      </p>

      <Panel className="space-y-3">
        {outbound.map((e) => {
          const reply = rfp.emails.find(
            (m) =>
              m.direction === "in" &&
              m.distributor_id === e.distributor_id &&
              m.in_reply_to === e.message_id,
          );
          return (
            <button
              key={e.id}
              type="button"
              onClick={() => setOpenEmailId(e.id)}
              className="flex w-full items-start justify-between gap-3 rounded border border-ink-600/60 bg-ink-700/30 px-4 py-3 text-left hover:bg-ink-700/60"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-ink-50">
                  {e.distributor_name ?? "(unattributed)"}
                </div>
                <div className="mt-0.5 truncate text-xs text-ink-300">
                  {e.subject}
                </div>
                <div className="mt-1 truncate text-xs text-ink-400 num">
                  → {e.recipient_actual}
                </div>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1 text-xs">
                <StatusPill status={e.status} />
                {reply ? (
                  <span className="pill border border-good/30 bg-good/10 text-good">
                    replied
                  </span>
                ) : (
                  <span className="pill border border-ink-500/30 text-ink-300">
                    awaiting reply
                  </span>
                )}
              </div>
            </button>
          );
        })}
      </Panel>

      {followups.length > 0 ? (
        <Panel>
          <div className="stage-eyebrow mb-2">Follow-ups sent</div>
          <ul className="space-y-1.5 text-sm">
            {followups.map((f) => (
              <li key={f.id} className="flex items-center justify-between gap-3">
                <span className="text-ink-100">{f.distributor_name}</span>
                <button
                  className="text-xs text-accent hover:underline"
                  onClick={() => setOpenEmailId(f.id)}
                >
                  view
                </button>
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}

      {unassignedIngredients.length > 0 ? (
        <Panel className="border-warn/40 bg-warn/5">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="text-sm font-medium text-warn">
              {unassignedIngredients.length} ingredients unassigned
            </h3>
            <span className="text-xs text-ink-300">
              honest gap — not silently routed
            </span>
          </div>
          <p className="mt-1 text-xs text-ink-300">
            No selected distributor specializes in these items — mostly
            in-house preparations (sauces, dressings) plus items outside the
            distributor cohort.
          </p>
          <details className="mt-2">
            <summary className="cursor-pointer text-xs text-ink-200">
              show list
            </summary>
            <ul className="mt-2 flex flex-wrap gap-1">
              {unassignedIngredients.map((u) => (
                <li
                  key={u}
                  className="pill border border-ink-500/40 text-ink-200"
                >
                  {u}
                </li>
              ))}
            </ul>
          </details>
        </Panel>
      ) : null}

      {open ? (
        <EmailBodyModal
          open
          onClose={() => setOpenEmailId(null)}
          subject={open.subject ?? "(no subject)"}
          recipient={open.recipient_actual ?? "(no recipient)"}
          body={open.body ?? "(empty body)"}
          meta={[
            { label: "Message-ID", value: open.message_id ?? "—" },
            { label: "Status", value: open.status },
            {
              label: "Sent",
              value: open.sent_at
                ? new Date(open.sent_at).toLocaleString()
                : "—",
            },
            {
              label: "Nominal",
              value: open.recipient_nominal ?? "—",
            },
          ]}
        />
      ) : null}
    </StageSection>
  );
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "sent"
      ? "border-good/30 bg-good/10 text-good"
      : status === "failed"
        ? "border-bad/30 bg-bad/10 text-bad"
        : "border-ink-500/30 text-ink-300";
  return <span className={`pill border ${tone}`}>{status}</span>;
}
