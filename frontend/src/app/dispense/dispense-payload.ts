import { DispenseRequest } from '../api/models';
import { sastLocalToUtcIso } from '../shared/time';

/** Everything the server is asked to record, except the idempotency key that identifies the attempt. */
export type DispensePayload = Omit<DispenseRequest, 'idempotency_key'>;

export interface DispenseFormValues {
  readonly medicine_code: string;
  readonly patient_ref: string;
  readonly quantity: number;
  /** A datetime-local value, which the form reads as SAST wall-clock time. */
  readonly dispensed_at: string;
  readonly authorisation_ref: string;
}

/**
 * What the form means as a request. Text is trimmed; the optional fields are left out when blank (an
 * omitted `dispensed_at` means "now" to the server, which is a different request from an explicit one).
 */
export function toDispensePayload(values: DispenseFormValues): DispensePayload {
  const dispensedAt = sastLocalToUtcIso(values.dispensed_at);
  const authorisationRef = values.authorisation_ref.trim();
  return {
    medicine_code: values.medicine_code.trim(),
    patient_ref: values.patient_ref.trim(),
    quantity: values.quantity,
    ...(dispensedAt !== null && { dispensed_at: dispensedAt }),
    ...(authorisationRef !== '' && { authorisation_ref: authorisationRef }),
  };
}
