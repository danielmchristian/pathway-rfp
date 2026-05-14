import { Panel, StageSection } from "@/components/Card";
import type { ScoredDistributorOut } from "@/lib/types";

interface Props {
  distributors: ScoredDistributorOut[];
}

export function StageDistributors({ distributors }: Props) {
  const sorted = [...distributors].sort(
    (a, b) => b.matched_ingredient_count - a.matched_ingredient_count,
  );
  const matched = sorted.filter((d) => d.matched_ingredient_count > 0).length;
  const controls = sorted.length - matched;

  return (
    <StageSection
      id="stage-distributors"
      eyebrow="Stage 3"
      title="Distributors"
      meta={
        <span className="num">
          {sorted.length} loaded · {matched} matched · {controls} controls
        </span>
      }
    >
      <p className="text-sm text-ink-300">
        Seed roster (10 curated NC-area wholesale distributors) merged with
        any Google Places candidates that survived the Claude noise filter.
        Match scores are computed from each ingredient&rsquo;s FDC category
        translated into a canonical specialty vocabulary.
      </p>

      <Panel className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
        {sorted.map((d) => (
          <DistributorCard key={d.distributor_id} d={d} />
        ))}
      </Panel>
    </StageSection>
  );
}

function DistributorCard({ d }: { d: ScoredDistributorOut }) {
  const hasMatch = d.matched_ingredient_count > 0;
  return (
    <article
      className={`rounded border bg-ink-700/40 p-4 ${
        hasMatch ? "border-ink-500/70" : "border-ink-600/40 opacity-70"
      }`}
    >
      <header className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-medium text-ink-50">{d.name}</h3>
        <SourceBadge source={d.source} />
      </header>

      <div className="mt-3 flex items-baseline gap-3">
        <span className="num text-h1 text-ink-50">
          {d.matched_ingredient_count}
        </span>
        <span className="text-xs text-ink-300">
          / {d.total_ingredients} ingredients
        </span>
        {d.distance_km !== null ? (
          <span className="ml-auto num text-xs text-ink-300">
            {d.distance_km.toFixed(1)} km
          </span>
        ) : null}
      </div>

      <ul className="mt-3 flex flex-wrap gap-1">
        {d.specialties.map((s) => (
          <li key={s} className="pill border border-ink-500/50 text-ink-200">
            {s.replace(/_/g, " ")}
          </li>
        ))}
      </ul>

      {d.sample_matched_ingredients.length > 0 ? (
        <p className="mt-3 text-xs text-ink-300">
          Sample:{" "}
          <span className="text-ink-100">
            {d.sample_matched_ingredients.slice(0, 3).join(", ")}
          </span>
          {d.sample_matched_ingredients.length > 3 ? "…" : null}
        </p>
      ) : null}
    </article>
  );
}

function SourceBadge({ source }: { source: string | null }) {
  if (!source) return null;
  const label = source.replace(/_/g, " ");
  const tone =
    source === "seed"
      ? "border-ink-500/40 text-ink-300"
      : source === "google_places_merged"
        ? "border-accent/40 text-accent"
        : "border-ink-500/40 text-ink-200";
  return <span className={`pill border ${tone}`}>{label}</span>;
}
