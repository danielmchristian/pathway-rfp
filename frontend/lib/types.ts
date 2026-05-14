// TypeScript shapes mirror the backend's Pydantic response models.
// Keep these aligned with app/schemas/*.py — when the backend schema
// changes, this file changes too. Loose types only where the backend
// uses string-encoded Decimal (we render them as strings on the UI).

export type Trend = "up" | "down" | "flat" | "unknown";

export interface IngredientSummaryRow {
  ingredient_id: number;
  ingredient_name: string;
  normalized_name: string;
  fdc_id: number | null;
  fdc_category: string | null;
  latest_price_per_unit: string | null;
  unit_normalized: string | null;
  delta_pct_30d: string | null;
  direction: Trend;
  observations_count: number;
  pricing_unavailable: boolean;
  source: string | null;
}

export interface IngredientOut {
  id: number;
  name: string;
  normalized_name: string;
  quantity: string | null;
  unit: string | null;
  estimation_confidence: number | null;
}

export interface DishOut {
  id: number;
  name: string;
  description: string | null;
  price: string | null;
  parse_confidence: number | null;
  ingredients: IngredientOut[];
}

export interface ScoredDistributorOut {
  distributor_id: number;
  name: string;
  specialties: string[];
  source: string | null;
  matched_ingredient_count: number;
  total_ingredients: number;
  match_pct: number;
  sample_matched_ingredients: string[];
  distance_km: number | null;
}

export interface RfpItemOut {
  id: number;
  ingredient_id: number;
  ingredient_name: string | null;
  normalized_name: string | null;
  quantity: string | null;
  unit: string | null;
}

export interface RfpEmailOut {
  id: number;
  distributor_id: number | null;
  distributor_name: string | null;
  direction: string;
  subject: string | null;
  body: string | null;
  message_id: string | null;
  in_reply_to: string | null;
  status: string;
  sent_at: string | null;
  received_at: string | null;
  recipient_actual: string | null;
  recipient_nominal: string | null;
  resend_id: string | null;
}

export interface RfpRequestOut {
  id: number;
  restaurant_id: number;
  status: string;
  deadline: string | null;
  created_at: string | null;
  items: RfpItemOut[];
  emails: RfpEmailOut[];
}

export interface RfpRequestSummaryOut {
  id: number;
  restaurant_id: number;
  status: string;
  deadline: string | null;
  created_at: string | null;
  items_count: number;
  emails_count: number;
  emails_sent: number;
  emails_failed: number;
}

export interface QuoteOut {
  id: number;
  ingredient_id: number;
  ingredient_name: string | null;
  unit_price: string | null;
  unit: string | null;
  min_order_qty: string | null;
  delivery_days: number | null;
  terms: string | null;
  parse_confidence: number | null;
  missing_fields: string[];
  source_email_id: number | null;
}

export interface DistributorQuotesOut {
  distributor_id: number;
  distributor_name: string;
  quotes: QuoteOut[];
}

export interface QuotesGroupedResponse {
  rfp_request_id: number;
  by_distributor: DistributorQuotesOut[];
}

export interface ComparisonCell {
  distributor_id: number | null;
  unit_price: string | null;
  unit: string | null;
  min_order_qty: string | null;
  delivery_days: number | null;
  missing_fields: string[];
}

export interface ComparisonRow {
  ingredient_id: number;
  ingredient_name: string;
  requested_quantity: string | null;
  requested_unit: string | null;
  cells: Record<string, ComparisonCell>;
}

export interface ComparisonResponse {
  rfp_request_id: number;
  distributors: { id: number; name: string }[];
  rows: ComparisonRow[];
}

export interface ComponentScoreOut {
  name: string;
  raw_value: number | null;
  normalized: number;
  null_imputed: boolean;
  note: string | null;
}

export interface DistributorRecommendationOut {
  distributor_id: number;
  distributor_name: string;
  score: number;
  coverage_pct: string;
  quoted_ingredient_count: number;
  requested_ingredient_count: number;
  incomplete_comparison: boolean;
  components: ComponentScoreOut[];
  rationale: string;
  excluded_for_cost: string[];
}

export interface RecommendationResponse {
  rfp_request_id: number;
  ready: boolean;
  deadline_passed: boolean;
  all_replied: boolean;
  pick: DistributorRecommendationOut | null;
  ranked: DistributorRecommendationOut[];
  not_ready_reason: string | null;
}

export interface UsageStageRollup {
  stage: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string;
}

export interface UsageResponse {
  total_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: string;
  by_stage: UsageStageRollup[];
}

export interface RestaurantOut {
  id: number;
  name: string;
  address: string | null;
  city: string | null;
  state: string | null;
  zip: string | null;
}

// Trigger response types
export interface PollInboxResponse {
  rfp_request_id: number;
  inbound_count: number;
  attributed_count: number;
  unattributed_count: number;
  duplicate_uids_skipped: number;
  persisted_email_ids: number[];
  poll_error: string | null;
  parse_results: unknown[];
  parse_failed_email_ids: number[];
  followups: unknown[];
  recommendation_ready: boolean;
  recommendation_not_ready_reason: string | null;
  pick_distributor_id: number | null;
  pick_score: number | null;
}
