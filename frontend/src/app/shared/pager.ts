import { Component, input, output } from '@angular/core';

/**
 * Previous / Next with a "Page n" indicator. It holds no state: the page position is derived from the
 * URL by the view (see CursorStack), which also performs the navigation.
 */
@Component({
  selector: 'app-pager',
  template: `
    <nav class="pager" aria-label="Pagination">
      <button type="button" (click)="previous.emit()" [disabled]="!hasPrevious()">Previous</button>
      <span class="pager-page" aria-live="polite" aria-atomic="true">Page {{ pageNumber() }}</span>
      <button type="button" (click)="next.emit()" [disabled]="!hasNext()">Next</button>
    </nav>
  `,
})
export class Pager {
  readonly pageNumber = input.required<number>();
  readonly hasPrevious = input.required<boolean>();
  readonly hasNext = input.required<boolean>();
  readonly previous = output<void>();
  readonly next = output<void>();
}
