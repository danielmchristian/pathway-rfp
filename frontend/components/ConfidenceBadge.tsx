// Traffic-light confidence pill. The above-and-beyond surface for the
// parser's per-dish / per-ingredient confidence scores so a viewer can
// see at a glance where the model was certain vs guessing.

interface Props {
  value: number | null;
  /** Tiny variant for inline use inside cards. */
  size?: "sm" | "xs";
}

export function ConfidenceBadge({ value, size = "sm" }: Props) {
  if (value === null || value === undefined) {
    return (
      <span className="pill border border-ink-500/40 text-ink-300">
        <span className="font-mono">n/a</span>
      </span>
    );
  }
  let tone = "text-bad border-bad/30 bg-bad/10";
  let label = "low";
  if (value >= 0.8) {
    tone = "text-good border-good/30 bg-good/10";
    label = "high";
  } else if (value >= 0.5) {
    tone = "text-warn border-warn/30 bg-warn/10";
    label = "med";
  }
  return (
    <span
      className={`pill border ${tone} ${size === "xs" ? "text-[0.65rem] px-1.5" : ""}`}
      title={`Confidence ${(value * 100).toFixed(0)}%`}
    >
      <span className="font-mono">{value.toFixed(2)}</span>
      <span className="opacity-60">{label}</span>
    </span>
  );
}
