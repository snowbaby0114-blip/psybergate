import { ApiError } from '../api/api-error';
import { DispensePayload } from './dispense-payload';

/**
 * One idempotency key per intent to dispense.
 *
 * A key identifies an attempt, and the server remembers the outcome of every key (accepted or rejected).
 * So the rules are:
 * - After a failure that gave no answer (no response, a timeout, any 5xx) the outcome is unknown: the
 *   same request is sent again with the same key, and the server either replays what happened the first
 *   time or processes it now. Retrying can therefore never record a dispense twice.
 * - After any answer that settles the attempt (a 2xx or a 4xx such as 422) the key is spent: the next
 *   submit is a new intent with a new key. Reusing it would only replay the old outcome, hiding a
 *   deliberate second try behind a replayed rejection.
 * - A different payload is a different intent, so it gets a new key. (Reusing a key with another payload
 *   is an error on the server: 422 idempotency_key_reuse.)
 */
export class IdempotencyKeyManager {
  private key: string | null = null;
  private fingerprint: string | null = null;

  constructor(private readonly generate: () => string = () => crypto.randomUUID()) {}

  /** The key to send with this payload. */
  keyFor(payload: DispensePayload): string {
    const fingerprint = fingerprintOf(payload);
    if (this.key === null || this.fingerprint !== fingerprint) {
      this.key = this.generate();
      this.fingerprint = fingerprint;
    }
    return this.key;
  }

  /** The attempt was answered with a 2xx: the key is spent. */
  succeeded(): void {
    this.spend();
  }

  /** The attempt failed. The key is only kept if the failure left the outcome unknown. */
  failed(error: ApiError): void {
    if (!error.retryable) {
      this.spend();
    }
  }

  private spend(): void {
    this.key = null;
    this.fingerprint = null;
  }
}

/** Explicit field order, so the fingerprint never depends on how the payload object was built. */
function fingerprintOf(payload: DispensePayload): string {
  return JSON.stringify([
    payload.medicine_code,
    payload.patient_ref,
    payload.quantity,
    payload.dispensed_at ?? null,
    payload.authorisation_ref ?? null,
  ]);
}
