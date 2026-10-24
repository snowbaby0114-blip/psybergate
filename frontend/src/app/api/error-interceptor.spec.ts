import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { problem } from '../../testing/fixtures';
import { ApiError } from './api-error';
import { REQUEST_TIMEOUT_MS, apiErrorInterceptor } from './error-interceptor';

describe('apiErrorInterceptor', () => {
  let http: HttpClient;
  let backend: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([apiErrorInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    http = TestBed.inject(HttpClient);
    backend = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    backend.verify();
    vi.useRealTimers();
  });

  function failureOf(url: string): { current: unknown } {
    const captured: { current: unknown } = { current: undefined };
    http.get(url).subscribe({ error: (error: unknown) => (captured.current = error) });
    return captured;
  }

  it('passes successful responses through untouched', () => {
    let body: unknown;
    http.get('/api/v1/medicines').subscribe((value) => (body = value));

    backend.expectOne('/api/v1/medicines').flush({ items: [], next_cursor: null });

    expect(body).toEqual({ items: [], next_cursor: null });
  });

  it('delivers a problem+json failure as an ApiError carrying the violations', () => {
    const failure = failureOf('/api/v1/dispenses');

    backend.expectOne('/api/v1/dispenses').flush(problem(), {
      status: 422,
      statusText: 'Unprocessable Entity',
      headers: { 'Content-Type': 'application/problem+json' },
    });

    expect(failure.current).toBeInstanceOf(ApiError);
    expect(failure.current).toMatchObject({
      status: 422,
      code: 'dispense_rejected',
      retryable: false,
    });
    expect((failure.current as ApiError).violations).toHaveLength(1);
  });

  it('delivers a non-JSON proxy error as a generic ApiError', () => {
    const failure = failureOf('/api/v1/medicines');

    backend
      .expectOne('/api/v1/medicines')
      .flush('<html>Bad Gateway</html>', { status: 502, statusText: 'Bad Gateway' });

    expect(failure.current).toMatchObject({
      status: 502,
      code: 'service_unavailable',
      retryable: true,
    });
  });

  it('delivers a network failure as an ApiError flagged isNetworkError', () => {
    const failure = failureOf('/api/v1/medicines');

    backend.expectOne('/api/v1/medicines').error(new ProgressEvent('error'));

    expect(failure.current).toMatchObject({
      status: 0,
      code: 'network_error',
      isNetworkError: true,
    });
  });

  it('gives up when no response arrives in time, cancelling the request', () => {
    vi.useFakeTimers();
    const failure = failureOf('/api/v1/medicines');
    const request = backend.expectOne('/api/v1/medicines');

    vi.advanceTimersByTime(REQUEST_TIMEOUT_MS - 1);
    expect(failure.current).toBeUndefined();
    vi.advanceTimersByTime(1);

    expect(request.cancelled).toBe(true);
    expect(failure.current).toMatchObject({
      status: 0,
      code: 'timeout',
      isNetworkError: true,
      retryable: true,
    });
  });
});
