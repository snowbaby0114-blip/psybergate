import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { dispense, problem } from '../../testing/fixtures';
import { ApiError } from './api-error';
import { DispenseResult, DispensesApi } from './dispenses-api';
import { apiErrorInterceptor } from './error-interceptor';
import { DispenseRequest } from './models';

const REQUEST: DispenseRequest = {
  idempotency_key: 'key-1',
  medicine_code: 'MED-0001',
  patient_ref: 'PT-7K2M9Q',
  quantity: 20,
};

describe('DispensesApi', () => {
  let api: DispensesApi;
  let backend: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([apiErrorInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    api = TestBed.inject(DispensesApi);
    backend = TestBed.inject(HttpTestingController);
  });

  afterEach(() => backend.verify());

  describe('create', () => {
    it('posts the request body, including the idempotency key, and returns the created dispense', () => {
      let result: DispenseResult | undefined;
      api.create(REQUEST).subscribe((value) => (result = value));

      const request = backend.expectOne('/api/v1/dispenses');
      expect(request.request.method).toBe('POST');
      expect(request.request.body).toEqual(REQUEST);
      request.flush(dispense(), { status: 201, statusText: 'Created' });

      expect(result).toEqual({ dispense: dispense(), replayed: false });
    });

    it('flags a replay when the server answers with Idempotency-Replayed: true', () => {
      let result: DispenseResult | undefined;
      api.create(REQUEST).subscribe((value) => (result = value));

      backend.expectOne('/api/v1/dispenses').flush(dispense(), {
        status: 201,
        statusText: 'Created',
        headers: { 'Idempotency-Replayed': 'true' },
      });

      expect(result?.replayed).toBe(true);
    });

    it('fails with an ApiError when a success response has no body', () => {
      let error: unknown;
      api.create(REQUEST).subscribe({ error: (value: unknown) => (error = value) });

      backend.expectOne('/api/v1/dispenses').flush(null, { status: 201, statusText: 'Created' });

      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({ code: 'unexpected_response', retryable: false });
    });

    it('surfaces a rejection as an ApiError with its violations', () => {
      let error: unknown;
      api.create(REQUEST).subscribe({ error: (value: unknown) => (error = value) });

      backend
        .expectOne('/api/v1/dispenses')
        .flush(problem(), { status: 422, statusText: 'Unprocessable Entity' });

      expect(error).toMatchObject({ status: 422, code: 'dispense_rejected' });
    });
  });

  describe('list', () => {
    it('sends only the filters that are set', () => {
      api.list({ patient_ref: 'PT-1', medicine: '', cursor: 'next', limit: 25 }).subscribe();

      const request = backend.expectOne((r) => r.url === '/api/v1/dispenses');

      expect(request.request.params.keys().sort()).toEqual(['cursor', 'limit', 'patient_ref']);
      expect(request.request.params.get('patient_ref')).toBe('PT-1');
      request.flush({ items: [], next_cursor: null });
    });
  });
});
