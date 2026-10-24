import { HttpErrorResponse } from '@angular/common/http';
import { Observable, Subject, of, throwError } from 'rxjs';

import { ApiError } from '../api/api-error';
import { Loaded, loadLatest } from './load-state';

type Seen = Loaded<string, number>;

function collect(pipeline: Observable<Seen>): Seen[] {
  const seen: Seen[] = [];
  pipeline.subscribe((value) => seen.push(value));
  return seen;
}

describe('loadLatest', () => {
  it('emits loading and then the data for a request', () => {
    const requests = new Subject<string>();
    const seen = collect(
      loadLatest(requests, (request) => of(request.length), new Subject<void>()),
    );

    requests.next('abc');

    expect(seen).toEqual([
      { request: 'abc', state: { status: 'loading' } },
      { request: 'abc', state: { status: 'ready', data: 3 } },
    ]);
  });

  it('cancels the request in flight when a newer one arrives, so a late response cannot overwrite it', () => {
    const requests = new Subject<string>();
    const responses: Record<string, Subject<number>> = { a: new Subject(), b: new Subject() };
    const seen = collect(
      loadLatest(requests, (request) => responses[request] ?? null, new Subject<void>()),
    );

    requests.next('a');
    requests.next('b');
    expect(responses['a']?.observed).toBe(false);
    responses['b']?.next(2);
    responses['a']?.next(1);

    expect(seen.at(-1)).toEqual({ request: 'b', state: { status: 'ready', data: 2 } });
    expect(seen.filter((entry) => entry.state.status === 'ready')).toHaveLength(1);
  });

  it('turns a failure into an error state holding an ApiError, whatever was thrown', () => {
    const requests = new Subject<string>();
    const seen = collect(
      loadLatest(
        requests,
        () => throwError(() => new HttpErrorResponse({ status: 502, error: '<html></html>' })),
        new Subject<void>(),
      ),
    );

    requests.next('abc');

    const last = seen.at(-1)?.state;
    expect(last?.status).toBe('error');
    expect(last?.status === 'error' && last.error).toBeInstanceOf(ApiError);
    expect(last?.status === 'error' && last.error).toMatchObject({ status: 502, retryable: true });
  });

  it('runs the current request again on retry, going through loading', () => {
    const requests = new Subject<string>();
    const retry = new Subject<void>();
    let calls = 0;
    const seen = collect(
      loadLatest(
        requests,
        () => {
          calls += 1;
          return calls === 1 ? throwError(() => new HttpErrorResponse({ status: 503 })) : of(42);
        },
        retry,
      ),
    );

    requests.next('abc');
    retry.next();

    expect(seen.map((entry) => entry.state.status)).toEqual([
      'loading',
      'error',
      'loading',
      'ready',
    ]);
    expect(seen.at(-1)).toEqual({ request: 'abc', state: { status: 'ready', data: 42 } });
  });

  it('retries the latest request, not an earlier one', () => {
    const requests = new Subject<string>();
    const retry = new Subject<void>();
    const asked: string[] = [];
    collect(
      loadLatest(
        requests,
        (request) => {
          asked.push(request);
          return of(1);
        },
        retry,
      ),
    );

    requests.next('first');
    requests.next('second');
    retry.next();

    expect(asked).toEqual(['first', 'second', 'second']);
  });

  it('is idle, without calling anything, when the request cannot be loaded yet', () => {
    const requests = new Subject<string>();
    const source = vi.fn(() => null);
    const seen = collect(loadLatest(requests, source, new Subject<void>()));

    requests.next('');

    expect(seen).toEqual([{ request: '', state: { status: 'idle' } }]);
    expect(source).toHaveBeenCalledTimes(1);
  });
});
