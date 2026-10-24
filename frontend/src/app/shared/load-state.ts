import { Observable, catchError, map, of, startWith, switchMap } from 'rxjs';

import { ApiError, toApiError } from '../api/api-error';

export type LoadState<T> =
  | { readonly status: 'idle' }
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly data: T }
  | { readonly status: 'error'; readonly error: ApiError };

/** A request together with the state of loading it, so a view reads both from one consistent value. */
export interface Loaded<R, T> {
  readonly request: R;
  readonly state: LoadState<T>;
}

/**
 * The single pipeline behind every view: requests (derived from the URL) in, load states out.
 *
 * - A newer request cancels the one in flight (switchMap unsubscribes, which aborts the HTTP call),
 *   so a stale response can never overwrite a newer one.
 * - `retry` re-runs the current request.
 * - `source` returns null when the request cannot be loaded yet (for example a ledger without a
 *   patient); the state is then `idle` and nothing is sent.
 */
export function loadLatest<R, T>(
  requests: Observable<R>,
  source: (request: R) => Observable<T> | null,
  retry: Observable<void>,
): Observable<Loaded<R, T>> {
  return requests.pipe(
    switchMap((request) =>
      retry.pipe(
        startWith(undefined),
        switchMap(() => {
          const response = source(request);
          if (response === null) {
            return of<LoadState<T>>({ status: 'idle' });
          }
          return response.pipe(
            map((data): LoadState<T> => ({ status: 'ready', data })),
            catchError((error: unknown) =>
              of<LoadState<T>>({ status: 'error', error: toApiError(error) }),
            ),
            startWith<LoadState<T>>({ status: 'loading' }),
          );
        }),
        map((state): Loaded<R, T> => ({ request, state })),
      ),
    ),
  );
}
