import { provideHttpClient, withInterceptors } from '@angular/common/http';
import {
  HttpTestingController,
  TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';

import { apiErrorInterceptor } from '../api/error-interceptor';
import { DispenseRequest, MedicineOut, Page, Violation } from '../api/models';
import { MEDICINE, dispense, problem, violation } from '../../testing/fixtures';
import { DispenseForm, SUGGESTION_DEBOUNCE_MS } from './dispense-form';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

describe('DispenseForm', () => {
  let harness: RouterTestingHarness;
  let backend: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'dispense', component: DispenseForm }]),
        provideHttpClient(withInterceptors([apiErrorInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    backend = TestBed.inject(HttpTestingController);
    harness = await RouterTestingHarness.create('/dispense');
    harness.detectChanges();
  });

  afterEach(() => {
    backend.verify({ ignoreCancelled: true });
    vi.useRealTimers();
  });

  const root = (): HTMLElement => harness.routeNativeElement as HTMLElement;
  const field = (name: string): HTMLInputElement =>
    root().querySelector<HTMLInputElement>(`#field-${name}`)!;
  const submitButton = (): HTMLButtonElement =>
    root().querySelector<HTMLButtonElement>('button[type=submit]')!;
  const alert = (): HTMLElement | null => root().querySelector<HTMLElement>('[role=alert]');
  const squash = (text: string | null | undefined): string =>
    (text ?? '').replace(/\s+/g, ' ').trim();
  const messagesUnder = (name: string): string[] =>
    Array.from(root().querySelectorAll(`#field-${name}-errors li`)).map((li) =>
      squash(li.textContent),
    );
  /** Message and rule code of every violation in the form-level summary. */
  const summary = (): string[][] =>
    Array.from(root().querySelectorAll('[role=alert] .violations li')).map((li) => [
      squash(li.querySelector('.violation-message')?.textContent),
      squash(li.querySelector('.badge-code')?.textContent),
    ]);
  /** The label/value pairs inside one details element. */
  const contextOf = (details: Element | undefined): Record<string, string> =>
    Object.fromEntries(
      Array.from(details?.querySelectorAll('dl > div') ?? []).map((group) => [
        squash(group.querySelector('dt')?.textContent),
        squash(group.querySelector('dd')?.textContent),
      ]),
    );

  function enter(name: string, value: string): void {
    field(name).value = value;
    field(name).dispatchEvent(new Event('input'));
  }

  function fillValid(overrides: Record<string, string> = {}): void {
    const values = {
      medicine_code: 'MED-0001',
      patient_ref: 'PT-7K2M9Q',
      quantity: '20',
      ...overrides,
    };
    for (const [name, value] of Object.entries(values)) {
      enter(name, value);
    }
  }

  function submit(): void {
    submitButton().click();
    harness.detectChanges();
  }

  function posted(): TestRequest {
    return backend.expectOne((r) => r.url === '/api/v1/dispenses' && r.method === 'POST');
  }

  const bodyOf = (request: TestRequest): DispenseRequest => request.request.body as DispenseRequest;

  function created(request: TestRequest, headers: Record<string, string> = {}): void {
    request.flush(dispense(), { status: 201, statusText: 'Created', headers });
    harness.detectChanges();
  }

  function rejected(request: TestRequest, violations: Violation[] = [violation()]): void {
    request.flush(problem({ violations }), { status: 422, statusText: 'Unprocessable Entity' });
    harness.detectChanges();
  }

  function networkFailure(request: TestRequest): void {
    request.error(new ProgressEvent('error'));
    harness.detectChanges();
  }

  describe('what is sent', () => {
    it('sends the entered values, trimmed, with the time read as SAST and a fresh UUID key', () => {
      fillValid({
        medicine_code: ' MED-0001 ',
        dispensed_at: '2026-03-01T00:30',
        authorisation_ref: ' AUTH-1 ',
      });

      submit();

      expect(bodyOf(posted())).toEqual({
        idempotency_key: expect.stringMatching(UUID) as string,
        medicine_code: 'MED-0001',
        patient_ref: 'PT-7K2M9Q',
        quantity: 20,
        dispensed_at: '2026-02-28T22:30:00Z',
        authorisation_ref: 'AUTH-1',
      });
    });

    it('leaves out the optional fields when they are empty', () => {
      fillValid({ dispensed_at: '', authorisation_ref: '   ' });

      submit();

      const body = bodyOf(posted());
      expect(Object.keys(body).sort()).toEqual([
        'idempotency_key',
        'medicine_code',
        'patient_ref',
        'quantity',
      ]);
    });

    it('prefills the medicine from the link on the medicine page, without asking for suggestions', async () => {
      await harness.navigateByUrl('/dispense?medicine_code=MED-0042', DispenseForm);
      harness.detectChanges();

      expect(field('medicine_code').value).toBe('MED-0042');
      backend.expectNone((r) => r.url === '/api/v1/medicines');
    });
  });

  describe('checks before sending', () => {
    it('does not send an empty form, says what is missing and moves focus to the first problem', () => {
      submit();

      backend.expectNone('/api/v1/dispenses');
      expect(messagesUnder('medicine_code')).toEqual(['Medicine code is required.']);
      expect(messagesUnder('patient_ref')).toEqual(['Patient reference is required.']);
      expect(messagesUnder('quantity')).toEqual(['Quantity is required.']);
      expect(field('medicine_code').getAttribute('aria-invalid')).toBe('true');
      expect(document.activeElement).toBe(field('medicine_code'));
    });

    it('does not show a field as wrong before the user has been to it', () => {
      expect(messagesUnder('quantity')).toEqual([]);
      expect(field('quantity').getAttribute('aria-invalid')).toBeNull();
    });

    it.each(['0', '-5', '2.5'])('rejects a quantity of %s', (quantity) => {
      fillValid({ quantity });

      submit();

      backend.expectNone('/api/v1/dispenses');
      expect(messagesUnder('quantity')).toEqual(['Quantity must be a whole number of at least 1.']);
    });

    it('treats whitespace as empty', () => {
      fillValid({ patient_ref: '   ' });

      submit();

      backend.expectNone('/api/v1/dispenses');
      expect(messagesUnder('patient_ref')).toEqual(['Patient reference is required.']);
    });
  });

  describe('while a request is in flight', () => {
    it('disables the button and sends nothing more, even if the form is submitted another way', () => {
      fillValid();
      submit();
      const request = posted();

      expect(submitButton().disabled).toBe(true);
      expect(squash(submitButton().textContent)).toBe('Recording…');
      submit();
      root().querySelector('form')!.dispatchEvent(new Event('submit'));
      harness.detectChanges();
      backend.expectNone('/api/v1/dispenses');

      created(request);

      expect(submitButton().disabled).toBe(false);
      expect(squash(submitButton().textContent)).toBe('Record dispense');
    });

    it('enables the button again after a failure', () => {
      fillValid();
      submit();

      networkFailure(posted());

      expect(submitButton().disabled).toBe(false);
    });
  });

  describe('when the dispense is recorded', () => {
    it('shows what was created, links to the ledger and gets the form ready for the next dispense', () => {
      fillValid({ authorisation_ref: 'AUTH-1' });
      submit();

      posted().flush(
        dispense({ id: 981, dispensed_at: '2026-02-28T22:30:00Z', quantity: 20, rule_id: 12 }),
        { status: 201, statusText: 'Created' },
      );
      harness.detectChanges();

      const banner = squash(root().querySelector('.alert-success')?.textContent);
      expect(banner).toContain('Dispense recorded');
      expect(banner).toContain('#981');
      expect(banner).toContain('Amoxil (MED-0001)');
      expect(banner).toContain('20');
      expect(banner).toContain('2026-03-01 00:30 SAST');
      expect(banner).toContain('#12');
      expect(banner).not.toContain('Already recorded');
      const link = root().querySelector<HTMLAnchorElement>('.alert-success a');
      expect(link?.getAttribute('href')).toBe('/ledger?patient_ref=PT-7K2M9Q');
      for (const name of [
        'medicine_code',
        'patient_ref',
        'quantity',
        'dispensed_at',
        'authorisation_ref',
      ]) {
        expect(field(name).value, name).toBe('');
      }
      expect(root().querySelector('[aria-invalid=true]')).toBeNull();
      expect(alert()).toBeNull();
    });

    it('says so when the server recognised the request and returned the original outcome', () => {
      fillValid();
      submit();

      created(posted(), { 'Idempotency-Replayed': 'true' });

      const banner = squash(root().querySelector('.alert-success')?.textContent);
      expect(banner).toContain('Already recorded');
      expect(banner).toContain('Nothing new was created');
    });
  });

  describe('when the dispense is rejected', () => {
    const REJECTIONS: Violation[] = [
      violation({
        code: 'medicine_inactive',
        field: 'medicine_code',
        message: 'Medicine MED-0001 is inactive.',
        context: {},
      }),
      violation({
        code: 'max_quantity_per_dispense_exceeded',
        field: 'quantity',
        message: 'Quantity 150 exceeds the maximum of 100 units per dispense.',
        context: { limit: 100, requested: 150, rule_id: 7 },
      }),
      violation({
        code: 'max_quantity_per_30_days_exceeded',
        field: 'quantity',
        message: 'Quantity 150 would bring the 30-day total to 190, above the limit of 120.',
        context: { limit: 120, already_dispensed: 40, requested: 150, rule_id: 7 },
      }),
      violation({
        code: 'authorisation_required',
        field: 'authorisation_ref',
        message: 'An authorisation reference is required.',
        context: { rule_id: 7 },
      }),
      violation({
        code: 'general_problem',
        field: null,
        message: 'Something not tied to a field.',
        context: {},
      }),
      violation({
        code: 'bad_key',
        field: 'idempotency_key',
        message: 'The key is not acceptable.',
        context: {},
      }),
    ];

    beforeEach(() => {
      fillValid({ quantity: '150' });
      submit();
      rejected(posted(), REJECTIONS);
    });

    it('lists every violation in the summary with its rule code, mapped to a field or not', () => {
      expect(summary()).toEqual([
        ['Medicine MED-0001 is inactive.', 'medicine_inactive'],
        [
          'Quantity 150 exceeds the maximum of 100 units per dispense.',
          'max_quantity_per_dispense_exceeded',
        ],
        [
          'Quantity 150 would bring the 30-day total to 190, above the limit of 120.',
          'max_quantity_per_30_days_exceeded',
        ],
        ['An authorisation reference is required.', 'authorisation_required'],
        ['Something not tied to a field.', 'general_problem'],
        ['The key is not acceptable.', 'bad_key'],
      ]);
      expect(squash(alert()?.textContent)).toContain('Dispense rejected');
    });

    it('shows every message of a field under that field, and marks it invalid for assistive technology', () => {
      expect(messagesUnder('quantity')).toEqual([
        'Quantity 150 exceeds the maximum of 100 units per dispense.',
        'Quantity 150 would bring the 30-day total to 190, above the limit of 120.',
      ]);
      expect(messagesUnder('medicine_code')).toEqual(['Medicine MED-0001 is inactive.']);
      expect(messagesUnder('authorisation_ref')).toEqual([
        'An authorisation reference is required.',
      ]);
      for (const name of ['quantity', 'medicine_code', 'authorisation_ref']) {
        expect(field(name).getAttribute('aria-invalid'), name).toBe('true');
        expect(field(name).getAttribute('aria-describedby'), name).toContain(
          `field-${name}-errors`,
        );
      }
    });

    it('leaves fields that nothing was said about alone, and shows unmapped violations only in the summary', () => {
      expect(messagesUnder('patient_ref')).toEqual([]);
      expect(messagesUnder('dispensed_at')).toEqual([]);
      expect(field('patient_ref').getAttribute('aria-invalid')).toBeNull();
      const underFields = [
        'medicine_code',
        'patient_ref',
        'quantity',
        'dispensed_at',
        'authorisation_ref',
      ]
        .flatMap(messagesUnder)
        .join(' ');
      expect(underFields).not.toContain('Something not tied to a field.');
      expect(underFields).not.toContain('The key is not acceptable.');
    });

    it('keeps the machine-readable context in a collapsed details element, never raw JSON', () => {
      // Violations with an empty context (inactive medicine, the two others) get no details element.
      const details = Array.from(
        root().querySelectorAll<HTMLDetailsElement>('[role=alert] details'),
      );

      expect(details).toHaveLength(3);
      expect(details.every((element) => !element.open)).toBe(true);
      expect(contextOf(details[0])).toEqual({ limit: '100', requested: '150', 'rule id': '7' });
      expect(contextOf(details[1])).toEqual({
        limit: '120',
        'already dispensed': '40',
        requested: '150',
        'rule id': '7',
      });
      expect(contextOf(details[2])).toEqual({ 'rule id': '7' });
      expect(alert()?.textContent).not.toMatch(/[{}[]]|"limit"/);
    });

    it('clears a field when the user edits it, and only that field', () => {
      enter('quantity', '50');
      harness.detectChanges();

      expect(messagesUnder('quantity')).toEqual([]);
      expect(field('quantity').getAttribute('aria-invalid')).toBeNull();
      expect(messagesUnder('medicine_code')).toHaveLength(1);
      expect(field('medicine_code').getAttribute('aria-invalid')).toBe('true');
    });

    it('can be submitted again as it is, since a rejection does not block the form', () => {
      submit();

      const again = posted();
      expect(bodyOf(again).quantity).toBe(150);
      created(again);
    });
  });

  describe('when something goes wrong without a decision', () => {
    it('explains it in words and says that trying again is safe', () => {
      fillValid();
      submit();

      networkFailure(posted());

      const text = squash(alert()?.textContent);
      expect(text).toContain('Cannot reach the server');
      expect(text).toContain('You can submit again');
      expect(text).not.toMatch(/ProgressEvent|\{|status 0/);
    });

    it('never shows the body of an error page from a proxy', () => {
      fillValid();
      submit();

      posted().flush('<html><body>502 Bad Gateway nginx</body></html>', {
        status: 502,
        statusText: 'Bad Gateway',
      });
      harness.detectChanges();

      expect(squash(alert()?.textContent)).toContain('Service unavailable');
      expect(alert()?.textContent).not.toMatch(/nginx|<html/);
    });
  });

  describe('the idempotency key across attempts', () => {
    it('is reused to retry after a failure without a response, so a retry can never double-record', () => {
      fillValid();
      submit();
      const first = posted();
      const key = bodyOf(first).idempotency_key;
      networkFailure(first);

      submit();
      const retry = posted();

      expect(bodyOf(retry).idempotency_key).toBe(key);
      created(retry, { 'Idempotency-Replayed': 'true' });
    });

    it('is reused after a 503 busy answer, which settled nothing', () => {
      fillValid();
      submit();
      const first = posted();
      first.flush(
        problem({ status: 503, code: 'resource_busy', title: 'Resource busy', violations: [] }),
        { status: 503, statusText: 'Service Unavailable' },
      );
      harness.detectChanges();

      submit();

      expect(bodyOf(posted()).idempotency_key).toBe(bodyOf(first).idempotency_key);
    });

    it('is replaced when the payload changes after a failure', () => {
      fillValid();
      submit();
      const first = posted();
      networkFailure(first);

      enter('quantity', '25');
      submit();

      expect(bodyOf(posted()).idempotency_key).not.toBe(bodyOf(first).idempotency_key);
    });

    it('is replaced after a rejection, even for an identical retry', () => {
      fillValid({ quantity: '150' });
      submit();
      const first = posted();
      rejected(first);

      submit();

      expect(bodyOf(posted()).idempotency_key).not.toBe(bodyOf(first).idempotency_key);
    });

    it('is replaced after a success, for the next dispense', () => {
      fillValid();
      submit();
      const first = posted();
      created(first);

      fillValid();
      submit();

      expect(bodyOf(posted()).idempotency_key).not.toBe(bodyOf(first).idempotency_key);
    });
  });

  describe('suggestions for the medicine code', () => {
    function suggestionCodes(): string[] {
      return Array.from(
        root().querySelectorAll<HTMLOptionElement>('#medicine-suggestions option'),
      ).map((option) => option.value);
    }

    const lookup = (): TestRequest => backend.expectOne((r) => r.url === '/api/v1/medicines');

    function medicines(...codes: string[]): Page<MedicineOut> {
      return { items: codes.map((code) => ({ ...MEDICINE, code })), next_cursor: null };
    }

    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    });

    it('asks for at most 8 matches once typing pauses, and offers them in the datalist', () => {
      enter('medicine_code', 'amo');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS - 1);
      backend.expectNone((r) => r.url === '/api/v1/medicines');
      vi.advanceTimersByTime(1);

      const request = lookup();
      expect(request.request.params.get('q')).toBe('amo');
      expect(request.request.params.get('limit')).toBe('8');
      request.flush(medicines('MED-0001', 'MED-0002'));
      harness.detectChanges();

      expect(suggestionCodes()).toEqual(['MED-0001', 'MED-0002']);
    });

    it('cancels a lookup that a newer keystroke has made obsolete', () => {
      enter('medicine_code', 'am');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS);
      const stale = lookup();

      enter('medicine_code', 'amo');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS);
      const current = lookup();
      current.flush(medicines('MED-0007'));
      harness.detectChanges();

      expect(stale.cancelled).toBe(true);
      expect(suggestionCodes()).toEqual(['MED-0007']);
    });

    it('does not ask again for the same text', () => {
      enter('medicine_code', 'amo');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS);
      lookup().flush(medicines('MED-0001'));

      enter('medicine_code', ' amo ');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS);

      backend.expectNone((r) => r.url === '/api/v1/medicines');
    });

    it('clears the suggestions without a request when the field is emptied', () => {
      enter('medicine_code', 'amo');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS);
      lookup().flush(medicines('MED-0001'));
      harness.detectChanges();

      enter('medicine_code', '');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS);
      harness.detectChanges();

      backend.expectNone((r) => r.url === '/api/v1/medicines');
      expect(suggestionCodes()).toEqual([]);
    });

    it('stays quiet when the lookup fails: suggestions are only a convenience', () => {
      enter('medicine_code', 'amo');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS);

      lookup().flush('<html>Bad Gateway</html>', { status: 502, statusText: 'Bad Gateway' });
      harness.detectChanges();

      expect(suggestionCodes()).toEqual([]);
      expect(alert()).toBeNull();
    });

    it('labels inactive medicines so the user knows before submitting', () => {
      enter('medicine_code', 'amo');
      vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS);
      lookup().flush({ items: [{ ...MEDICINE, is_active: false }], next_cursor: null });
      harness.detectChanges();

      const option = root().querySelector('#medicine-suggestions option');
      expect(squash(option?.textContent)).toContain('inactive');
    });
  });
});
