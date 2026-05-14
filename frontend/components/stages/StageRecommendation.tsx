import { EmptyState, Panel, StageSection } from "@/components/Card";
import type {
  DistributorRecommendationOut,
  RecommendationResponse,
} from "@/lib/types";

interface Props {
  recommendation: RecommendationResponse | null;
}

export function StageRecommendation({ recommendation }: Props) {
  // Empty state: no quotes yet → recommender returns ready=false OR pick=null.
  if (
    !recommendation ||
    !recommendation.ready ||
    !recommendation.pick ||
    recommendation.ranked.length === 0
  ) {
    const reason = recommendation?.not_ready_reason ?? null;
    return (
      <StageSection
        id="stage-recommendation"
        eyebrow="Stage 6"
        title="Recommendation"
      >
        <EmptyState
          icon="✦"
          title="Recommendation pending — collect quotes first"
          hint={
            <>
              {reason ? (
                <span className="text-ink-200">{reason}.</span>
              ) : (
                <>Reply to the RFP emails, then poll the inbox.</>
              )}{" "}
              You can force a partial recommendation now via the{" "}
              <span className="rounded bg-ink-700 px-1.5 py-0.5 text-ink-100">
                Finalize
              </span>{" "}
              button below.
            </>
          }
        />
      </StageSection>
    );
  }

  const pick = recommendation.pick;
  const runnersUp = recommendation.ranked.filter(
    (r) => r.distributor_id !== pick.distributor_id,
  );

  return (
    <StageSection
      id="stage-recommendation"
      eyebrow="Stage 6"
      title="Recommendation"
      meta={
        <span className="num">
          {recommendation.ranked.length} distributors compared
        </span>
      }
    >
      <Panel className="border-accent/30 bg-accent/5">
        <header className="flex flex-wrap items-baseline justify-between gap-3">
          <div>
            <div className="stage-eyebrow text-accent">Recommended pick</div>
            <h3 className="text-display font-semibold tracking-tight text-ink-50">
              {pick.distributor_name}
            </h3>
          </div>
          <div className="flex items-baseline gap-3 text-right">
            <div>
              <div className="num text-display font-semibold text-accent">
                {pick.score.toFixed(2)}
              </div>
              <div className="text-xs uppercase tracking-wider text-ink-300">
                weighted score
              </div>
            </div>
            <div>
              <div className="num text-h1 text-ink-100">
                {pick.coverage_pct}%
              </div>
              <div className="text-xs uppercase tracking-wider text-ink-300">
                basket coverage
              </div>
            </div>
          </div>
        </header>

        {pick.incomplete_comparison ? (
          <div className="mt-4 rounded border border-warn/40 bg-warn/5 px-4 py-3 text-sm text-warn">
            <strong className="font-medium">incomplete_comparison:</strong>{" "}
            this pick won its basket but only quoted{" "}
            <span className="num">{pick.quoted_ingredient_count}</span> of{" "}
            <span className="num">{pick.requested_ingredient_count}</span>{" "}
            requested items. Other distributors covered different baskets — a
            single &ldquo;winner&rdquo; here is per-basket, not a global apples-to-apples
            ranking.
          </div>
        ) : null}

        <div className="mt-5 grid grid-cols-1 gap-3 md:grid-cols-2">
          {pick.components.map((c) => (
            <ComponentBar key={c.name} c={c} />
          ))}
        </div>

        <p className="mt-5 border-t border-ink-600/50 pt-4 text-sm italic text-ink-200">
          {pick.rationale}
        </p>
      </Panel>

      {runnersUp.length > 0 ? (
        <Panel>
          <div className="stage-eyebrow mb-3">Ranked alternatives</div>
          <ul className="divide-y divide-ink-700/50">
            {runnersUp.map((d) => (
              <RunnerUp key={d.distributor_id} d={d} />
            ))}
          </ul>
        </Panel>
      ) : null}
    </StageSection>
  );
}

function ComponentBar({
  c,
}: {
  c: RecommendationResponse["pick"] extends infer T
    ? T extends DistributorRecommendationOut
      ? T["components"][number]
      : never
    : never;
}) {
  const weights: Record<string, number> = {
    cost: 0.5,
    delivery: 0.2,
    moq_fit: 0.15,
    completeness: 0.15,
  };
  const weight = weights[c.name] ?? 0;
  const filled = Math.max(0, Math.min(1, c.normalized));
  return (
    <div className="rounded border border-ink-600/60 bg-ink-700/30 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-sm text-ink-100 capitalize">
          {c.name.replace(/_/g, " ")}
        </div>
        <div className="flex items-baseline gap-2">
          <span className="num text-sm text-ink-50">
            {(filled * 100).toFixed(0)}
          </span>
          <span className="text-xs text-ink-400">
            × {(weight * 100).toFixed(0)}%
          </span>
        </div>
      </div>
      <div className="mt-2 h-1.5 rounded-full bg-ink-700/80">
        <div
          className={`h-full rounded-full ${
            c.null_imputed ? "bg-warn/60" : "bg-accent"
          }`}
          style={{ width: `${filled * 100}%` }}
        />
      </div>
      {c.note ? (
        <p className="mt-2 text-[0.7rem] leading-snug text-ink-300">
          {c.note}
        </p>
      ) : null}
    </div>
  );
}

function RunnerUp({ d }: { d: DistributorRecommendationOut }) {
  return (
    <li className="flex flex-col gap-1 py-3 first:pt-0 last:pb-0">
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-sm text-ink-100">{d.distributor_name}</div>
        <div className="flex items-baseline gap-3 text-xs">
          <span className="num text-ink-50">{d.score.toFixed(2)}</span>
          <span className="num text-ink-300">{d.coverage_pct}%</span>
          {d.incomplete_comparison ? (
            <span className="pill border border-warn/30 text-warn">
              incomplete
            </span>
          ) : null}
        </div>
      </div>
      <p className="text-xs leading-snug text-ink-300">{d.rationale}</p>
    </li>
  );
}
