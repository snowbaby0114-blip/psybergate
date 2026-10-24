import { TestBed } from '@angular/core/testing';

import { RuleOut } from '../api/models';
import { rule } from '../../testing/fixtures';
import { RuleTimeline } from './rule-timeline';

function render(rules: RuleOut[]): HTMLElement {
  const fixture = TestBed.createComponent(RuleTimeline);
  fixture.componentRef.setInput('rules', rules);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

/** The dt/dd pairs of one rule, e.g. { 'Max per dispense': '30' }. */
function facts(item: Element | undefined): Record<string, string> {
  return Object.fromEntries(
    Array.from(item?.querySelectorAll('dl > div') ?? []).map((group) => [
      group.querySelector('dt')?.textContent?.trim() ?? '',
      group.querySelector('dd')?.textContent?.trim() ?? '',
    ]),
  );
}

function items(element: HTMLElement): string[] {
  return Array.from(element.querySelectorAll('li')).map((li) =>
    (li.textContent ?? '').replace(/\s+/g, ' ').trim(),
  );
}

describe('RuleTimeline', () => {
  it('lists the rules oldest first, whatever order they arrive in', () => {
    const element = render([
      rule({ id: 3, effective_from: '2026-09-01', status: 'scheduled' }),
      rule({ id: 1, effective_from: '2025-01-01', effective_to: '2025-07-01', status: 'expired' }),
      rule({ id: 2, effective_from: '2025-07-01', effective_to: '2026-09-01' }),
    ]);

    const ids = items(element).map((text) => /Rule (\d+)/.exec(text)?.[1]);

    expect(ids).toEqual(['1', '2', '3']);
  });

  it('shows the exclusive end as the last day the rule applies, or open-ended', () => {
    const element = render([
      rule({ id: 1, effective_from: '2026-01-01', effective_to: '2026-03-01', status: 'expired' }),
      rule({ id: 2, effective_from: '2026-03-01', effective_to: null }),
    ]);

    const [closed, open] = items(element);

    expect(closed).toContain('From 2026-01-01 · until 2026-02-28');
    expect(closed).not.toContain('open-ended');
    expect(open).toContain('From 2026-03-01 · open-ended');
    expect(open).not.toContain('until');
  });

  it('handles an end date right after a leap day', () => {
    const element = render([
      rule({ effective_from: '2028-01-01', effective_to: '2028-03-01', status: 'expired' }),
    ]);

    expect(items(element)[0]).toContain('until 2028-02-29');
  });

  it('takes the badge from the server status and does not compare dates with the browser clock', () => {
    const element = render([
      // Dates in the distant past, yet the server says it is in force: the server decides.
      rule({ id: 1, effective_from: '2001-01-01', effective_to: '2002-01-01', status: 'in_force' }),
      rule({
        id: 2,
        effective_from: '2003-01-01',
        effective_to: '2004-01-01',
        status: 'scheduled',
      }),
      rule({ id: 3, effective_from: '2090-01-01', effective_to: null, status: 'expired' }),
    ]);

    const badges = Array.from(element.querySelectorAll('.badge')).map((badge) =>
      badge.textContent?.trim(),
    );

    expect(badges).toEqual(['In force', 'Scheduled', 'Expired']);
  });

  it('shows the limits and whether authorisation is required', () => {
    const element = render([
      rule({
        id: 1,
        effective_from: '2026-01-01',
        effective_to: '2026-03-01',
        max_quantity_per_dispense: 30,
        max_quantity_per_30_days: 90,
        requires_authorisation: true,
        status: 'expired',
      }),
      rule({
        id: 2,
        max_quantity_per_dispense: 60,
        max_quantity_per_30_days: 120,
        requires_authorisation: false,
      }),
    ]);

    const [first, second] = Array.from(element.querySelectorAll('li'));

    expect(facts(first)).toEqual({
      'Max per dispense': '30',
      'Max per 30 days': '90',
      Authorisation: 'Required',
    });
    expect(facts(second)).toEqual({
      'Max per dispense': '60',
      'Max per 30 days': '120',
      Authorisation: 'Not required',
    });
  });

  it('shows when a rule was recorded in SAST', () => {
    const element = render([rule({ created_at: '2026-02-20T09:15:00Z' })]);

    expect(items(element)[0]).toContain('recorded 2026-02-20 11:15 SAST');
  });

  it('says so when there are no rules', () => {
    const element = render([]);

    expect(element.textContent).toContain('No rules have been defined for this medicine.');
    expect(element.querySelector('ol')).toBeNull();
  });
});
