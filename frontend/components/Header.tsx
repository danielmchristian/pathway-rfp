import type { RestaurantOut } from "@/lib/types";

interface Props {
  restaurant: RestaurantOut;
  rfpId: number | null;
  rfpStatus: string | null;
  emailsSent: number;
  emailsExpected: number;
}

export function Header({
  restaurant,
  rfpId,
  rfpStatus,
  emailsSent,
  emailsExpected,
}: Props) {
  const location = [restaurant.city, restaurant.state].filter(Boolean).join(", ");
  return (
    <header className="flex flex-wrap items-end justify-between gap-3 border-b border-ink-600/60 pb-4">
      <div>
        <div className="stage-eyebrow text-accent">Pathway RFP</div>
        <h1 className="text-display font-semibold tracking-tight text-ink-50">
          {restaurant.name}
        </h1>
        {location ? (
          <p className="mt-1 text-sm text-ink-300">{location}</p>
        ) : null}
      </div>
      <div className="text-right text-sm">
        {rfpId !== null ? (
          <>
            <div className="num text-ink-100">RFP #{rfpId}</div>
            <div className="text-xs text-ink-300">
              {emailsSent}/{emailsExpected} emails sent ·{" "}
              <span className="text-ink-200">{rfpStatus ?? "—"}</span>
            </div>
          </>
        ) : (
          <div className="text-ink-300">no RFP yet</div>
        )}
      </div>
    </header>
  );
}
