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
import { MedicineOut, Page } from '../api/models';
import { MEDICINE, problem } from '../../testing/fixtures';
import { MedicineSearch, TYPING_DEBOUNCE_MS } from './medicine-search';

function page(names: string[], nextCursor: string | null): Page<MedicineOut> {
  return {
    items: names.map((name, index) => ({ ...MEDICINE, code: `MED-${index + 1}`, name })),
    next_cursor: nextCursor,
  };
}

function paramsOf(request: TestRequest): Record<string, string | null> {
  const { params } = request.request;
  return Object.fromEntries(params.keys().map((key) => [key, params.get(key)]));
}

describe('MedicineSearch', () => {
  let harness: RouterTestingHarness;
  let backend: HttpTestingController;
  let router: Router;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'medicines', component: MedicineSearch }]),
        provideHttpClient(withInterceptors([apiErrorInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    backend = TestBed.inject(HttpTestingController);
    router = TestBed.inject(Router);
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => {
    backend.verify();
    vi.useRealTimers();
  });

  const root = (): HTMLElement => harness.routeNativeElement as HTMLElement;
  const text = (selector: string): string =>
    (root().querySelector(selector)?.textContent ?? '').replace(/\s+/g, ' ').trim();
  const box = (): HTMLInputElement => root().querySelector<HTMLInputElement>('#medicine-search')!;
  const button = (label: string): HTMLButtonElement =>
    Array.from(root().querySelectorAll('button')).find((b) => b.textContent?.trim() === label)!;
  const codes = (): string[] =>
    Array.from(root().querySelectorAll('tbody tr td:first-child')).map((cell) =>
      (cell.textContent ?? '').trim(),
    );

  /** Runs something that navigates and waits until the router is done. */
  async function navigating(action: () => void): Promise<void> {
    const done = firstValueFrom(router.events.pipe(filter((e) => e instanceof NavigationEnd)));
    action();
    await done;
    harness.detectChanges();
  }

  function type(value: string): void {
    box().value = value;
    box().dispatchEvent(new Event('input'));
  }

  function search(): TestRequest {
    return backend.expectOne((r) => r.url === '/api/v1/medicines');
  }

  async function open(url: string, first: Page<MedicineOut>): Promise<void> {
    await harness.navigateByUrl(url);
    search().flush(first);
    harness.detectChanges();
  }

  describe('loading from the URL', () => {
    it('shows loading, then the rows, the count and the pager on the first page', async () => {
      await harness.navigateByUrl('/medicines');
      harness.detectChanges();
      const request = search();
      expect(text('[role=status]')).toBe('Loading medicines…');
      expect(paramsOf(request)).toEqual({});

      request.flush(page(['Amoxil', 'Aspirin'], 'C1'));
      harness.detectChanges();

      expect(codes()).toEqual(['MED-1', 'MED-2']);
      expect(text('[role=status]')).toBe('2 medicines on this page');
      expect(text('.pager-page')).toBe('Page 1');
      expect(button('Previous').disabled).toBe(true);
      expect(button('Next').disabled).toBe(false);
    });

    it('reads term, cursor and trail from the URL, as after a reload or a shared link', async () => {
      await harness.navigateByUrl('/medicines?q=amox&cursor=C2&trail=C1');
      harness.detectChanges();

      const request = search();
      expect(paramsOf(request)).toEqual({ q: 'amox', cursor: 'C2' });
      request.flush(page(['Amoxil'], null));
      harness.detectChanges();

      expect(box().value).toBe('amox');
      expect(text('.pager-page')).toBe('Page 3');
      expect(button('Previous').disabled).toBe(false);
      expect(button('Next').disabled).toBe(true);
      expect(text('[role=status]')).toBe('1 medicine on this page');
    });

    it('says what was searched for when nothing matches', async () => {
      await open('/medicines?q=zzz', page([], null));

      expect(text('[role=status]')).toBe('No medicines match "zzz".');
      expect(root().querySelector('table')).toBeNull();
    });

    it('does not invent a search term for an empty catalogue', async () => {
      await open('/medicines', page([], null));

      expect(text('[role=status]')).toBe('There are no medicines to show.');
    });

    it('marks inactive medicines', async () => {
      await harness.navigateByUrl('/medicines');
      search().flush({
        items: [{ ...MEDICINE, is_active: false }],
        next_cursor: null,
      } satisfies Page<MedicineOut>);
      harness.detectChanges();

      expect(text('tbody')).toContain('Inactive');
    });
  });

  describe('paging', () => {
    it('walks forward and back through three pages with the right requests and page numbers', async () => {
      await open('/medicines?q=a', page(['A'], 'C1'));

      await navigating(() => button('Next').click());
      expect(router.url).toBe('/medicines?q=a&cursor=C1');
      search().flush(page(['B'], 'C2'));
      harness.detectChanges();
      expect(text('.pager-page')).toBe('Page 2');

      await navigating(() => button('Next').click());
      expect(router.url).toBe('/medicines?q=a&cursor=C2&trail=C1');
      const third = search();
      expect(paramsOf(third)).toEqual({ q: 'a', cursor: 'C2' });
      third.flush(page(['C'], null));
      harness.detectChanges();
      expect(text('.pager-page')).toBe('Page 3');
      expect(button('Next').disabled).toBe(true);

      await navigating(() => button('Previous').click());
      expect(router.url).toBe('/medicines?q=a&cursor=C1');
      expect(paramsOf(search())).toEqual({ q: 'a', cursor: 'C1' });
    });

    it('keeps every earlier cursor in the URL, oldest first, however deep the walk goes', async () => {
      await open('/medicines', page(['A'], 'C1'));

      for (const [next, expected] of [
        ['C2', '/medicines?cursor=C1'],
        ['C3', '/medicines?cursor=C2&trail=C1'],
        [null, '/medicines?cursor=C3&trail=C1&trail=C2'],
      ] as const) {
        await navigating(() => button('Next').click());
        expect(router.url).toBe(expected);
        search().flush(page(['X'], next));
        harness.detectChanges();
      }
      expect(text('.pager-page')).toBe('Page 4');

      for (const expected of [
        '/medicines?cursor=C2&trail=C1',
        '/medicines?cursor=C1',
        '/medicines',
      ]) {
        await navigating(() => button('Previous').click());
        expect(router.url).toBe(expected);
        search().flush(page(['X'], 'more'));
        harness.detectChanges();
      }
      expect(text('.pager-page')).toBe('Page 1');
    });

    it('reproduces a deep page from its URL alone, as after a reload or a shared link', async () => {
      await harness.navigateByUrl('/medicines?cursor=C3&trail=C1&trail=C2');
      harness.detectChanges();

      const request = search();
      expect(paramsOf(request)).toEqual({ cursor: 'C3' });
      request.flush(page(['X'], null));
      harness.detectChanges();
      expect(text('.pager-page')).toBe('Page 4');

      await navigating(() => button('Previous').click());
      expect(router.url).toBe('/medicines?cursor=C2&trail=C1');
      expect(paramsOf(search())).toEqual({ cursor: 'C2' });
    });

    it('goes back to a clean first-page URL from page 2', async () => {
      await open('/medicines?cursor=C1', page(['B'], null));

      await navigating(() => button('Previous').click());

      expect(router.url).toBe('/medicines');
      expect(paramsOf(search())).toEqual({});
    });

    it('lets the browser history drive the page, since the URL is the only state', async () => {
      await open('/medicines?q=a', page(['A'], 'C1'));
      await navigating(() => button('Next').click());
      search().flush(page(['B'], null));
      harness.detectChanges();

      await navigating(() => void router.navigateByUrl('/medicines?q=a'));

      expect(paramsOf(search())).toEqual({ q: 'a' });
      expect(text('.pager-page')).toBe('Page 1');
    });
  });

  describe('searching', () => {
    it('submitting applies the term at once and starts again from the first page', async () => {
      await open('/medicines?q=old&cursor=C2&trail=C1', page(['X'], null));

      type('amox');
      await navigating(() => root().querySelector('form')!.dispatchEvent(new Event('submit')));

      expect(router.url).toBe('/medicines?q=amox');
      expect(paramsOf(search())).toEqual({ q: 'amox' });
    });

    it('leaving the box applies the term at once', async () => {
      await open('/medicines', page(['X'], null));

      type('amox');
      await navigating(() => box().dispatchEvent(new Event('blur')));

      expect(paramsOf(search())).toEqual({ q: 'amox' });
    });

    it('waits for typing to pause, then searches once for the final text', async () => {
      await open('/medicines', page(['X'], null));
      vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });

      type('a');
      vi.advanceTimersByTime(TYPING_DEBOUNCE_MS - 1);
      type('am');
      vi.advanceTimersByTime(TYPING_DEBOUNCE_MS - 1);
      type('amo');
      vi.advanceTimersByTime(TYPING_DEBOUNCE_MS - 1);
      backend.expectNone((r) => r.url === '/api/v1/medicines');

      await navigating(() => vi.advanceTimersByTime(1));

      expect(router.url).toBe('/medicines?q=amo');
      expect(paramsOf(search())).toEqual({ q: 'amo' });
    });

    it('trims the term and treats a blank box as no filter', async () => {
      await open('/medicines?q=amox', page(['X'], null));

      type('   ');
      await navigating(() => box().dispatchEvent(new Event('blur')));

      expect(router.url).toBe('/medicines');
      expect(paramsOf(search())).toEqual({});
    });

    it('does not search again when the box is left without changing the term', async () => {
      await open('/medicines?q=amox', page(['X'], null));

      box().dispatchEvent(new Event('blur'));
      await harness.fixture.whenStable();

      backend.expectNone((r) => r.url === '/api/v1/medicines');
    });

    it('reloads the page when the same term is submitted again, so Search never seems to do nothing', async () => {
      await open('/medicines?q=amox&cursor=C1', page(['Old'], null));

      root().querySelector('form')!.dispatchEvent(new Event('submit'));
      harness.detectChanges();
      const reload = search();
      expect(paramsOf(reload)).toEqual({ q: 'amox', cursor: 'C1' });
      expect(router.url).toBe('/medicines?q=amox&cursor=C1');
      reload.flush(page(['New'], null));
      harness.detectChanges();

      expect(text('tbody')).toContain('New');
    });

    it('does not reload when the URL changes without changing the request', async () => {
      await open('/medicines?q=amox', page(['X'], null));

      await navigating(() => void router.navigateByUrl('/medicines?q=amox%20'));

      backend.expectNone((r) => r.url === '/api/v1/medicines');
      expect(text('[role=status]')).toBe('1 medicine on this page');
    });

    it('cancels the request in flight when the URL changes, and shows only the newer answer', async () => {
      await harness.navigateByUrl('/medicines?q=a');
      const stale = backend.expectOne((r) => r.params.get('q') === 'a');

      await navigating(() => void router.navigateByUrl('/medicines?q=b'));
      const current = backend.expectOne((r) => r.params.get('q') === 'b');
      current.flush(page(['Bravo'], null));
      harness.detectChanges();

      expect(stale.cancelled).toBe(true);
      expect(text('tbody')).toContain('Bravo');
      expect(text('[role=status]')).toBe('1 medicine on this page');
    });
  });

  describe('keeping the box and the URL in step', () => {
    it('copies a term that arrives from outside the box, such as back/forward, into the box', async () => {
      await open('/medicines?q=old', page(['X'], null));

      await navigating(() => void router.navigateByUrl('/medicines?q=other'));
      search().flush(page(['Y'], null));
      harness.detectChanges();

      expect(box().value).toBe('other');
    });

    it('does not overwrite characters typed while its own navigation was still in flight', async () => {
      await open('/medicines', page(['X'], null));
      vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });

      type('am');
      const echoed = navigating(() => vi.advanceTimersByTime(TYPING_DEBOUNCE_MS));
      type('ami');
      await echoed;

      expect(router.url).toBe('/medicines?q=am');
      expect(box().value).toBe('ami');
      search().flush(page(['Amoxil'], null));
    });
  });

  describe('failure', () => {
    it('shows what went wrong in words with a reference and retries on demand', async () => {
      await harness.navigateByUrl('/medicines');
      search().flush(
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

      expect(text('[role=alert]')).toContain('Resource busy');
      expect(text('[role=alert]')).toContain('The system is busy. Try again.');
      expect(text('[role=alert]')).toContain('req-9f0c1c5e');
      expect(root().querySelector('table')).toBeNull();

      button('Retry').click();
      search().flush(page(['Amoxil'], null));
      harness.detectChanges();

      expect(root().querySelector('[role=alert]')).toBeNull();
      expect(codes()).toEqual(['MED-1']);
    });

    it('never shows a proxy error page to the user', async () => {
      await harness.navigateByUrl('/medicines');
      search().flush('<html><body>502 Bad Gateway nginx</body></html>', {
        status: 502,
        statusText: 'Bad Gateway',
      });
      harness.detectChanges();

      expect(text('[role=alert]')).toContain('Service unavailable');
      expect(text('[role=alert]')).not.toMatch(/nginx|<html/);
    });

    it('offers a way back to the first page when a deep page fails', async () => {
      await harness.navigateByUrl('/medicines?q=a&cursor=bad');
      search().flush(
        problem({ status: 400, code: 'invalid_cursor', title: 'Invalid cursor', violations: [] }),
        { status: 400, statusText: 'Bad Request' },
      );
      harness.detectChanges();
      const link = Array.from(root().querySelectorAll('a')).find(
        (a) => a.textContent?.trim() === 'Back to first page',
      )!;

      await navigating(() => link.click());

      expect(router.url).toBe('/medicines?q=a');
      expect(paramsOf(search())).toEqual({ q: 'a' });
    });

    it('does not offer the way back on the first page', async () => {
      await harness.navigateByUrl('/medicines');
      search().flush(problem({ status: 500, code: 'internal_error', violations: [] }), {
        status: 500,
        statusText: 'Server Error',
      });
      harness.detectChanges();

      expect(root().textContent).not.toContain('Back to first page');
    });
  });
});
