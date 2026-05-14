// Thin fetch wrappers over the backend REST API. Server Components call
// these during render (no caching — every render hits the live DB so the
// demo reflects the latest poll). Client Components import individually
// when they need to refetch after a trigger.

import type {
  ComparisonResponse,
  DishOut,
  IngredientSummaryRow,
  PollInboxResponse,
  QuotesGroupedResponse,
  RecommendationResponse,
  RestaurantOut,
  RfpRequestOut,
  RfpRequestSummaryOut,
  ScoredDistributorOut,
  UsageResponse,
} from "./types";

const BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `API ${init?.method ?? "GET"} ${path} → ${res.status}: ${text.slice(0, 200)}`,
    );
  }
  return (await res.json()) as T;
}

// The demo always uses restaurant_id=1 (Sweetgreen — Park Road).
export const DEMO_RESTAURANT_ID = 1;

export async function getRestaurant(id: number): Promise<RestaurantOut> {
  return request<RestaurantOut>(`/api/restaurants/${id}`);
}

export async function getDishes(id: number): Promise<DishOut[]> {
  return request<DishOut[]>(`/api/restaurants/${id}/dishes`);
}

export async function getIngredientSummary(
  id: number,
): Promise<IngredientSummaryRow[]> {
  return request<IngredientSummaryRow[]>(
    `/api/restaurants/${id}/ingredients/summary`,
  );
}

export async function getDistributors(
  id: number,
): Promise<ScoredDistributorOut[]> {
  return request<ScoredDistributorOut[]>(
    `/api/restaurants/${id}/distributors`,
  );
}

export async function listRfps(id: number): Promise<RfpRequestSummaryOut[]> {
  return request<RfpRequestSummaryOut[]>(`/api/restaurants/${id}/rfps`);
}

export async function getRfp(rfpId: number): Promise<RfpRequestOut> {
  return request<RfpRequestOut>(`/api/rfp/${rfpId}`);
}

export async function getQuotes(
  rfpId: number,
): Promise<QuotesGroupedResponse> {
  return request<QuotesGroupedResponse>(`/api/rfp/${rfpId}/quotes`);
}

export async function getComparison(
  rfpId: number,
): Promise<ComparisonResponse> {
  return request<ComparisonResponse>(`/api/rfp/${rfpId}/comparison`);
}

export async function getRecommendation(
  rfpId: number,
): Promise<RecommendationResponse> {
  return request<RecommendationResponse>(`/api/rfp/${rfpId}/recommendation`);
}

export async function getUsage(): Promise<UsageResponse> {
  return request<UsageResponse>(`/api/usage`);
}

// Triggers (called from Client Components)
export async function pollInbox(
  rfpId: number,
  forceRecommendation = false,
): Promise<PollInboxResponse> {
  return request<PollInboxResponse>(`/api/rfp/${rfpId}/poll_inbox`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force_recommendation: forceRecommendation }),
  });
}

export async function finalizeRfp(rfpId: number): Promise<RecommendationResponse> {
  return request<RecommendationResponse>(`/api/rfp/${rfpId}/finalize`, {
    method: "POST",
  });
}

export function sseUrl(restaurantId: number): string {
  return `${BASE}/api/restaurants/${restaurantId}/events`;
}
