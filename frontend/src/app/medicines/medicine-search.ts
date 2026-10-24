import { Component, effect, inject } from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { Subject, debounceTime, distinctUntilChanged, map } from 'rxjs';

import { MedicinesApi } from '../api/medicines-api';
import { CursorStack, ParamReader } from '../shared/cursor-stack';
import { ErrorPanel } from '../shared/error-panel';
import { loadLatest } from '../shared/load-state';
import { Pager } from '../shared/pager';

/** How long typing must pause before it becomes a search. Submitting or leaving the box skips the wait. */
export const TYPING_DEBOUNCE_MS = 300;

export interface SearchRequest {
  readonly q: string;
  readonly stack: CursorStack;
}

export function searchRequestFrom(params: ParamReader): SearchRequest {
  return { q: (params.get('q') ?? '').trim(), stack: CursorStack.fromParams(params) };
}

export function sameSearchRequest(a: SearchRequest, b: SearchRequest): boolean {
  return a.q === b.q && a.stack.equals(b.stack);
}

@Component({
  selector: 'app-medicine-search',
  imports: [ReactiveFormsModule, RouterLink, Pager, ErrorPanel],
  templateUrl: './medicine-search.html',
})
export class MedicineSearch {
  private readonly api = inject(MedicinesApi);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly retryRequests = new Subject<void>();

  /**
   * The term the search box and the URL last agreed on. A URL carrying a different term came from
   * outside (back/forward, a link) and is copied into the box; the echo of our own navigation is not,
   * so characters typed while it was in flight are never overwritten.
   */
  private shownTerm = '';

  protected readonly term = new FormControl('', { nonNullable: true });
  protected readonly firstPage = CursorStack.FIRST.toParams();

  /** The URL is the only input: query params in, request and load state out, in one value. */
  protected readonly view = toSignal(
    loadLatest(
      this.route.queryParamMap.pipe(
        map(searchRequestFrom),
        distinctUntilChanged(sameSearchRequest),
      ),
      (request) => this.api.search({ q: request.q, cursor: request.stack.cursor }),
      this.retryRequests,
    ),
    { requireSync: true },
  );

  constructor() {
    effect(() => {
      const { q } = this.view().request;
      if (q !== this.shownTerm) {
        this.shownTerm = q;
        this.term.setValue(q, { emitEvent: false });
      }
    });

    this.term.valueChanges
      .pipe(debounceTime(TYPING_DEBOUNCE_MS), takeUntilDestroyed())
      .subscribe(() => this.applyTerm(true));
  }

  /** Asking again for the term that is already shown reloads it, so Search never seems to do nothing. */
  protected onSubmit(event: Event): void {
    event.preventDefault();
    if (this.term.value.trim() === this.shownTerm) {
      this.retry();
      return;
    }
    this.applyTerm(false);
  }

  /**
   * Puts the box's term into the URL and starts again from the first page. Typing replaces the current
   * history entry, so one burst of typing is one entry; a deliberate submit adds one.
   */
  protected applyTerm(replaceUrl: boolean): void {
    const q = this.term.value.trim();
    if (q === this.shownTerm) {
      return;
    }
    this.shownTerm = q;
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { q: q || null, ...CursorStack.FIRST.toParams() },
      queryParamsHandling: 'merge',
      replaceUrl,
    });
  }

  protected next(): void {
    const { request, state } = this.view();
    if (state.status === 'ready' && state.data.next_cursor !== null) {
      this.goTo(request.stack.next(state.data.next_cursor));
    }
  }

  protected previous(): void {
    this.goTo(this.view().request.stack.previous());
  }

  protected retry(): void {
    this.retryRequests.next();
  }

  private goTo(stack: CursorStack): void {
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: stack.toParams(),
      queryParamsHandling: 'merge',
    });
  }
}
