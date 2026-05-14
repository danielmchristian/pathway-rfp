import { Panel, StageSection } from "@/components/Card";
import { TrendIndicator } from "@/components/TrendIndicator";
import type { IngredientSummaryRow } from "@/lib/types";

interface Props {
  rows: IngredientSummaryRow[];
}

export function StagePricing({ rows }: Props) {
  const priced = rows.filter((r) => !r.pricing_unavailable && r.latest_price_per_unit);
  const unavailable = rows.filter((r) => r.pricing_unavailable);
  const unmatched = rows.filter((r) => !r.fdc_id);

  return (
    <StageSection
      id="stage-pricing"
      eyebrow="Stage 2"
      title="Ingredient Pricing"
      meta={
        <span className="num">
          {priced.length} priced · {unavailable.length} no AMS feed ·{" "}
          {unmatched.length} unmatched
        </span>
      }
    >
      <p className="text-sm text-ink-300">
        FoodData Central matches paired with Agricultural Marketing Service
        commodity prices (Atlanta Terminal Market, retail-tier). Rows with no
        AMS feed are flagged honestly rather than imputed.
      </p>

      <Panel className="overflow-hidden p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-ink-600 text-left text-xs uppercase tracking-wider text-ink-300">
              <th className="px-4 py-2 font-medium">Ingredient</th>
              <th className="px-4 py-2 font-medium">FDC Category</th>
              <th className="px-4 py-2 text-right font-medium">Latest price</th>
              <th className="px-4 py-2 font-medium">30-day trend</th>
              <th className="px-4 py-2 text-right font-medium">Obs</th>
              <th className="px-4 py-2 font-medium">Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.ingredient_id}
                className="border-b border-ink-700/50 last:border-b-0"
              >
                <td className="px-4 py-2">
                  <div className="text-ink-100">{r.ingredient_name}</div>
                  {r.fdc_id ? (
                    <div className="text-xs text-ink-400 num">FDC {r.fdc_id}</div>
                  ) : (
                    <div className="text-xs italic text-ink-400">unmatched</div>
                  )}
                </td>
                <td className="px-4 py-2 text-ink-200 text-xs">
                  {r.fdc_category ?? <span className="italic text-ink-400">—</span>}
                </td>
                <td className="px-4 py-2 text-right">
                  {r.pricing_unavailable || !r.latest_price_per_unit ? (
                    <span className="pill border border-ink-500/40 text-ink-300">
                      no AMS feed
                    </span>
                  ) : (
                    <span className="num text-ink-50">
                      ${r.latest_price_per_unit}{" "}
                      <span className="text-ink-400">/{r.unit_normalized}</span>
                    </span>
                  )}
                </td>
                <td className="px-4 py-2">
                  <TrendIndicator
                    direction={r.direction}
                    delta={r.delta_pct_30d}
                  />
                </td>
                <td className="px-4 py-2 text-right num text-ink-200">
                  {r.observations_count}
                </td>
                <td className="px-4 py-2 text-xs text-ink-300">
                  {r.source ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </StageSection>
  );
}
