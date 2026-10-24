import { HttpParams } from '@angular/common/http';
import { InjectionToken } from '@angular/core';

/** Relative on purpose: nginx (production) and the ng-serve proxy (development) forward /api to the API. */
export const API_BASE_URL = new InjectionToken<string>('API_BASE_URL', {
  providedIn: 'root',
  factory: () => '/api/v1',
});

type ParamValue = string | number | null | undefined;

/** Builds query params, leaving out values that are absent or empty. */
export function queryParams(values: Readonly<Record<string, ParamValue>>): HttpParams {
  let params = new HttpParams();
  for (const [name, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== '') {
      params = params.set(name, String(value));
    }
  }
  return params;
}
