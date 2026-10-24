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
import { medicineDetail, problem, rule } from '../../testing/fixtures';
import { MedicineDetail } from './medicine-detail';

describe('MedicineDetail', () => {
  let harness: RouterTestingHarness;
  let backend: HttpTestingController;
  let router: Router;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'medicines/:code', component: MedicineDetail }]),
        provideHttpClient(withInterceptors([apiErrorInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    backend = TestBed.inject(HttpTestingController);
    router = TestBed.inject(Router);
    harness = await RouterTestingHarness.create();
  });

  // Cancelled requests stay "open" in the testing backend; cancelling is asserted where it matters.
  afterEach(() => backend.verify({ ignoreCancelled: true }));

  const root = (): HTMLElement => harness.routeNativeElement as HTMLElement;
  const text = (): string => (root().textContent ?? '').replace(/\s+/g, ' ').trim();

  /** The one request for `url` that is still wanted (a retry may leave a cancelled one behind). */
  function live(url: string): TestRequest {
    const found = backend.match(url).filter((request) => !request.cancelled);
    expect(found, url).toHaveLength(1);
    return found[0]!;
  }

  function respond(code: string, detail = medicineDetail(), rules = [rule()]): void {
    live(`/api/v1/medicines/${code}`).flush(detail);
    live(`/api/v1/medicines/${code}/rules`).flush({ items: rules });
    harness.detectChanges();
  }

  /** The dt/dd pairs of the medicine header, e.g. { Form: 'capsule' }. */
  function headerFacts(): Record<string, string> {
    return Object.fromEntries(
      Array.from(root().querySelectorAll('dl')[0]?.querySelectorAll('div') ?? []).map((group) => [
        group.querySelector('dt')?.textContent?.trim() ?? '',
        group.querySelector('dd')?.textContent?.trim() ?? '',
      ]),
    );
  }

  it('asks for the medicine and its rules together and shows both', async () => {
    await harness.navigateByUrl('/medicines/MED-0001');
    harness.detectChanges();
    expect(text()).toContain('Loading MED-0001…');

    respond('MED-0001', medicineDetail(), [
      rule({ id: 11, effective_from: '2025-01-01', effective_to: '2026-03-01', status: 'expired' }),
      rule({ id: 12 }),
    ]);

    expect(root().querySelector('h1')?.textContent?.replace(/\s+/g, ' ').trim()).toBe(
      'Amoxil MED-0001',
    );
    expect(headerFacts()).toEqual({ Form: 'capsule', Strength: '500 mg', Status: 'Active' });
    expect(root().querySelectorAll('.timeline li')).toHaveLength(2);
  });

  it('states the rule in force today with the server date it was decided on', async () => {
    await harness.navigateByUrl('/medicines/MED-0001');
    respond(
      'MED-0001',
      medicineDetail({
        as_of: '2026-03-15',
        current_rule: rule({
          max_quantity_per_dispense: 60,
          max_quantity_per_30_days: 120,
          requires_authorisation: true,
        }),
      }),
    );

    expect(text()).toContain(
      'Today (2026-03-15, SAST): at most 60 per dispense and 120 per 30 days; authorisation required.',
    );
  });

  it('says when no rule is in force', async () => {
    await harness.navigateByUrl('/medicines/MED-0001');
    respond('MED-0001', medicineDetail({ as_of: '2026-03-15', current_rule: null }), [
      rule({ effective_from: '2026-09-01', status: 'scheduled' }),
    ]);

    expect(text()).toContain('No rule is in force today (2026-03-15, SAST).');
  });

  it('flags an inactive medicine', async () => {
    await harness.navigateByUrl('/medicines/MED-0001');
    respond('MED-0001', medicineDetail({ is_active: false }));

    expect(root().querySelector('.badge-inactive')?.textContent).toContain('Inactive');
  });

  it('requests a code with reserved characters percent-encoded', async () => {
    await harness.navigateByUrl('/medicines/MED%2F1');

    respond('MED%2F1', medicineDetail({ code: 'MED/1' }));

    expect(text()).toContain('MED/1');
  });

  describe('when the medicine does not exist', () => {
    it('shows a friendly page with a way back instead of an error panel', async () => {
      await harness.navigateByUrl('/medicines/NOPE');
      const notFound = problem({
        status: 404,
        code: 'medicine_not_found',
        title: 'Medicine not found',
        detail: 'No medicine has code NOPE.',
        violations: [],
      });
      const rules = backend.expectOne('/api/v1/medicines/NOPE/rules');
      backend
        .expectOne('/api/v1/medicines/NOPE')
        .flush(notFound, { status: 404, statusText: 'Not Found' });
      harness.detectChanges();

      expect(rules.cancelled).toBe(true);
      expect(root().querySelector('h1')?.textContent).toBe('Medicine not found');
      expect(text()).toContain('There is no medicine with code "NOPE".');
      const back = Array.from(root().querySelectorAll('a')).find((a) =>
        a.textContent?.includes('Back to the medicines'),
      );
      expect(back?.getAttribute('href')).toBe('/medicines');
      expect(root().querySelectorAll('button')).toHaveLength(0);
    });

    it('shows the same page when the address does not route at all (a code containing a slash)', async () => {
      await harness.navigateByUrl('/medicines/A%2FB');
      const unrouted = problem({
        status: 404,
        code: 'not_found',
        title: 'Not found',
        detail: 'No endpoint matches the requested path.',
        violations: [],
      });
      backend.expectOne('/api/v1/medicines/A%2FB/rules');
      backend
        .expectOne('/api/v1/medicines/A%2FB')
        .flush(unrouted, { status: 404, statusText: 'Not Found' });
      harness.detectChanges();

      expect(root().querySelector('h1')?.textContent).toBe('Medicine not found');
      expect(text()).toContain('There is no medicine with code "A/B".');
      expect(root().querySelectorAll('button')).toHaveLength(0);
    });
  });

  describe('when loading fails', () => {
    async function failWith(status: number): Promise<void> {
      await harness.navigateByUrl('/medicines/MED-0001');
      backend
        .expectOne('/api/v1/medicines/MED-0001')
        .flush(
          problem({ status, code: 'internal_error', title: 'Internal error', violations: [] }),
          {
            status,
            statusText: 'Server Error',
          },
        );
      harness.detectChanges();
    }

    it('shows the error with a reference and retries both requests', async () => {
      await failWith(500);

      expect(root().querySelector('[role=alert]')?.textContent).toContain('Internal error');
      expect(text()).toContain('req-9f0c1c5e');

      root().querySelector('button')!.click();
      respond('MED-0001');

      expect(root().querySelector('[role=alert]')).toBeNull();
      expect(text()).toContain('Amoxil');
    });

    it('does not show a half-loaded page when only the rules request fails', async () => {
      await harness.navigateByUrl('/medicines/MED-0001');
      live('/api/v1/medicines/MED-0001').flush(medicineDetail());
      live('/api/v1/medicines/MED-0001/rules').flush('<html>Bad Gateway</html>', {
        status: 502,
        statusText: 'Bad Gateway',
      });
      harness.detectChanges();

      expect(root().querySelector('[role=alert]')?.textContent).toContain('Service unavailable');
      expect(root().querySelector('.timeline')).toBeNull();
      expect(root().querySelector('h1')).toBeNull();
    });
  });

  describe('introducing a rule', () => {
    const introduce = (): void => {
      for (const [name, value] of [
        ['effective_from', '2026-11-01'],
        ['max_quantity_per_dispense', '30'],
        ['max_quantity_per_30_days', '60'],
      ] as const) {
        const field = root().querySelector<HTMLInputElement>(`#rule-${name}`)!;
        field.value = value;
        field.dispatchEvent(new Event('input'));
      }
      root().querySelector<HTMLButtonElement>('app-new-rule-form button[type=submit]')!.click();
      harness.detectChanges();
    };

    it('reloads the timeline and says what changed, including the rule it shortened', async () => {
      await harness.navigateByUrl('/medicines/MED-0001');
      respond('MED-0001', medicineDetail(), [rule({ id: 12, effective_from: '2026-03-01' })]);
      expect(root().querySelectorAll('.timeline li')).toHaveLength(1);

      introduce();
      live('/api/v1/medicines/MED-0001/rules').flush(
        {
          rule: rule({ id: 13, effective_from: '2026-11-01', status: 'scheduled' }),
          superseded: rule({ id: 12, effective_from: '2026-03-01', effective_to: '2026-11-01' }),
        },
        { status: 201, statusText: 'Created' },
      );
      harness.detectChanges();
      respond('MED-0001', medicineDetail(), [
        rule({ id: 12, effective_from: '2026-03-01', effective_to: '2026-11-01' }),
        rule({ id: 13, effective_from: '2026-11-01', status: 'scheduled' }),
      ]);

      expect(root().querySelectorAll('.timeline li')).toHaveLength(2);
      const notice = text();
      expect(notice).toContain('Rule 13 introduced');
      expect(notice).toContain('It applies from 2026-11-01, open-ended.');
      expect(notice).toContain('Rule 12 now ends on 2026-10-31.');
    });

    it('says so when no rule was in force on that date', async () => {
      await harness.navigateByUrl('/medicines/MED-0001');
      respond('MED-0001', medicineDetail(), []);

      introduce();
      live('/api/v1/medicines/MED-0001/rules').flush(
        { rule: rule({ id: 13, effective_to: '2026-12-01' }), superseded: null },
        { status: 201, statusText: 'Created' },
      );
      harness.detectChanges();
      respond('MED-0001', medicineDetail(), [rule({ id: 13, effective_to: '2026-12-01' })]);

      expect(text()).toContain('It applies from 2026-03-01, until 2026-11-30.');
      expect(text()).toContain('No earlier rule was in force on that date.');
    });

    it('does not carry the notice over to another medicine', async () => {
      await harness.navigateByUrl('/medicines/MED-0001');
      respond('MED-0001');
      introduce();
      live('/api/v1/medicines/MED-0001/rules').flush(
        { rule: rule({ id: 13 }), superseded: null },
        { status: 201, statusText: 'Created' },
      );
      harness.detectChanges();
      respond('MED-0001');
      expect(text()).toContain('Rule 13 introduced');

      const navigated = firstValueFrom(
        router.events.pipe(filter((e) => e instanceof NavigationEnd)),
      );
      void router.navigateByUrl('/medicines/MED-0002');
      await navigated;
      respond('MED-0002', medicineDetail({ code: 'MED-0002', name: 'Brufen' }));

      expect(text()).not.toContain('introduced');
    });
  });

  it('abandons the requests of the previous medicine when another one is opened', async () => {
    await harness.navigateByUrl('/medicines/MED-0001');
    const staleDetail = backend.expectOne('/api/v1/medicines/MED-0001');
    const staleRules = backend.expectOne('/api/v1/medicines/MED-0001/rules');

    const navigated = firstValueFrom(router.events.pipe(filter((e) => e instanceof NavigationEnd)));
    void router.navigateByUrl('/medicines/MED-0002');
    await navigated;
    respond('MED-0002', medicineDetail({ code: 'MED-0002', name: 'Brufen' }));

    expect(staleDetail.cancelled).toBe(true);
    expect(staleRules.cancelled).toBe(true);
    expect(text()).toContain('Brufen');
  });
});
