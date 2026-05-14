import type { ReactNode } from "react";

interface SectionProps {
  eyebrow: string;
  title: string;
  meta?: ReactNode;
  children: ReactNode;
  id?: string;
}

// Top-level stage wrapper used by every pipeline section so the page
// reads as a coherent vertical scroll.
export function StageSection({
  eyebrow,
  title,
  meta,
  children,
  id,
}: SectionProps) {
  return (
    <section id={id} className="scroll-mt-24 space-y-4">
      <header className="flex items-baseline justify-between gap-4 border-b border-ink-600/60 pb-2">
        <div>
          <div className="stage-eyebrow">{eyebrow}</div>
          <h2 className="stage-heading">{title}</h2>
        </div>
        {meta ? <div className="text-sm text-ink-200">{meta}</div> : null}
      </header>
      {children}
    </section>
  );
}

interface PanelProps {
  children: ReactNode;
  className?: string;
}

export function Panel({ children, className = "" }: PanelProps) {
  return (
    <div className={`card card-pad ${className}`}>{children}</div>
  );
}

interface EmptyStateProps {
  title: string;
  hint?: ReactNode;
  icon?: string;
}

// Intentional empty state — used for Stages 5 & 6 before any quotes
// land. Quiet, instructional, never an error.
export function EmptyState({ title, hint, icon = "○" }: EmptyStateProps) {
  return (
    <div className="card card-pad flex flex-col items-center justify-center gap-3 py-12 text-center">
      <div className="text-3xl text-ink-400" aria-hidden>
        {icon}
      </div>
      <div className="text-base text-ink-100">{title}</div>
      {hint ? <div className="max-w-md text-sm text-ink-300">{hint}</div> : null}
    </div>
  );
}
