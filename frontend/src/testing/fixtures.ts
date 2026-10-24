import {
  DispenseOut,
  MedicineDetail,
  MedicineOut,
  Problem,
  RuleOut,
  Violation,
} from '../app/api/models';

export const MEDICINE: MedicineOut = {
  code: 'MED-0001',
  name: 'Amoxil',
  form: 'capsule',
  strength_value: 500,
  strength_unit: 'mg',
  is_active: true,
};

export function rule(overrides: Partial<RuleOut> = {}): RuleOut {
  return {
    id: 12,
    effective_from: '2026-03-01',
    effective_to: null,
    max_quantity_per_dispense: 60,
    max_quantity_per_30_days: 120,
    requires_authorisation: false,
    status: 'in_force',
    created_at: '2026-02-20T09:15:00Z',
    ...overrides,
  };
}

export function medicineDetail(overrides: Partial<MedicineDetail> = {}): MedicineDetail {
  return { ...MEDICINE, as_of: '2026-03-15', current_rule: rule(), ...overrides };
}

export function dispense(overrides: Partial<DispenseOut> = {}): DispenseOut {
  return {
    id: 981,
    medicine_code: 'MED-0001',
    medicine_name: 'Amoxil',
    patient_ref: 'PT-7K2M9Q',
    quantity: 20,
    dispensed_at: '2026-03-15T10:00:00Z',
    authorisation_ref: null,
    idempotency_key: '7f6c0f0e-0c1b-4c3e-9a55-3f1f5f0f2a11',
    rule_id: 12,
    created_at: '2026-03-15T10:00:04Z',
    ...overrides,
  };
}

export function violation(overrides: Partial<Violation> = {}): Violation {
  return {
    code: 'max_quantity_per_dispense_exceeded',
    field: 'quantity',
    message: 'Quantity 150 exceeds the maximum of 100 units per dispense.',
    context: { limit: 100, requested: 150, rule_id: 7 },
    ...overrides,
  };
}

export function problem(overrides: Partial<Problem> = {}): Problem {
  return {
    type: 'urn:emguidance:problem:dispense_rejected',
    title: 'Dispense rejected',
    status: 422,
    detail: 'The dispense violates 1 formulary rule.',
    code: 'dispense_rejected',
    request_id: 'req-9f0c1c5e',
    violations: [violation()],
    ...overrides,
  };
}
