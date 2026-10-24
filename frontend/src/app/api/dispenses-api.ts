import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map } from 'rxjs';

import { ApiError } from './api-error';
import { API_BASE_URL, queryParams } from './api-config';
import { DispenseOut, DispenseRequest, Page } from './models';

export interface DispenseResult {
  readonly dispense: DispenseOut;
  /** The server recognised the idempotency key and returned the original outcome; nothing new was created. */
  readonly replayed: boolean;
}

export interface DispenseListQuery {
  patient_ref?: string;
  medicine?: string;
  cursor?: string | null;
  limit?: number;
}

@Injectable({ providedIn: 'root' })
export class DispensesApi {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = inject(API_BASE_URL);

  create(request: DispenseRequest): Observable<DispenseResult> {
    return this.http
      .post<DispenseOut>(`${this.baseUrl}/dispenses`, request, { observe: 'response' })
      .pipe(
        map((response) => {
          if (response.body === null) {
            throw new ApiError({
              status: response.status,
              code: 'unexpected_response',
              title: 'Unexpected response',
              detail: 'The server accepted the request but returned no dispense.',
            });
          }
          return {
            dispense: response.body,
            replayed: response.headers.get('Idempotency-Replayed') === 'true',
          };
        }),
      );
  }

  list(query: DispenseListQuery): Observable<Page<DispenseOut>> {
    return this.http.get<Page<DispenseOut>>(`${this.baseUrl}/dispenses`, {
      params: queryParams({
        patient_ref: query.patient_ref,
        medicine: query.medicine,
        cursor: query.cursor,
        limit: query.limit,
      }),
    });
  }
}
