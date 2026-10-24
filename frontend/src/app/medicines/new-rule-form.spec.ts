import { provideHttpClient, withInterceptors } from '@angular/common/http';
import {
  HttpTestingController,
  TestRequest,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { apiErrorInterceptor } from '../api/error-interceptor';
import { NewRule, RuleSupersedeResult, Violation } from '../api/models';
import { problem, rule, violation } from '../../testing/fixtures';
import { NewRuleForm } from './new-rule-form';

describe('NewRuleForm', () => {
  let fixture: ComponentFixture<NewRuleForm>;
  let backend: HttpTestingController;
  let introduced: RuleSupersedeResult[];

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([apiErrorInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    backend = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(NewRuleForm);
    fixture.componentRef.setInput('code', 'MED-0001');
    introduced = [];
    fixture.componentInstance.introduced.subscribe((result) => introduced.push(result));
    fixture.detectChanges();
  });

  afterEach(() => backend.verify());

  const root = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const squash = (text: string | null | undefined): string =>
    (text ?? '').replace(/\s+/g, ' ').trim();
  const input = (name: string): HTMLInputElement =>
    root().querySelector<HTMLInputElement>(`#rule-${name}`)!;
  const button = (): HTMLButtonElement =>
    root().querySelector<HTMLButtonElement>('button[type=submit]')!;
  const alert = (): HTMLElement | null => root().querySelector<HTMLElement>('[role=alert]');
  const messagesUnder = (name: string): string[] =>
    Array.from(root().querySelectorAll(`#rule-${name}-errors li`)).map((li) =>
      squash(li.textContent),
    );

  function enter(name: string, value: string): void {
    input(name).value = value;
    input(name).dispatchEvent(new Event('input'));
  }

  function fillValid(): void {
    enter('effective_from', '2026-11-01');
    enter('max_quantity_per_dispense', '30');
    enter('max_quantity_per_30_days', '60');
  }

  function submit(): void {
    button().click();
    fixture.detectChanges();
  }

  const posted = (): TestRequest => backend.expectOne('/api/v1/medicines/MED-0001/rules');

  function rejected(
    request: TestRequest,
    status: number,
    code: string,
    violations: Violation[],
  ): void {
    request.flush(problem({ status, code, title: 'Problem', violations }), {
      status,
      statusText: 'Error',
    });
    fixture.detectChanges();
  }

  it('sends the new rule to the medicine it was opened on', () => {
    fillValid();
    input('requires_authorisation').click();

    submit();

    const request = posted();
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      effective_from: '2026-11-01',
      max_quantity_per_dispense: 30,
      max_quantity_per_30_days: 60,
      requires_authorisation: true,
    } satisfies NewRule);
  });

  it('sends requires_authorisation as false unless it was ticked', () => {
    fillValid();

    submit();

    expect((posted().request.body as NewRule).requires_authorisation).toBe(false);
  });

  it('does not send an incomplete form, and says what is missing', () => {
    submit();

    backend.expectNone('/api/v1/medicines/MED-0001/rules');
    expect(messagesUnder('effective_from')).toEqual(['Start date is required.']);
    expect(messagesUnder('max_quantity_per_dispense')).toEqual([
      'Maximum per dispense is required.',
    ]);
    expect(messagesUnder('max_quantity_per_30_days')).toEqual(['Maximum per 30 days is required.']);
  });

  it('rejects limits that are not whole numbers of at least 1 before sending', () => {
    fillValid();
    enter('max_quantity_per_dispense', '0');
    enter('max_quantity_per_30_days', '2.5');

    submit();

    backend.expectNone('/api/v1/medicines/MED-0001/rules');
    expect(messagesUnder('max_quantity_per_dispense')).toEqual([
      'Maximum per dispense must be a whole number of at least 1.',
    ]);
    expect(messagesUnder('max_quantity_per_30_days')).toEqual([
      'Maximum per 30 days must be a whole number of at least 1.',
    ]);
  });

  it('disables the button while saving and sends nothing more, even if submitted another way', () => {
    fillValid();
    submit();
    const request = posted();

    expect(button().disabled).toBe(true);
    submit();
    root().querySelector('form')!.dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    backend.expectNone('/api/v1/medicines/MED-0001/rules');
    request.flush({ rule: rule(), superseded: null }, { status: 201, statusText: 'Created' });
    fixture.detectChanges();

    expect(button().disabled).toBe(false);
  });

  it('reports the result to its parent and gets ready for the next rule', () => {
    const result: RuleSupersedeResult = {
      rule: rule({ id: 13, effective_from: '2026-11-01' }),
      superseded: rule({ id: 12, effective_to: '2026-11-01' }),
    };
    fillValid();
    input('requires_authorisation').click();
    submit();

    posted().flush(result, { status: 201, statusText: 'Created' });
    fixture.detectChanges();

    expect(introduced).toEqual([result]);
    for (const name of [
      'effective_from',
      'max_quantity_per_dispense',
      'max_quantity_per_30_days',
    ]) {
      expect(input(name).value, name).toBe('');
    }
    expect(input('requires_authorisation').checked).toBe(false);
    expect(alert()).toBeNull();
  });

  describe('when the new rule conflicts with the history', () => {
    const CONFLICTS: Violation[] = [
      violation({
        code: 'rule_already_starts_on_date',
        field: 'effective_from',
        message: 'A rule already starts on 2026-11-01.',
        context: { effective_from: '2026-11-01' },
      }),
      violation({
        code: 'rule_would_alter_recorded_dispenses',
        field: 'effective_from',
        message: '3 recorded dispenses would be governed by a different rule.',
        context: {
          dispense_count: 3,
          earliest_dispensed_at: '2026-11-02T08:00:00Z',
          latest_dispensed_at: '2026-11-05T10:30:00Z',
        },
      }),
    ];

    beforeEach(() => {
      fillValid();
      submit();
      rejected(posted(), 409, 'rule_conflict', CONFLICTS);
    });

    it('shows every conflict under the start date and in the summary with its rule code', () => {
      expect(messagesUnder('effective_from')).toEqual([
        'A rule already starts on 2026-11-01.',
        '3 recorded dispenses would be governed by a different rule.',
      ]);
      expect(input('effective_from').getAttribute('aria-invalid')).toBe('true');
      expect(input('effective_from').getAttribute('aria-describedby')).toContain(
        'rule-effective_from-errors',
      );
      const summary = Array.from(root().querySelectorAll('[role=alert] .violations li')).map(
        (li) => [
          squash(li.querySelector('.violation-message')?.textContent),
          squash(li.querySelector('.badge-code')?.textContent),
        ],
      );
      expect(summary).toEqual([
        ['A rule already starts on 2026-11-01.', 'rule_already_starts_on_date'],
        [
          '3 recorded dispenses would be governed by a different rule.',
          'rule_would_alter_recorded_dispenses',
        ],
      ]);
    });

    it('shows the instants in the context in SAST', () => {
      expect(squash(alert()?.textContent)).toContain('2026-11-02 10:00 SAST');
      expect(squash(alert()?.textContent)).toContain('2026-11-05 12:30 SAST');
    });

    it('leaves the other fields alone and clears the date when it is edited', () => {
      expect(input('max_quantity_per_dispense').getAttribute('aria-invalid')).toBeNull();

      enter('effective_from', '2026-12-01');
      fixture.detectChanges();

      expect(messagesUnder('effective_from')).toEqual([]);
      expect(input('effective_from').getAttribute('aria-invalid')).toBeNull();
    });

    it('can be submitted again as it is, and the old alert goes away while saving', () => {
      submit();

      expect(alert()).toBeNull();
      posted().flush({ rule: rule(), superseded: null }, { status: 201, statusText: 'Created' });
    });
  });

  it('maps validation errors of the limits to their fields', () => {
    fillValid();
    submit();

    rejected(posted(), 422, 'validation_error', [
      violation({
        code: 'out_of_range',
        field: 'max_quantity_per_30_days',
        message: 'Must be at most 1000000.',
        context: {},
      }),
    ]);

    expect(messagesUnder('max_quantity_per_30_days')).toEqual(['Must be at most 1000000.']);
    expect(messagesUnder('max_quantity_per_dispense')).toEqual([]);
  });

  it('explains a network failure in words', () => {
    fillValid();
    submit();

    posted().error(new ProgressEvent('error'));
    fixture.detectChanges();

    expect(squash(alert()?.textContent)).toContain('Cannot reach the server');
    expect(alert()?.textContent).not.toMatch(/ProgressEvent|\{/);
    expect(button().disabled).toBe(false);
  });
});
