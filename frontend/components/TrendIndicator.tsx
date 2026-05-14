import type { Trend } from "@/lib/types";

interface Props {
  direction: Trend;
  delta?: string | null;
}

// Asymmetric color: rising price = bad for the buyer = rose;
// falling price = good = emerald. Unknown / flat are neutral.
export function TrendIndicator({ direction, delta }: Props) {
  let glyph = "—";
  let tone = "text-ink-300";
  let display: string | null = null;

  if (direction === "up") {
    glyph = "▲";
    tone = "text-bad";
  } else if (direction === "down") {
    glyph = "▼";
    tone = "text-good";
  } else if (direction === "flat") {
    glyph = "→";
    tone = "text-ink-200";
  }

  if (delta && direction !== "unknown") {
    const num = Number.parseFloat(delta);
    if (!Number.isNaN(num)) {
      const sign = num > 0 ? "+" : "";
      display = `${sign}${num.toFixed(1)}%`;
    }
  }

  if (direction === "unknown") {
    return (
      <span className="text-ink-400 text-xs italic">no trend</span>
    );
  }

  return (
    <span className={`inline-flex items-center gap-1 text-xs ${tone}`}>
      <span aria-hidden>{glyph}</span>
      <span className="num">{display ?? "—"}</span>
    </span>
  );
}
