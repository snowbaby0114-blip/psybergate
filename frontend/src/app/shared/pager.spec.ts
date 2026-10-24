import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { Pager } from './pager';

@Component({
  imports: [Pager],
  template: `<app-pager
    [pageNumber]="page()"
    [hasPrevious]="hasPrevious"
    [hasNext]="hasNext"
    (previous)="events.push('previous')"
    (next)="events.push('next')"
  />`,
})
class Host {
  readonly page = signal(1);
  hasPrevious = false;
  hasNext = false;
  events: string[] = [];
}

describe('Pager', () => {
  function render(state: { page?: number; hasPrevious?: boolean; hasNext?: boolean } = {}) {
    const fixture = TestBed.createComponent(Host);
    fixture.componentInstance.page.set(state.page ?? 1);
    fixture.componentInstance.hasPrevious = state.hasPrevious ?? false;
    fixture.componentInstance.hasNext = state.hasNext ?? false;
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const button = (label: string) =>
      Array.from(root.querySelectorAll('button')).find((b) => b.textContent?.trim() === label)!;
    return { fixture, root, button, events: fixture.componentInstance.events };
  }

  it('shows the page number in a polite live region, so a page change is announced', () => {
    const { fixture, root } = render({ page: 3 });

    const indicator = root.querySelector('.pager-page')!;
    expect(indicator.textContent?.trim()).toBe('Page 3');
    expect(indicator.getAttribute('aria-live')).toBe('polite');
    expect(indicator.getAttribute('aria-atomic')).toBe('true');

    fixture.componentInstance.page.set(4);
    fixture.detectChanges();
    expect(root.querySelector('.pager-page')).toBe(indicator);
    expect(indicator.textContent?.trim()).toBe('Page 4');
  });

  it('is a labelled navigation landmark made of real buttons', () => {
    const { root } = render();

    expect(root.querySelector('nav')?.getAttribute('aria-label')).toBe('Pagination');
    expect(root.querySelectorAll('button[type=button]')).toHaveLength(2);
  });

  it.each([
    [{ hasPrevious: false, hasNext: false }, true, true],
    [{ hasPrevious: true, hasNext: false }, false, true],
    [{ hasPrevious: false, hasNext: true }, true, false],
    [{ hasPrevious: true, hasNext: true }, false, false],
  ])('disables a button exactly when there is nowhere to go: %j', (state, previousOff, nextOff) => {
    const { button } = render(state);

    expect(button('Previous').disabled).toBe(previousOff);
    expect(button('Next').disabled).toBe(nextOff);
  });

  it('emits previous and next when the enabled buttons are used', () => {
    const { button, events } = render({ hasPrevious: true, hasNext: true });

    button('Next').click();
    button('Previous').click();

    expect(events).toEqual(['next', 'previous']);
  });

  it('emits nothing from a disabled button', () => {
    const { button, events } = render();

    button('Next').click();
    button('Previous').click();

    expect(events).toEqual([]);
  });
});
