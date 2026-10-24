import { HttpInterceptorFn } from '@angular/common/http';
import { catchError, throwError, timeout } from 'rxjs';

import { toApiError } from './api-error';

/** Longer than the server's statement timeout (15 s), so a slow server answers before the client gives up. */
export const REQUEST_TIMEOUT_MS = 20_000;

/** The one place where transport failures become `ApiError`. */
export const apiErrorInterceptor: HttpInterceptorFn = (request, next) =>
  next(request).pipe(
    // `each`, not `first`: the backend emits a "sent" event immediately, then waits for the response.
    timeout({ each: REQUEST_TIMEOUT_MS }),
    catchError((error: unknown) => throwError(() => toApiError(error))),
  );
