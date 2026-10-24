import { HttpErrorResponse, HttpHeaders } from '@angular/common/http';
import { TimeoutError } from 'rxjs';

import { problem, violation } from '../../testing/fixtures';
import { ApiError, toApiError } from './api-error';

function httpError(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): HttpErrorResponse {
  return new HttpErrorResponse({
    status,
    statusText: 'status text',
    url: '/api/v1/dispenses',
    error: body,
    headers: new HttpHeaders(headers),
  });
}

describe('toApiError', () => {
  describe('problem+json responses', () => {
    it('keeps the server decision: status, code, title, detail, request id and every violation in order', () => {
      const first = violation();
      const second = violation({
        code: 'authorisation_required',
        field: 'authorisation_ref',
        message: 'An authorisation reference is required.',
        context: { rule_id: 7 },
      });

      const error = toApiError(httpError(422, problem({ violations: [first, second] })));

      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({
        status: 422,
        code: 'dispense_rejected',
        title: 'Dispense rejected',
        detail: 'The dispense violates 1 formulary rule.',
        requestId: 'req-9f0c1c5e',
        isNetworkError: false,
      });
      expect(error.violations).toEqual([first, second]);
    });

    it('is not retryable when the server has made a decision about the request', () => {
      expect(toApiError(httpError(422, problem())).retryable).toBe(false);
      expect(
        toApiError(httpError(409, problem({ status: 409, code: 'rule_conflict' }))).retryable,
      ).toBe(false);
      expect(
        toApiError(httpError(404, problem({ status: 404, code: 'medicine_not_found' }))).retryable,
      ).toBe(false);
    });

    it('is retryable for a server-side failure even though it carries a problem body', () => {
      const busy = toApiError(
        httpError(
          503,
          problem({ status: 503, code: 'resource_busy', title: 'Resource busy', violations: [] }),
        ),
      );

      expect(busy).toMatchObject({
        status: 503,
        code: 'resource_busy',
        retryable: true,
        isNetworkError: false,
      });
      expect(busy.violations).toEqual([]);
    });

    it('falls back to the X-Request-ID header when the body carries no request id', () => {
      const { request_id: _omitted, ...withoutRequestId } = problem();

      const error = toApiError(httpError(422, withoutRequestId, { 'X-Request-ID': 'from-header' }));

      expect(error.requestId).toBe('from-header');
    });

    it('drops malformed violations, keeps valid ones and defaults a missing field and context', () => {
      const body = {
        ...problem(),
        violations: [
          { code: 'required', message: 'Field required.' },
          { code: 'no_message', field: 'quantity' },
          'not an object',
          null,
          { code: 'invalid_type', field: 7, message: 'Wrong type.', context: 'oops' },
        ],
      };

      const error = toApiError(httpError(422, body));

      expect(error.violations).toEqual([
        { code: 'required', field: null, message: 'Field required.', context: {} },
        { code: 'invalid_type', field: null, message: 'Wrong type.', context: {} },
      ]);
    });

    it('uses a generic detail when the problem has none and no violations list', () => {
      const error = toApiError(httpError(500, { code: 'internal_error', title: 'Internal error' }));

      expect(error.violations).toEqual([]);
      expect(error.detail).toBe('Something went wrong on the server. Try again in a moment.');
    });
  });

  describe('failures that are not problem+json', () => {
    it('turns an HTML 502 from a proxy into a generic, retryable error without leaking the body', () => {
      const html = '<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>';

      const error = toApiError(httpError(502, html, { 'X-Request-ID': 'edge-1' }));

      expect(error).toMatchObject({
        status: 502,
        code: 'service_unavailable',
        title: 'Service unavailable',
        requestId: 'edge-1',
        isNetworkError: false,
        retryable: true,
      });
      expect(error.violations).toEqual([]);
      expect(`${error.title} ${error.detail} ${error.message}`).not.toMatch(/<|nginx/);
    });

    it('maps a body-less 500 to an internal error that is retryable', () => {
      const error = toApiError(httpError(500, null));

      expect(error).toMatchObject({ status: 500, code: 'internal_error', retryable: true });
    });

    it('ignores JSON that is not problem-shaped, such as a framework default error', () => {
      const error = toApiError(httpError(404, { detail: 'Not Found' }));

      expect(error).toMatchObject({
        status: 404,
        code: 'unexpected_response',
        title: 'Request failed',
        retryable: false,
      });
      expect(error.detail).not.toContain('Not Found');
    });
  });

  describe('no response', () => {
    it('reports a network failure (status 0) as a retryable network error', () => {
      const error = toApiError(httpError(0, new ProgressEvent('error')));

      expect(error).toMatchObject({
        status: 0,
        code: 'network_error',
        isNetworkError: true,
        retryable: true,
        requestId: null,
      });
    });

    it('reports a timeout as a retryable network error', () => {
      const error = toApiError(new TimeoutError());

      expect(error).toMatchObject({
        status: 0,
        code: 'timeout',
        isNetworkError: true,
        retryable: true,
      });
    });

    it('wraps an unknown failure without claiming it is a network problem or worth retrying', () => {
      const error = toApiError(new RangeError('bug'));

      expect(error).toMatchObject({
        status: 0,
        code: 'client_error',
        isNetworkError: false,
        retryable: false,
      });
    });

    it('returns an ApiError unchanged', () => {
      const original = toApiError(httpError(422, problem()));

      expect(toApiError(original)).toBe(original);
    });
  });
});
