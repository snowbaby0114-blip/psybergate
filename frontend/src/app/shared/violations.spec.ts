import { FormControl, FormGroup } from '@angular/forms';

import { violation } from '../../testing/fixtures';
import { notBlank } from './validators';
import {
  applyServerErrors,
  ariaInvalid,
  describeContext,
  discardServerErrors,
  fieldMessages,
  showsErrors,
} from './violations';

function dispenseForm() {
  return new FormGroup({
    medicine_code: new FormControl('MED-0001', { nonNullable: true }),
    quantity: new FormControl<number | null>(150),
    authorisation_ref: new FormControl('', { nonNullable: true }),
    patient_ref: new FormControl('PT-1', { nonNullable: true, validators: [notBlank('Patient')] }),
  });
}

describe('applyServerErrors', () => {
  it('gives a control all the messages of all the violations addressed to it, in order', () => {
    const form = dispenseForm();

    applyServerErrors(form, [
      violation({ field: 'quantity', message: 'Over the limit per dispense.' }),
      violation({ field: 'authorisation_ref', message: 'Authorisation is required.' }),
      violation({ field: 'quantity', message: 'Over the limit for 30 days.' }),
    ]);

    expect(form.controls.quantity.errors).toEqual({
      server: ['Over the limit per dispense.', 'Over the limit for 30 days.'],
    });
    expect(form.controls.authorisation_ref.errors).toEqual({
      server: ['Authorisation is required.'],
    });
    expect(form.controls.quantity.invalid).toBe(true);
    expect(form.controls.authorisation_ref.invalid).toBe(true);
  });

  it('leaves controls that no violation addresses alone', () => {
    const form = dispenseForm();

    applyServerErrors(form, [violation({ field: 'quantity' })]);

    expect(form.controls.medicine_code.valid).toBe(true);
    expect(form.controls.patient_ref.valid).toBe(true);
  });

  it('does not attach violations without a field, or for a field the form does not have', () => {
    const form = dispenseForm();

    applyServerErrors(form, [
      violation({ field: null, message: 'General problem.' }),
      violation({ field: 'idempotency_key', message: 'Bad key.' }),
      violation({ field: 'no_such_field', message: 'Unknown.' }),
    ]);

    expect(form.valid).toBe(true);
  });

  it('is not fooled by field names that exist on every object', () => {
    const form = dispenseForm();

    expect(() =>
      applyServerErrors(form, [
        violation({ field: 'constructor' }),
        violation({ field: 'toString' }),
        violation({ field: '__proto__' }),
      ]),
    ).not.toThrow();
    expect(form.valid).toBe(true);
  });

  it('clears a control once the user edits it, but not the other controls', () => {
    const form = dispenseForm();
    applyServerErrors(form, [
      violation({ field: 'quantity' }),
      violation({ field: 'authorisation_ref' }),
    ]);

    form.controls.quantity.setValue(50);

    expect(form.controls.quantity.errors).toBeNull();
    expect(form.controls.authorisation_ref.invalid).toBe(true);
  });
});

describe('discardServerErrors', () => {
  it('removes the server verdict without an edit, so the same input can be submitted again', () => {
    const form = dispenseForm();
    applyServerErrors(form, [violation({ field: 'quantity' })]);
    expect(form.invalid).toBe(true);

    discardServerErrors(form);

    expect(form.valid).toBe(true);
  });

  it('keeps what the client-side validators say', () => {
    const form = dispenseForm();
    form.controls.patient_ref.setValue('');
    applyServerErrors(form, [violation({ field: 'patient_ref', message: 'Server says no.' })]);

    discardServerErrors(form);

    expect(form.controls.patient_ref.errors).toEqual({ message: 'Patient is required.' });
  });
});

describe('what a control shows', () => {
  it('lists the server messages, or else the message of the client-side check', () => {
    const control = new FormControl('', [notBlank('Patient')]);
    expect(fieldMessages(control)).toEqual(['Patient is required.']);

    control.setErrors({ server: ['One.', 'Two.'] });
    expect(fieldMessages(control)).toEqual(['One.', 'Two.']);

    control.setErrors(null);
    expect(fieldMessages(control)).toEqual([]);
  });

  it('shows a server error at once but waits for the user before showing a client-side one', () => {
    const control = new FormControl('', [notBlank('Patient')]);
    expect(control.invalid).toBe(true);
    expect(showsErrors(control)).toBe(false);

    control.markAsTouched();
    expect(showsErrors(control)).toBe(true);

    const untouched = new FormControl('x');
    untouched.setErrors({ server: ['Rejected.'] });
    expect(showsErrors(untouched)).toBe(true);
  });

  it('marks a control aria-invalid exactly while its errors are showing', () => {
    const control = new FormControl('', [notBlank('Patient')]);
    expect(ariaInvalid(control)).toBeNull();

    control.markAsTouched();
    expect(ariaInvalid(control)).toBe('true');

    control.setValue('PT-1');
    expect(ariaInvalid(control)).toBeNull();
  });

  it('shows nothing for a valid control', () => {
    const control = new FormControl('ok');
    control.markAsTouched();

    expect(showsErrors(control)).toBe(false);
  });
});

describe('describeContext', () => {
  it('turns the machine-readable context into readable label and value pairs', () => {
    expect(
      describeContext({ limit: 100, requested: 150, already_dispensed: 40, rule_id: 7 }),
    ).toEqual([
      { label: 'limit', value: '100' },
      { label: 'requested', value: '150' },
      { label: 'already dispensed', value: '40' },
      { label: 'rule id', value: '7' },
    ]);
  });

  it('flattens nested values instead of showing raw JSON', () => {
    const [entry] = describeContext({
      affected: [
        { business_date: '2026-03-02', limit: 100, resulting_total: 120, dispense_ids: [5, 9] },
      ],
    });

    expect(entry?.value).toBe(
      'business date: 2026-03-02; limit: 100; resulting total: 120; dispense ids: 5, 9',
    );
    expect(entry?.value).not.toMatch(/[{}[\]"]/);
  });

  it('shows instants in SAST and leaves business dates alone', () => {
    const entries = describeContext({
      earliest_dispensed_at: '2026-02-28T22:30:00Z',
      window_start: '2026-02-01',
    });

    expect(entries).toEqual([
      { label: 'earliest dispensed at', value: '2026-03-01 00:30 SAST' },
      { label: 'window start', value: '2026-02-01' },
    ]);
  });

  it('has nothing to show for an empty context', () => {
    expect(describeContext({})).toEqual([]);
  });
});
