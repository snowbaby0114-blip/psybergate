import { DispenseFormValues, toDispensePayload } from './dispense-payload';

const VALUES: DispenseFormValues = {
  medicine_code: 'MED-0001',
  patient_ref: 'PT-7K2M9Q',
  quantity: 20,
  dispensed_at: '',
  authorisation_ref: '',
};

describe('toDispensePayload', () => {
  it('sends only what was entered: optional fields that are blank are left out', () => {
    expect(toDispensePayload(VALUES)).toEqual({
      medicine_code: 'MED-0001',
      patient_ref: 'PT-7K2M9Q',
      quantity: 20,
    });
    expect(Object.keys(toDispensePayload(VALUES))).not.toContain('dispensed_at');
    expect(Object.keys(toDispensePayload(VALUES))).not.toContain('authorisation_ref');
  });

  it('trims text fields and treats a blank authorisation reference as absent', () => {
    const payload = toDispensePayload({
      ...VALUES,
      medicine_code: '  MED-0001 ',
      patient_ref: ' PT-7K2M9Q  ',
      authorisation_ref: '   ',
    });

    expect(payload).toEqual({ medicine_code: 'MED-0001', patient_ref: 'PT-7K2M9Q', quantity: 20 });
  });

  it('keeps an authorisation reference, trimmed', () => {
    expect(
      toDispensePayload({ ...VALUES, authorisation_ref: ' AUTH-123 ' }).authorisation_ref,
    ).toBe('AUTH-123');
  });

  it('reads dispensed_at as SAST and sends it as a UTC instant, across the SAST midnight', () => {
    expect(toDispensePayload({ ...VALUES, dispensed_at: '2026-03-01T00:30' }).dispensed_at).toBe(
      '2026-02-28T22:30:00Z',
    );
    expect(toDispensePayload({ ...VALUES, dispensed_at: '2026-03-15T12:00' }).dispensed_at).toBe(
      '2026-03-15T10:00:00Z',
    );
  });
});
