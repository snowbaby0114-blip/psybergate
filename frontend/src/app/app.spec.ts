import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';

import { App } from './app';

@Component({ template: 'page' })
class Page {}

describe('App', () => {
  async function open(url: string): Promise<HTMLElement> {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'medicines', component: Page },
          { path: 'medicines/:code', component: Page },
          { path: 'dispense', component: Page },
          { path: 'ledger', component: Page },
        ]),
      ],
    });
    const fixture = TestBed.createComponent(App);
    fixture.autoDetectChanges();
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl(url);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const links = (root: HTMLElement): Record<string, string | null> =>
    Object.fromEntries(
      Array.from(root.querySelectorAll('nav[aria-label=Main] a')).map((a) => [
        a.textContent?.trim() ?? '',
        a.getAttribute('aria-current'),
      ]),
    );

  it('offers the four views in a labelled navigation', async () => {
    const root = await open('/medicines');

    expect(Object.keys(links(root))).toEqual(['Medicines', 'Dispense', 'Patient ledger']);
    expect(root.querySelector('.brand')?.getAttribute('href')).toBe('/medicines');
    expect(root.querySelector('main')).not.toBeNull();
  });

  it.each([
    ['/medicines', 'Medicines'],
    ['/medicines/MED-0001', 'Medicines'],
    ['/dispense', 'Dispense'],
    ['/ledger?patient_ref=PT-1', 'Patient ledger'],
  ])('marks only the current section as the current page on %s', async (url, current) => {
    const root = await open(url);

    const marked = Object.entries(links(root))
      .filter(([, value]) => value === 'page')
      .map(([label]) => label);
    expect(marked).toEqual([current]);
  });
});
