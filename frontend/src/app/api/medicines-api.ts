import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map } from 'rxjs';

import { API_BASE_URL, queryParams } from './api-config';
import {
  MedicineDetail,
  MedicineOut,
  NewRule,
  Page,
  RuleOut,
  RuleList,
  RuleSupersedeResult,
} from './models';

export interface MedicineSearch {
  q?: string;
  cursor?: string | null;
  limit?: number;
}

@Injectable({ providedIn: 'root' })
export class MedicinesApi {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = inject(API_BASE_URL);

  search(search: MedicineSearch): Observable<Page<MedicineOut>> {
    return this.http.get<Page<MedicineOut>>(`${this.baseUrl}/medicines`, {
      params: queryParams({ q: search.q, cursor: search.cursor, limit: search.limit }),
    });
  }

  get(code: string): Observable<MedicineDetail> {
    return this.http.get<MedicineDetail>(this.medicineUrl(code));
  }

  /** Ascending by `effective_from`; not paginated (a handful of rows per medicine). */
  rules(code: string): Observable<readonly RuleOut[]> {
    return this.http
      .get<RuleList>(`${this.medicineUrl(code)}/rules`)
      .pipe(map((list) => list.items));
  }

  introduceRule(code: string, rule: NewRule): Observable<RuleSupersedeResult> {
    return this.http.post<RuleSupersedeResult>(`${this.medicineUrl(code)}/rules`, rule);
  }

  private medicineUrl(code: string): string {
    return `${this.baseUrl}/medicines/${encodeURIComponent(code)}`;
  }
}
