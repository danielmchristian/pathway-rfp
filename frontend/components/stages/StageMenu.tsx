import { ConfidenceBadge } from "@/components/ConfidenceBadge";
import { Panel, StageSection } from "@/components/Card";
import type { DishOut } from "@/lib/types";

interface Props {
  restaurantName: string;
  dishes: DishOut[];
}

export function StageMenu({ restaurantName, dishes }: Props) {
  const dishCount = dishes.length;
  const ingredientCount = new Set(
    dishes.flatMap((d) => d.ingredients.map((i) => i.id)),
  ).size;
  const lowConfidence = dishes.filter(
    (d) => (d.parse_confidence ?? 0) < 0.7,
  ).length;

  return (
    <StageSection
      id="stage-menu"
      eyebrow="Stage 1"
      title="Menu → Recipes"
      meta={
        <span className="num">
          {dishCount} dishes · {ingredientCount} ingredients
          {lowConfidence > 0 ? (
            <>
              {" · "}
              <span className="text-warn">{lowConfidence} low-confidence</span>
            </>
          ) : null}
        </span>
      }
    >
      <p className="text-sm text-ink-300">
        Claude tool-use extracted {dishCount} dishes and {ingredientCount}{" "}
        distinct ingredients from{" "}
        <span className="text-ink-100">{restaurantName}</span>&rsquo;s menu
        snapshot. Each dish carries a parse-confidence score; each ingredient
        carries an estimation-confidence score. Low values flag rows for human
        review.
      </p>

      <Panel className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {dishes.map((d) => (
          <DishCard key={d.id} dish={d} />
        ))}
      </Panel>
    </StageSection>
  );
}

function DishCard({ dish }: { dish: DishOut }) {
  return (
    <article className="rounded border border-ink-600/60 bg-ink-700/40 p-4">
      <header className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-medium text-ink-50">{dish.name}</h3>
          {dish.description ? (
            <p className="mt-1 text-xs text-ink-300">{dish.description}</p>
          ) : null}
        </div>
        <div className="flex flex-col items-end gap-1.5">
          {dish.price ? (
            <span className="num text-sm text-ink-100">${dish.price}</span>
          ) : null}
          <ConfidenceBadge value={dish.parse_confidence} />
        </div>
      </header>
      {dish.ingredients.length > 0 ? (
        <ul className="mt-3 flex flex-wrap gap-1.5">
          {dish.ingredients.map((ing) => (
            <li key={ing.id}>
              <span
                className="pill border border-ink-500/60 bg-ink-800/60 text-ink-100"
                title={
                  ing.quantity
                    ? `~${ing.quantity} ${ing.unit ?? ""} per serving`
                    : undefined
                }
              >
                <span>{ing.name}</span>
                <ConfidenceBadge value={ing.estimation_confidence} size="xs" />
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}
