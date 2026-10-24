import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { MEDICINE, medicineDetail, rule } from '../../testing/fixtures';
import { MedicinesApi } from './medicines-api';
import { NewRule, Page, MedicineOut, RuleSupersedeResult } from './models';

describe('MedicinesApi', () => {
  let api: MedicinesApi;
  let backend: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(MedicinesApi);
    backend = TestBed.inject(HttpTestingController);
  });

  afterEach(() => backend.verify());

  it('searches with query, cursor and limit as query parameters', () => {
    let page: Page<MedicineOut> | undefined;
    api.search({ q: 'amox', cursor: 'abc=', limit: 8 }).subscribe((value) => (page = value));

    const request = backend.expectOne((r) => r.url === '/api/v1/medicines');
    expect(request.request.method).toBe('GET');
    expect(request.request.params.get('q')).toBe('amox');
    expect(request.request.params.get('cursor')).toBe('abc=');
    expect(request.request.params.get('limit')).toBe('8');
    request.flush({ items: [MEDICINE], next_cursor: null });

    expect(page?.items).toEqual([MEDICINE]);
  });

  it('leaves out empty and absent search parameters', () => {
    api.search({ q: '', cursor: null }).subscribe();

    const request = backend.expectOne('/api/v1/medicines');

    expect(request.request.params.keys()).toEqual([]);
    request.flush({ items: [], next_cursor: null });
  });

  it('percent-encodes the medicine code in the path', () => {
    let code: string | undefined;
    api.get('MED/1 2').subscribe((detail) => (code = detail.code));

    backend.expectOne('/api/v1/medicines/MED%2F1%202').flush(medicineDetail({ code: 'MED/1 2' }));

    expect(code).toBe('MED/1 2');
  });

  it('unwraps the rule list into an array, keeping the server order', () => {
    const rules = [rule({ id: 1, status: 'expired' }), rule({ id: 2 })];
    let result: readonly unknown[] | undefined;
    api.rules('MED-0001').subscribe((value) => (result = value));

    backend.expectOne('/api/v1/medicines/MED-0001/rules').flush({ items: rules });

    expect(result).toEqual(rules);
  });

  it('posts a new rule to the rules collection and returns both rules', () => {
    const body: NewRule = {
      effective_from: '2026-11-01',
      max_quantity_per_dispense: 30,
      max_quantity_per_30_days: 60,
      requires_authorisation: true,
    };
    const created: RuleSupersedeResult = { rule: rule({ id: 13 }), superseded: rule({ id: 12 }) };
    let result: RuleSupersedeResult | undefined;
    api.introduceRule('MED-0001', body).subscribe((value) => (result = value));

    const request = backend.expectOne('/api/v1/medicines/MED-0001/rules');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual(body);
    request.flush(created, { status: 201, statusText: 'Created' });

    expect(result).toEqual(created);
  });
});
