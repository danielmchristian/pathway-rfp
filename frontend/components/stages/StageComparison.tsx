import { EmptyState, Panel, StageSection } from "@/components/Card";
import type { ComparisonResponse, IngredientSummaryRow } from "@/lib/types";

interface Props {
  comparison: ComparisonResponse | null;
  ingredientSummary: IngredientSummaryRow[];
}

// Rows are grouped by FDC category so the per-distributor basket is
// legible (Carolina Fresh quotes produce, Queen City quotes proteins, etc.).
export function StageComparison({ comparison, ingredientSummary }: Props) {
  if (!comparison || comparison.rows.length === 0 || comparison.distributors.length === 0) {
    return (
      <StageSection
        id="stage-comparison"
        eyebrow="Stage 5"
        title="Quotes & Comparison"
      >
        <EmptyState
          icon="◌"
          title="Awaiting distributor quotes"
          hint={
            <>
              Reply to the RFP emails in your inbox, then click{" "}
              <span className="rounded bg-ink-700 px-1.5 py-0.5 text-ink-100">
                Poll Inbox
              </span>{" "}
              below to parse the replies. This panel will populate as quotes
              arrive.
            </>
          }
        />
      </StageSection>
    );
  }

  const categoryByIngredient = new Map<number, string | null>();
  ingredientSummary.forEach((r) => {
    categoryByIngredient.set(r.ingredient_id, r.fdc_category);
  });

  // Group ingredients by FDC category (falling back to "Unclassified").
  const groups = new Map<string, typeof comparison.rows>();
  for (const row of comparison.rows) {
    const cat = categoryByIngredient.get(row.ingredient_id) ?? "Unclassified";
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat)!.push(row);
  }
  const orderedGroups = [...groups.entries()].sort((a, b) =>
    a[0].localeCompare(b[0]),
  );

  // Per-distributor coverage chip in the column header.
  const coverageByDistributor = new Map<number, number>();
  for (const d of comparison.distributors) {
    let quoted = 0;
    for (const row of comparison.rows) {
      const cell = row.cells[String(d.id)];
      if (cell && cell.unit_price !== null && !cell.missing_fields.includes("no_quote")) {
        quoted += 1;
      }
    }
    coverageByDistributor.set(d.id, quoted);
  }
  const totalRequested = comparison.rows.length;

  return (
    <StageSection
      id="stage-comparison"
      eyebrow="Stage 5"
      title="Quotes & Comparison"
      meta={
        <span className="num">
          {totalRequested} items · {comparison.distributors.length} distributors
        </span>
      }
    >
      <p className="text-sm text-ink-300">
        Distributors quote different baskets — coverage chips on each column
        header show how many of the requested items each one actually quoted.
        Empty cells mean &ldquo;no quote received&rdquo;, distinct from a
        cell with a NULL price.
      </p>

      <Panel className="overflow-x-auto p-0">
        <table className="w-full min-w-[700px] text-sm">
          <thead className="bg-ink-800/60">
            <tr className="border-b border-ink-600 text-left text-xs text-ink-300">
              <th className="sticky left-0 z-10 bg-ink-800/95 px-4 py-3 font-medium">
                Ingredient
              </th>
              {comparison.distributors.map((d) => {
                const quoted = coverageByDistributor.get(d.id) ?? 0;
                const pct = Math.round((quoted / totalRequested) * 100);
                return (
                  <th
                    key={d.id}
                    className="px-3 py-3 text-left font-medium min-w-[170px]"
                  >
                    <div className="text-ink-100">{d.name}</div>
                    <div className="mt-1 text-[0.65rem] uppercase tracking-wider num">
                      <span
                        className={`pill border ${
                          pct >= 80
                            ? "border-good/40 text-good"
                            : pct >= 40
                              ? "border-warn/40 text-warn"
                              : "border-ink-500/40 text-ink-300"
                        }`}
                      >
                        {quoted}/{totalRequested} · {pct}%
                      </span>
                    </div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {orderedGroups.map(([cat, rows]) => (
              <CategoryGroup
                key={cat}
                category={cat}
                rows={rows}
                distributors={comparison.distributors}
              />
            ))}
          </tbody>
        </table>
      </Panel>
    </StageSection>
  );
}

function CategoryGroup({
  category,
  rows,
  distributors,
}: {
  category: string;
  rows: ComparisonResponse["rows"];
  distributors: ComparisonResponse["distributors"];
}) {
  return (
    <>
      <tr className="bg-ink-700/30">
        <td
          colSpan={distributors.length + 1}
          className="sticky left-0 px-4 py-1.5 text-xs uppercase tracking-wider text-ink-300"
        >
          {category}
        </td>
      </tr>
      {rows.map((r) => (
        <tr
          key={r.ingredient_id}
          className="border-b border-ink-700/40 last:border-b-0"
        >
          <td className="sticky left-0 z-10 bg-ink-800/95 px-4 py-2">
            <div className="text-ink-100">{r.ingredient_name}</div>
            {r.requested_quantity ? (
              <div className="num text-[0.7rem] text-ink-400">
                ~{r.requested_quantity} {r.requested_unit}
              </div>
            ) : (
              <div className="text-[0.7rem] italic text-ink-400">TBD qty</div>
            )}
          </td>
          {distributors.map((d) => (
            <Cell key={d.id} cell={r.cells[String(d.id)] ?? null} />
          ))}
        </tr>
      ))}
    </>
  );
}

function Cell({ cell }: { cell: ComparisonResponse["rows"][number]["cells"][string] | null }) {
  if (!cell || cell.missing_fields.includes("no_quote")) {
    return (
      <td className="px-3 py-2 text-xs italic text-ink-500">no quote</td>
    );
  }
  if (cell.unit_price === null) {
    return (
      <td className="px-3 py-2">
        <span className="pill border border-warn/40 bg-warn/5 text-warn">
          no price
        </span>
      </td>
    );
  }
  return (
    <td className="px-3 py-2">
      <div className="num text-ink-50">
        ${cell.unit_price}
        <span className="text-ink-400"> /{cell.unit ?? "—"}</span>
      </div>
      <div className="mt-0.5 flex gap-2 text-[0.7rem] text-ink-400 num">
        {cell.min_order_qty !== null ? (
          <span>MOQ {cell.min_order_qty}</span>
        ) : (
          <span className="italic">moq —</span>
        )}
        {cell.delivery_days !== null ? (
          <span>{cell.delivery_days}d</span>
        ) : (
          <span className="italic">delivery —</span>
        )}
      </div>
      {cell.missing_fields.length > 0 ? (
        <div className="mt-1 text-[0.65rem] text-warn">
          missing: {cell.missing_fields.join(", ")}
        </div>
      ) : null}
    </td>
  );
}
