import { CostDashboard } from "@/components/CostDashboard";
import { EmptyState, StageSection } from "@/components/Card";
import { Header } from "@/components/Header";
import { PipelineTriggers } from "@/components/PipelineTriggers";
import { SseEventStream } from "@/components/SseEventStream";
import { StageComparison } from "@/components/stages/StageComparison";
import { StageDistributors } from "@/components/stages/StageDistributors";
import { StageMenu } from "@/components/stages/StageMenu";
import { StagePricing } from "@/components/stages/StagePricing";
import { StageRecommendation } from "@/components/stages/StageRecommendation";
import { StageRfpEmails } from "@/components/stages/StageRfpEmails";
import {
  DEMO_RESTAURANT_ID,
  getComparison,
  getDishes,
  getDistributors,
  getIngredientSummary,
  getRecommendation,
  getRestaurant,
  getRfp,
  getUsage,
  listRfps,
  sseUrl,
} from "@/lib/api";
import type {
  ComparisonResponse,
  RecommendationResponse,
  RfpRequestOut,
  RfpRequestSummaryOut,
} from "@/lib/types";

export const dynamic = "force-dynamic";

async function safeFetch<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (err) {
    if (process.env.NODE_ENV !== "production") {
      console.error("page fetch failed:", err);
    }
    return null;
  }
}

export default async function Page() {
  const restaurant = await safeFetch(getRestaurant(DEMO_RESTAURANT_ID));

  if (!restaurant) {
    return <RootError />;
  }

  // Parallel fetches — cheap to do server-side and keeps TTFB small.
  const [dishes, ingredientSummary, distributors, rfps, usage] =
    await Promise.all([
      safeFetch(getDishes(DEMO_RESTAURANT_ID)).then((v) => v ?? []),
      safeFetch(getIngredientSummary(DEMO_RESTAURANT_ID)).then((v) => v ?? []),
      safeFetch(getDistributors(DEMO_RESTAURANT_ID)).then((v) => v ?? []),
      safeFetch(listRfps(DEMO_RESTAURANT_ID)).then((v) => v ?? []),
      safeFetch(getUsage()),
    ]);

  // Use the most recent RFP for the per-RFP stages.
  const latestRfp: RfpRequestSummaryOut | null =
    rfps.length > 0 ? rfps[0] : null;

  let rfpDetail: RfpRequestOut | null = null;
  let comparison: ComparisonResponse | null = null;
  let recommendation: RecommendationResponse | null = null;

  if (latestRfp) {
    [rfpDetail, comparison, recommendation] = await Promise.all([
      safeFetch(getRfp(latestRfp.id)),
      safeFetch(getComparison(latestRfp.id)),
      safeFetch(getRecommendation(latestRfp.id)),
    ]);
  }

  // unassigned_ingredients lives on rfp_request_items context but isn't on
  // the audit endpoint — surfaced via the rfp_pipeline result that
  // creates the RFP. For the UI we read it from the rfp_emails raw_payload
  // history if present, otherwise empty. The send_rfps response includes
  // it; in steady state it's not stored as a column, so we compute a
  // proxy: ingredients in dish_ingredients that are NOT in rfp_request_items.
  const requestedIngredientIds = new Set(
    (rfpDetail?.items ?? []).map((i) => i.ingredient_id),
  );
  const allIngredientNames = ingredientSummary.map((r) => ({
    id: r.ingredient_id,
    name: r.ingredient_name,
  }));
  const unassignedIngredients = allIngredientNames
    .filter((i) => !requestedIngredientIds.has(i.id))
    .map((i) => i.name)
    .sort();

  return (
    <div className="min-h-screen pb-32">
      {/* Sticky cost dashboard (top-right). Shrinks on small screens. */}
      <div className="mx-auto max-w-7xl px-6 py-6 sm:py-8">
        <div className="grid grid-cols-1 gap-6 md:grid-cols-[1fr_18rem]">
          <Header
            restaurant={restaurant}
            rfpId={latestRfp?.id ?? null}
            rfpStatus={latestRfp?.status ?? null}
            emailsSent={latestRfp?.emails_sent ?? 0}
            emailsExpected={
              (latestRfp?.emails_sent ?? 0) + (latestRfp?.emails_failed ?? 0)
            }
          />
          {usage ? (
            <div className="md:row-span-2">
              <CostDashboard usage={usage} />
            </div>
          ) : null}
        </div>

        <main className="mt-10 space-y-12">
          {dishes.length > 0 ? (
            <StageMenu restaurantName={restaurant.name} dishes={dishes} />
          ) : (
            <StageSection eyebrow="Stage 1" title="Menu → Recipes">
              <EmptyState
                title="No dishes parsed"
                hint="Run `make demo` to populate the pipeline."
              />
            </StageSection>
          )}

          {ingredientSummary.length > 0 ? (
            <StagePricing rows={ingredientSummary} />
          ) : (
            <StageSection eyebrow="Stage 2" title="Ingredient Pricing">
              <EmptyState
                title="No ingredients enriched"
                hint="Stage 1 needs to run first."
              />
            </StageSection>
          )}

          {distributors.length > 0 ? (
            <StageDistributors distributors={distributors} />
          ) : (
            <StageSection eyebrow="Stage 3" title="Distributors">
              <EmptyState
                title="No distributors discovered"
                hint="Run `make demo` to load the seed roster."
              />
            </StageSection>
          )}

          {rfpDetail ? (
            <StageRfpEmails
              rfp={rfpDetail}
              unassignedIngredients={unassignedIngredients}
            />
          ) : (
            <StageSection eyebrow="Stage 4" title="RFP Emails">
              <EmptyState
                title="No RFPs sent"
                hint="Run `make demo` to send the first batch."
              />
            </StageSection>
          )}

          <StageComparison
            comparison={comparison}
            ingredientSummary={ingredientSummary}
          />

          <StageRecommendation recommendation={recommendation} />
        </main>
      </div>

      {/* Sticky footer triggers + SSE live tail. */}
      <footer className="fixed bottom-0 left-0 right-0 z-40 border-t border-ink-600/60 bg-ink-900/95 backdrop-blur-sm">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-6 py-3">
          {latestRfp ? (
            <PipelineTriggers rfpId={latestRfp.id} />
          ) : (
            <span className="text-xs text-ink-400">
              run `make demo` to enable triggers
            </span>
          )}
          <SseEventStream url={sseUrl(DEMO_RESTAURANT_ID)} />
        </div>
      </footer>
    </div>
  );
}

function RootError() {
  return (
    <div className="mx-auto max-w-2xl px-6 py-24">
      <div className="card card-pad">
        <h1 className="text-h1 font-semibold text-ink-50">
          Backend unreachable
        </h1>
        <p className="mt-2 text-sm text-ink-300">
          The frontend can&rsquo;t reach{" "}
          <span className="num">
            {process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"}
          </span>
          . Start it with{" "}
          <span className="num bg-ink-700 px-1.5 py-0.5 rounded">
            make dev
          </span>{" "}
          (or{" "}
          <span className="num bg-ink-700 px-1.5 py-0.5 rounded">
            docker compose up
          </span>
          ).
        </p>
        <p className="mt-3 text-sm text-ink-300">
          Then{" "}
          <span className="num bg-ink-700 px-1.5 py-0.5 rounded">make demo</span>{" "}
          to populate the pipeline.
        </p>
      </div>
    </div>
  );
}
