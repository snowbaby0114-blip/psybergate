/** Wire types of the API (SPEC 6.1). Property names mirror the JSON exactly; there is no mapping layer. */

export interface MedicineOut {
  readonly code: string;
  readonly name: string;
  readonly form: string;
  readonly strength_value: number;
  readonly strength_unit: string;
  readonly is_active: boolean;
}

/** Relative to today's business date in SAST, decided by the server. */
export type RuleStatus = 'expired' | 'in_force' | 'scheduled';

export interface RuleOut {
  readonly id: number;
  /** Business date (YYYY-MM-DD), inclusive. */
  readonly effective_from: string;
  /** Business date (YYYY-MM-DD), exclusive: the first day the rule no longer applies. Null = open-ended. */
  readonly effective_to: string | null;
  readonly max_quantity_per_dispense: number;
  readonly max_quantity_per_30_days: number;
  readonly requires_authorisation: boolean;
  readonly status: RuleStatus;
  readonly created_at: string;
}

export interface MedicineDetail extends MedicineOut {
  /** Today's business date in SAST, as seen by the server. */
  readonly as_of: string;
  readonly current_rule: RuleOut | null;
}

export interface RuleList {
  readonly items: readonly RuleOut[];
}

export interface NewRule {
  effective_from: string;
  max_quantity_per_dispense: number;
  max_quantity_per_30_days: number;
  requires_authorisation: boolean;
}

export interface RuleSupersedeResult {
  readonly rule: RuleOut;
  /** The previously governing rule, after truncation; null when nothing was in force at the start date. */
  readonly superseded: RuleOut | null;
}

export interface DispenseOut {
  readonly id: number;
  readonly medicine_code: string;
  readonly medicine_name: string;
  readonly patient_ref: string;
  readonly quantity: number;
  readonly dispensed_at: string;
  readonly authorisation_ref: string | null;
  readonly idempotency_key: string;
  readonly rule_id: number;
  readonly created_at: string;
}

export interface DispenseRequest {
  idempotency_key: string;
  medicine_code: string;
  patient_ref: string;
  quantity: number;
  /** UTC instant; omitted means "now" on the server. */
  dispensed_at?: string;
  authorisation_ref?: string;
}

export interface Page<T> {
  readonly items: readonly T[];
  readonly next_cursor: string | null;
}

export interface Violation {
  /** Stable machine-readable rule or cause code. */
  readonly code: string;
  /** Request field the violation is about, or null for non-field issues. */
  readonly field: string | null;
  readonly message: string;
  /** Machine-readable details (limit, requested, ...). Never required to understand the message. */
  readonly context: Readonly<Record<string, unknown>>;
}

/** RFC 9457 problem details plus the uniform `violations` list (SPEC 6.3). */
export interface Problem {
  readonly type: string;
  readonly title: string;
  readonly status: number;
  readonly detail: string;
  readonly code: string;
  readonly request_id: string;
  readonly violations: readonly Violation[];
  /** Only on `dispense_rejected`. */
  readonly attempt_id?: number;
}
