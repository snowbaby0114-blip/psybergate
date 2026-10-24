import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { ApiError } from '../api/api-error';
import { violation } from '../../testing/fixtures';
import { ErrorPanel } from './error-panel';

@Component({
  imports: [ErrorPanel],
  template: `<app-error-panel [error]="error" (retry)="retries = retries + 1"
    ><a href="/somewhere">Somewhere else</a></app-error-panel
  >`,
})
class Host {
  error = new ApiError({
    status: 503,
    code: 'resource_busy',
    title: 'Resource busy',
    detail: 'Try again.',
  });
  retries = 0;
}

describe('ErrorPanel', () => {
  function render(error: ApiError) {
    const fixture = TestBed.createComponent(Host);
    fixture.componentInstance.error = error;
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    return { fixture, root, text: () => (root.textContent ?? '').replace(/\s+/g, ' ').trim() };
  }

  it('says what happened in the words of the error, as an alert', () => {
    const { root, text } = render(
      new ApiError({
        status: 503,
        code: 'resource_busy',
        title: 'Resource busy',
        detail: 'Another request is working on the same data.',
        requestId: 'req-123',
      }),
    );

    expect(root.querySelector('[role=alert]')).not.toBeNull();
    expect(text()).toContain('Resource busy');
    expect(text()).toContain('Another request is working on the same data.');
    expect(text()).toContain('Reference: req-123');
  });

  it('leaves out the reference when there is none', () => {
    const { text } = render(
      new ApiError({ status: 0, code: 'network_error', title: 'Offline', detail: 'x' }),
    );

    expect(text()).not.toContain('Reference');
  });

  it('lists the specific problems the server named, with their codes', () => {
    const { root } = render(
      new ApiError({
        status: 422,
        code: 'validation_error',
        title: 'Validation failed',
        detail: 'The request is invalid: 1 problem found.',
        violations: [
          violation({
            code: 'too_long',
            field: 'q',
            message: 'String should have at most 100 characters.',
            context: { max_length: 100 },
          }),
        ],
      }),
    );

    expect(root.querySelector('.violation-message')?.textContent).toBe(
      'String should have at most 100 characters.',
    );
    expect(root.querySelector('.badge-code')?.textContent).toBe('too_long');
  });

  it('shows no problem list when the server named none', () => {
    const { root } = render(
      new ApiError({ status: 500, code: 'internal_error', title: 'Oops', detail: 'x' }),
    );

    expect(root.querySelector('.violations')).toBeNull();
  });

  it('emits retry from its button and shows projected actions beside it', () => {
    const { fixture, root } = render(
      new ApiError({ status: 503, code: 'resource_busy', title: 'Busy', detail: 'x' }),
    );

    root.querySelector<HTMLButtonElement>('button')!.click();

    expect(fixture.componentInstance.retries).toBe(1);
    expect(root.querySelector('.actions a')?.textContent).toBe('Somewhere else');
  });
});
