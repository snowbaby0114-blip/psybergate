import { provideHttpClient, withInterceptors } from '@angular/common/http';
import {
  HttpTestingController,
  TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { NavigationEnd, Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { filter, firstValueFrom } from 'rxjs';

import { apiErrorInterceptor } from '../api/error-interceptor';
import { DispenseOut, Page } from '../api/models';
import { dispense, problem } from '../../testing/fixtures';
import { Ledger } from './ledger';

function page(items: DispenseOut[], nextCursor: string | null): Page<DispenseOut> {
  return { items, next_cursor: nextCursor };
}

function paramsOf(request: TestRequest): Record<string, string | null> {
  const { params } = request.request;
  return Object.fromEntries(params.keys().map((key) => [key, params.get(key)]));
}

describe('Ledger', () => {
  let harness: RouterTestingHarness;
  let backend: HttpTestingController;
  let router: Router;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'ledger', component: Ledger }]),
        provideHttpClient(withInterceptors([apiErrorInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    backend = TestBed.inject(HttpTestingController);
    router = TestBed.inject(Router);
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => backend.verify({ ignoreCancelled: true }));

  const root = (): HTMLElement => harness.routeNativeElement as HTMLElement;
  const squash = (text: string | null | undefined): string =>
    (text ?? '').replace(/\s+/g, ' ').trim();
  const text = (selector: string): string => squash(root().querySelector(selector)?.textContent);
  const patientBox = (): HTMLInputElement =>
    root().querySelector<HTMLInputElement>('#ledger-patient')!;
  const medicineBox = (): HTMLInputElement =>
    root().querySelector<HTMLInputElement>('#ledger-medicine')!;
  const button = (label: string): HTMLButtonElement =>
    Array.from(root().querySelectorAll('button')).find((b) => squash(b.textContent) === label)!;
  const cells = (row: number): string[] =>
    Array.from(root().querySelectorAll('tbody tr')[row]?.querySelectorAll('td') ?? []).map((cell) =>
      squash(cell.textContent),
    );

  async function navigating(action: () => void): Promise<void> {
    const done = firstValueFrom(router.events.pipe(filter((e) => e instanceof NavigationEnd)));
    action();
    await done;
    harness.detectChanges();
  }

  function enter(box: HTMLInputElement, value: string): void {
    box.value = value;
    box.dispatchEvent(new Event('input'));
  }

  function submit(): void {
    root().querySelector('form')!.dispatchEvent(new Event('submit'));
    harness.detectChanges();
  }

  const listing = (): TestRequest => backend.expectOne((r) => r.url === '/api/v1/dispenses');

  async function open(url: string, first: Page<DispenseOut>): Promise<void> {
    await harness.navigateByUrl(url);
    listing().flush(first);
    harness.detectChanges();
  }

  describe('without a patient', () => {
    it('asks for a patient reference and requests nothing', async () => {
      await harness.navigateByUrl('/ledger');
      harness.detectChanges();

      expect(text('[role=status]')).toBe('Enter a patient reference to see their dispenses.');
      backend.expectNone((r) => r.url === '/api/v1/dispenses');
      expect(root().querySelector('app-pager')).toBeNull();
      expect(root().querySelector('table')).toBeNull();
    });

    it('treats a blank patient_ref in the URL as no patient', async () => {
      await harness.navigateByUrl('/ledger?patient_ref=%20%20&medicine=MED-1');
      harness.detectChanges();

      backend.expectNone((r) => r.url === '/api/v1/dispenses');
      expect(text('[role=status]')).toContain('Enter a patient reference');
    });
  });

  describe('loading from the URL', () => {
    it('shows loading, then the dispenses with the time in SAST, newest first as sent', async () => {
      await harness.navigateByUrl('/ledger?patient_ref=PT-7K2M9Q');
      harness.detectChanges();
      const request = listing();
      expect(paramsOf(request)).toEqual({ patient_ref: 'PT-7K2M9Q' });
      expect(text('[role=status]')).toBe('Loading dispenses…');

      request.flush(
        page(
          [
            dispense({ id: 2, dispensed_at: '2026-02-28T22:30:00Z', quantity: 30, rule_id: 13 }),
            dispense({
              id: 1,
              dispensed_at: '2026-02-01T08:00:00Z',
              authorisation_ref: 'AUTH-9',
              medicine_code: 'MED-0002',
              medicine_name: 'Brufen',
            }),
          ],
          'C1',
        ),
      );
      harness.detectChanges();

      expect(text('[role=status]')).toBe('2 dispenses on this page, newest first');
      expect(cells(0)).toEqual(['2026-03-01 00:30 SAST', 'MED-0001 Amoxil', '30', '—', '13']);
      expect(cells(1)).toEqual(['2026-02-01 10:00 SAST', 'MED-0002 Brufen', '20', 'AUTH-9', '12']);
      expect(root().querySelector('tbody tr a')?.getAttribute('href')).toBe('/medicines/MED-0001');
      expect(text('.pager-page')).toBe('Page 1');
      expect(button('Previous').disabled).toBe(true);
      expect(button('Next').disabled).toBe(false);
    });

    it('fills the boxes from the URL, as after a reload or the link from the dispense page', async () => {
      await open('/ledger?patient_ref=PT-1&medicine=MED-0002', page([dispense()], null));

      expect(patientBox().value).toBe('PT-1');
      expect(medicineBox().value).toBe('MED-0002');
    });

    it('sends the medicine filter only when there is one', async () => {
      await harness.navigateByUrl('/ledger?patient_ref=PT-1&medicine=MED-0002');

      expect(paramsOf(listing())).toEqual({ patient_ref: 'PT-1', medicine: 'MED-0002' });
    });

    it('says which patient has nothing recorded, and for which medicine when filtered', async () => {
      await open('/ledger?patient_ref=PT-1', page([], null));
      expect(text('[role=status]')).toBe('No dispenses recorded for "PT-1".');

      await navigating(() => void router.navigateByUrl('/ledger?patient_ref=PT-1&medicine=MED-9'));
      listing().flush(page([], null));
      harness.detectChanges();
      expect(text('[role=status]')).toBe('No dispenses recorded for "PT-1" and medicine "MED-9".');
      expect(root().querySelector('table')).toBeNull();
    });
  });

  describe('looking a patient up', () => {
    it('submitting puts patient and medicine in the URL and starts from the first page', async () => {
      await open('/ledger?patient_ref=PT-OLD&cursor=C2&trail=C1', page([dispense()], null));

      enter(patientBox(), '  PT-NEW ');
      enter(medicineBox(), 'MED-0002');
      await navigating(submit);

      expect(router.url).toBe('/ledger?patient_ref=PT-NEW&medicine=MED-0002');
      expect(paramsOf(listing())).toEqual({ patient_ref: 'PT-NEW', medicine: 'MED-0002' });
    });

    it('goes back to the prompt when the patient is cleared', async () => {
      await open('/ledger?patient_ref=PT-1', page([dispense()], null));

      enter(patientBox(), '');
      await navigating(submit);

      expect(router.url).toBe('/ledger');
      backend.expectNone((r) => r.url === '/api/v1/dispenses');
      expect(text('[role=status]')).toContain('Enter a patient reference');
    });

    it('reloads when the same patient is asked for again', async () => {
      await open('/ledger?patient_ref=PT-1', page([dispense({ quantity: 1 })], null));

      submit();
      const reload = listing();
      reload.flush(page([dispense({ quantity: 99 })], null));
      harness.detectChanges();

      expect(cells(0)[2]).toBe('99');
      expect(router.url).toBe('/ledger?patient_ref=PT-1');
    });

    it('cancels the request in flight when another patient is asked for', async () => {
      await harness.navigateByUrl('/ledger?patient_ref=PT-1');
      const stale = backend.expectOne((r) => r.params.get('patient_ref') === 'PT-1');

      await navigating(() => void router.navigateByUrl('/ledger?patient_ref=PT-2'));
      backend
        .expectOne((r) => r.params.get('patient_ref') === 'PT-2')
        .flush(page([dispense({ patient_ref: 'PT-2', quantity: 7 })], null));
      harness.detectChanges();

      expect(stale.cancelled).toBe(true);
      expect(cells(0)[2]).toBe('7');
    });
  });

  describe('paging', () => {
    it('goes forward twice and back once, keeping patient and medicine and reproducing the earlier page', async () => {
      await open('/ledger?patient_ref=PT-1&medicine=MED-1', page([dispense({ id: 3 })], 'C1'));

      await navigating(() => button('Next').click());
      listing().flush(page([dispense({ id: 2 })], 'C2'));
      harness.detectChanges();
      await navigating(() => button('Next').click());
      expect(router.url).toBe('/ledger?patient_ref=PT-1&medicine=MED-1&cursor=C2&trail=C1');
      listing().flush(page([dispense({ id: 1 })], null));
      harness.detectChanges();
      expect(text('.pager-page')).toBe('Page 3');
      expect(button('Next').disabled).toBe(true);

      await navigating(() => button('Previous').click());

      expect(router.url).toBe('/ledger?patient_ref=PT-1&medicine=MED-1&cursor=C1');
      expect(paramsOf(listing())).toEqual({ patient_ref: 'PT-1', medicine: 'MED-1', cursor: 'C1' });
    });

    it('does not throw away what was typed but not submitted when paging', async () => {
      await open('/ledger?patient_ref=PT-1', page([dispense()], 'C1'));
      enter(patientBox(), 'PT-2');

      await navigating(() => button('Next').click());
      listing().flush(page([dispense()], null));
      harness.detectChanges();

      expect(patientBox().value).toBe('PT-2');
      expect(router.url).toBe('/ledger?patient_ref=PT-1&cursor=C1');
    });

    it('copies a patient that arrives from outside the boxes, such as back/forward', async () => {
      await open('/ledger?patient_ref=PT-1', page([dispense()], null));

      await navigating(() => void router.navigateByUrl('/ledger?patient_ref=PT-9'));
      listing().flush(page([], null));
      harness.detectChanges();

      expect(patientBox().value).toBe('PT-9');
    });
  });

  describe('failure', () => {
    it('explains it with a reference and retries on demand', async () => {
      await harness.navigateByUrl('/ledger?patient_ref=PT-1');
      listing().flush(
        problem({
          status: 503,
          code: 'resource_busy',
          title: 'Resource busy',
          detail: 'The system is busy. Try again.',
          violations: [],
        }),
        { status: 503, statusText: 'Service Unavailable' },
      );
      harness.detectChanges();

      const alert = text('[role=alert]');
      expect(alert).toContain('Resource busy');
      expect(alert).toContain('The system is busy. Try again.');
      expect(alert).toContain('req-9f0c1c5e');
      expect(root().querySelector('table')).toBeNull();

      button('Retry').click();
      listing().flush(page([dispense()], null));
      harness.detectChanges();

      expect(root().querySelector('[role=alert]')).toBeNull();
      expect(root().querySelectorAll('tbody tr')).toHaveLength(1);
    });

    it('never shows a proxy error page to the user', async () => {
      await harness.navigateByUrl('/ledger?patient_ref=PT-1');
      listing().flush('<html>502 Bad Gateway nginx</html>', {
        status: 502,
        statusText: 'Bad Gateway',
      });
      harness.detectChanges();

      expect(text('[role=alert]')).toContain('Service unavailable');
      expect(text('[role=alert]')).not.toMatch(/nginx|<html/);
    });

    it('offers a way back to the first page when a deep page fails', async () => {
      await harness.navigateByUrl('/ledger?patient_ref=PT-1&cursor=bad');
      listing().flush(
        problem({ status: 400, code: 'invalid_cursor', title: 'Invalid cursor', violations: [] }),
        { status: 400, statusText: 'Bad Request' },
      );
      harness.detectChanges();
      const link = Array.from(root().querySelectorAll('a')).find(
        (a) => squash(a.textContent) === 'Back to first page',
      )!;

      await navigating(() => link.click());

      expect(router.url).toBe('/ledger?patient_ref=PT-1');
      expect(paramsOf(listing())).toEqual({ patient_ref: 'PT-1' });
    });
  });
});
