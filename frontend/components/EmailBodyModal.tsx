"use client";

import { useEffect } from "react";

interface Props {
  open: boolean;
  onClose: () => void;
  subject: string;
  recipient: string;
  body: string;
  meta?: { label: string; value: string }[];
}

export function EmailBodyModal({
  open,
  onClose,
  subject,
  recipient,
  body,
  meta = [],
}: Props) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/80 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="card max-h-[85vh] w-full max-w-3xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-3 border-b border-ink-600/60 px-6 py-4">
          <div className="min-w-0">
            <div className="stage-eyebrow">RFP email</div>
            <h3 className="truncate text-base font-medium text-ink-50">
              {subject}
            </h3>
            <p className="mt-1 truncate text-xs text-ink-300 num">
              → {recipient}
            </p>
          </div>
          <button
            onClick={onClose}
            className="btn btn-ghost"
            aria-label="Close"
          >
            close
          </button>
        </header>
        {meta.length > 0 ? (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 border-b border-ink-700/50 px-6 py-3 text-xs">
            {meta.map((m) => (
              <div key={m.label} className="flex gap-2">
                <dt className="text-ink-400">{m.label}:</dt>
                <dd className="num truncate text-ink-200">{m.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        <div className="max-h-[60vh] overflow-y-auto px-6 py-4">
          <pre className="whitespace-pre-wrap text-sm text-ink-100 font-sans">
            {body}
          </pre>
        </div>
      </div>
    </div>
  );
}
