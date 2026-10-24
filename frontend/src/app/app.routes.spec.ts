import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';

import { routes } from './app.routes';

describe('routes', () => {
  let harness: RouterTestingHarness;
  let router: Router;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter(routes), provideHttpClient(), provideHttpClientTesting()],
    });
    router = TestBed.inject(Router);
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => {
    // The views start loading as soon as they open; these tests are about where the URL ends up.
    TestBed.inject(HttpTestingController).match(() => true);
  });

  it.each([
    ['/', '/medicines'],
    ['/medicines', '/medicines'],
    ['/medicines?q=amox', '/medicines?q=amox'],
    ['/medicines/MED-0001', '/medicines/MED-0001'],
    ['/dispense', '/dispense'],
    ['/dispense?medicine_code=MED-0001', '/dispense?medicine_code=MED-0001'],
    ['/ledger?patient_ref=PT-1', '/ledger?patient_ref=PT-1'],
  ])('%s ends up at %s', async (url, expected) => {
    await harness.navigateByUrl(url);

    expect(router.url).toBe(expected);
  });

  it.each(['/nope', '/medicines/MED-0001/rules/extra', '/ledger/PT-1', '/api/v1/medicines'])(
    'redirects the unknown route %s to the medicines',
    async (url) => {
      await harness.navigateByUrl(url);

      expect(router.url).toBe('/medicines');
    },
  );

  it.each([
    ['/medicines', 'Medicines · EMGuidance'],
    ['/medicines/MED-0001', 'MED-0001 · EMGuidance'],
    ['/dispense', 'Dispense · EMGuidance'],
    ['/ledger', 'Patient ledger · EMGuidance'],
  ])(
    'titles %s "%s", so each view is told apart in the tab and to a screen reader',
    async (url, title) => {
      await harness.navigateByUrl(url);

      expect(document.title).toBe(title);
    },
  );
});
