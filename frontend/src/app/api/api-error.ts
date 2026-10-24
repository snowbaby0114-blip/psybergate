import { HttpErrorResponse } from '@angular/common/http';
import { TimeoutError } from 'rxjs';

import { Violation } from './models';

interface ApiErrorFields {
  status: number;
  code: string;
  title: string;
  detail: string;
  requestId?: string | null;
  violations?: readonly Violation[];
  isNetworkError?: boolean;
}

/**
 * The only error type the rest of the app deals with. Whatever went wrong (problem+json, an HTML error
 * page from a proxy, no response at all, a timeout) is normalised here, so views never touch raw
 * responses and never show raw bodies to the user.
 */
export class ApiError extends Error {
  /** HTTP status; 0 when no response was received. */
  readonly status: number;
  readonly code: string;
  readonly title: string;
  readonly detail: string;
  readonly requestId: string | null;
  readonly violations: readonly Violation[];
  /** No usable response arrived: the network failed or the request timed out. */
  readonly isNetworkError: boolean;
  /** Sending the same request again may succeed: no response, or a server-side failure. */
  readonly retryable: boolean;

  constructor(fields: ApiErrorFields) {
    super(fields.detail);
    this.name = 'ApiError';
    this.status = fields.status;
    this.code = fields.code;
    this.title = fields.title;
    this.detail = fields.detail;
    this.requestId = fields.requestId ?? null;
    this.violations = fields.violations ?? [];
    this.isNetworkError = fields.isNetworkError ?? false;
    this.retryable = this.isNetworkError || fields.status >= 500;
  }
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }
  if (error instanceof TimeoutError) {
    return new ApiError({
      status: 0,
      code: 'timeout',
      title: 'The server took too long to respond',
      detail: 'Try again in a moment.',
      isNetworkError: true,
    });
  }
  if (error instanceof HttpErrorResponse) {
    return fromHttpError(error);
  }
  return new ApiError({
    status: 0,
    code: 'client_error',
    title: 'Unexpected error',
    detail: 'Something went wrong in the application. Reload the page and try again.',
  });
}

function fromHttpError(response: HttpErrorResponse): ApiError {
  if (response.status === 0) {
    return new ApiError({
      status: 0,
      code: 'network_error',
      title: 'Cannot reach the server',
      detail: 'Check your connection and try again.',
      isNetworkError: true,
    });
  }
  const headerRequestId = response.headers.get('X-Request-ID');
  const generic = genericForStatus(response.status);
  const body: unknown = response.error;
  if (isRecord(body) && typeof body['code'] === 'string' && typeof body['title'] === 'string') {
    const requestId = body['request_id'];
    return new ApiError({
      status: response.status,
      code: body['code'],
      title: body['title'],
      detail: typeof body['detail'] === 'string' ? body['detail'] : generic.detail,
      requestId: typeof requestId === 'string' ? requestId : headerRequestId,
      violations: parseViolations(body['violations']),
    });
  }
  return new ApiError({ status: response.status, ...generic, requestId: headerRequestId });
}

function genericForStatus(status: number): { code: string; title: string; detail: string } {
  if (status === 502 || status === 503 || status === 504) {
    return {
      code: 'service_unavailable',
      title: 'Service unavailable',
      detail: 'The server is temporarily unavailable. Try again in a moment.',
    };
  }
  if (status >= 500) {
    return {
      code: 'internal_error',
      title: 'Unexpected server error',
      detail: 'Something went wrong on the server. Try again in a moment.',
    };
  }
  return {
    code: 'unexpected_response',
    title: 'Request failed',
    detail: 'The server could not process the request.',
  };
}

function parseViolations(raw: unknown): Violation[] {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.flatMap((item: unknown) => {
    if (!isRecord(item)) {
      return [];
    }
    const { code, field, message, context } = item;
    if (typeof code !== 'string' || typeof message !== 'string') {
      return [];
    }
    return [
      {
        code,
        field: typeof field === 'string' ? field : null,
        message,
        context: isRecord(context) ? context : {},
      },
    ];
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
