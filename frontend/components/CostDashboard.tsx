import type { UsageResponse } from "@/lib/types";

interface Props {
  usage: UsageResponse;
}

const PRETTY_NAMES: Record<string, string> = {
  menu_parse: "Menu parser",
  fdc_match: "FDC matcher",
  ingredient_match: "FDC matcher",
  distributor_filter: "Places filter",
  rfp_compose: "RFP compose",
  quote_parse: "Quote parser",
  followup_compose: "Follow-up",
};

export function CostDashboard({ usage }: Props) {
  const max = Math.max(
    1,
    ...usage.by_stage.map((s) => Number.parseFloat(s.cost_usd || "0")),
  );
  return (
    <div className="card card-pad">
      <div className="flex items-baseline justify-between gap-3">
        <span className="stage-eyebrow">LLM cost</span>
        <span className="num text-h1 font-semibold text-accent">
          ${Number.parseFloat(usage.total_cost_usd).toFixed(2)}
        </span>
      </div>
      <ul className="mt-3 space-y-1.5">
        {usage.by_stage.map((s) => {
          const cost = Number.parseFloat(s.cost_usd || "0");
          const pct = max > 0 ? (cost / max) * 100 : 0;
          return (
            <li key={s.stage} className="flex items-center gap-2 text-xs">
              <span className="w-28 shrink-0 truncate text-ink-200">
                {PRETTY_NAMES[s.stage] ?? s.stage}
              </span>
              <span className="num w-12 shrink-0 text-right text-ink-100">
                ${cost.toFixed(3)}
              </span>
              <span
                className="h-1 flex-1 rounded-full bg-accent/40"
                style={{ width: `${pct}%`, maxWidth: "100%" }}
                aria-hidden
              />
              <span className="num w-6 shrink-0 text-right text-ink-400">
                {s.calls}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
