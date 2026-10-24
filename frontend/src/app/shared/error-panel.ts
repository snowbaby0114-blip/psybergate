import { Component, input, output } from '@angular/core';

import { ApiError } from '../api/api-error';
import { ViolationList } from './violation-list';

/**
 * Shows an ApiError in plain words with a Retry button; extra actions can be projected in. When the
 * server named specific problems (an over-long search term, a bad cursor) they are listed too.
 */
@Component({
  selector: 'app-error-panel',
  imports: [ViolationList],
  template: `
    <div class="alert alert-error" role="alert">
      <h2 class="alert-title">{{ error().title }}</h2>
      <p>{{ error().detail }}</p>
      @if (error().violations.length > 0) {
        <app-violation-list [violations]="error().violations" />
      }
      @if (error().requestId; as requestId) {
        <p class="muted">
          Reference: <code>{{ requestId }}</code>
        </p>
      }
      <div class="actions">
        <button type="button" (click)="retry.emit()">Retry</button>
        <ng-content />
      </div>
    </div>
  `,
})
export class ErrorPanel {
  readonly error = input.required<ApiError>();
  readonly retry = output<void>();
}
