import { HttpErrorResponse } from '@angular/common/http';

import { problem } from '../../testing/fixtures';
import { ApiError, toApiError } from '../api/api-error';
import { DispensePayload } from './dispense-payload';
import { IdempotencyKeyManager } from './idempotency-key';

const PAYLOAD: DispensePayload = {
  medicine_code: 'MED-0001',
  patient_ref: 'PT-1',
  quantity: 20,
};

function numbered(): () => string {
  let count = 0;
  return () => `key-${(count += 1)}`;
}

function failure(status: number, body: unknown = null): ApiError {
  return toApiError(new HttpErrorResponse({ status, error: body }));
}

const NO_RESPONSE = failure(0);
const TIMEOUT = new ApiError({
  status: 0,
  code: 'timeout',
  title: 'Timed out',
  detail: 'Try again.',
  isNetworkError: true,
});

describe('IdempotencyKeyManager', () => {
  let keys: IdempotencyKeyManager;

  beforeEach(() => {
    keys = new IdempotencyKeyManager(numbered());
  });

  it('generates a UUID by default', () => {
    expect(new IdempotencyKeyManager().keyFor(PAYLOAD)).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
  });

  it('hands out one key for one intent, however often it is asked', () => {
    expect(keys.keyFor(PAYLOAD)).toBe('key-1');
    expect(keys.keyFor({ ...PAYLOAD })).toBe('key-1');
  });

  describe('after a failure that left the outcome unknown', () => {
    it.each([
      ['no response at all', NO_RESPONSE],
      ['a timeout', TIMEOUT],
      ['a 502 from a proxy, with an HTML body', failure(502, '<html>Bad Gateway</html>')],
      ['a 500 without a body', failure(500)],
      [
        'a 503 resource_busy problem',
        failure(503, problem({ status: 503, code: 'resource_busy' })),
      ],
      [
        'a 500 internal_error problem',
        failure(500, problem({ status: 500, code: 'internal_error' })),
      ],
    ])('reuses the key for the same payload: %s', (_description, error) => {
      const first = keys.keyFor(PAYLOAD);

      keys.failed(error);

      expect(keys.keyFor(PAYLOAD)).toBe(first);
    });

    it('keeps reusing it across several failed retries until an answer arrives', () => {
      const first = keys.keyFor(PAYLOAD);
      keys.failed(NO_RESPONSE);
      keys.keyFor(PAYLOAD);
      keys.failed(TIMEOUT);

      expect(keys.keyFor(PAYLOAD)).toBe(first);

      keys.succeeded();
      expect(keys.keyFor(PAYLOAD)).not.toBe(first);
    });
  });

  describe('after the server has answered', () => {
    it('starts a new intent with a new key after a success, even for an identical payload', () => {
      const first = keys.keyFor(PAYLOAD);

      keys.succeeded();

      expect(keys.keyFor(PAYLOAD)).not.toBe(first);
    });

    it('starts a new key after a rejection, so a retry is not answered by the replayed rejection', () => {
      const first = keys.keyFor(PAYLOAD);

      keys.failed(failure(422, problem()));

      expect(keys.keyFor(PAYLOAD)).not.toBe(first);
    });

    it.each([
      ['400 invalid_cursor', failure(400, problem({ status: 400, code: 'invalid_cursor' }))],
      [
        '404 medicine_not_found',
        failure(404, problem({ status: 404, code: 'medicine_not_found' })),
      ],
      ['409 rule_conflict', failure(409, problem({ status: 409, code: 'rule_conflict' }))],
      ['422 validation_error', failure(422, problem({ code: 'validation_error' }))],
      ['422 idempotency_key_reuse', failure(422, problem({ code: 'idempotency_key_reuse' }))],
      ['a 4xx that is not a problem document', failure(403, { detail: 'Forbidden' })],
    ])('treats %s as an answer: the key is spent', (_description, error) => {
      const first = keys.keyFor(PAYLOAD);

      keys.failed(error);

      expect(keys.keyFor(PAYLOAD)).not.toBe(first);
    });
  });

  describe('when the payload changes', () => {
    it.each<[string, Partial<DispensePayload>]>([
      ['medicine_code', { medicine_code: 'MED-0002' }],
      ['patient_ref', { patient_ref: 'PT-2' }],
      ['quantity', { quantity: 21 }],
      ['dispensed_at', { dispensed_at: '2026-03-01T00:00:00Z' }],
      ['authorisation_ref', { authorisation_ref: 'AUTH-1' }],
    ])('uses a new key when %s changes, even before any answer', (_field, change) => {
      const first = keys.keyFor(PAYLOAD);
      keys.failed(NO_RESPONSE);

      const second = keys.keyFor({ ...PAYLOAD, ...change });

      expect(second).not.toBe(first);
    });

    it('treats an explicit dispensed_at and an omitted one as different payloads', () => {
      const withTime = { ...PAYLOAD, dispensed_at: '2026-03-01T00:00:00Z' };
      const first = keys.keyFor(withTime);

      expect(keys.keyFor(PAYLOAD)).not.toBe(first);
    });

    it('does not depend on the order in which the payload object was built', () => {
      const first = keys.keyFor({ medicine_code: 'MED-0001', patient_ref: 'PT-1', quantity: 20 });

      const second = keys.keyFor({ quantity: 20, patient_ref: 'PT-1', medicine_code: 'MED-0001' });

      expect(second).toBe(first);
    });

    it('does not resurrect an abandoned key when an edit is reverted', () => {
      const first = keys.keyFor(PAYLOAD);
      keys.failed(NO_RESPONSE);
      keys.keyFor({ ...PAYLOAD, quantity: 21 });

      expect(keys.keyFor(PAYLOAD)).not.toBe(first);
    });
  });
});
