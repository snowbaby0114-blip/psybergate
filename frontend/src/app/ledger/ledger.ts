import { Component, effect, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { Subject, distinctUntilChanged, map } from 'rxjs';

import { DispensesApi } from '../api/dispenses-api';
import { CursorStack, ParamReader } from '../shared/cursor-stack';
import { ErrorPanel } from '../shared/error-panel';
import { loadLatest } from '../shared/load-state';
import { Pager } from '../shared/pager';
import { formatInstantSast } from '../shared/time';

export interface LedgerRequest {
  readonly patientRef: string;
  readonly medicine: string;
  readonly stack: CursorStack;
}

export function ledgerRequestFrom(params: ParamReader): LedgerRequest {
  return {
    patientRef: (params.get('patient_ref') ?? '').trim(),
    medicine: (params.get('medicine') ?? '').trim(),
    stack: CursorStack.fromParams(params),
  };
}

export function sameLedgerRequest(a: LedgerRequest, b: LedgerRequest): boolean {
  return a.patientRef === b.patientRef && a.medicine === b.medicine && a.stack.equals(b.stack);
}

@Component({
  selector: 'app-ledger',
  imports: [ReactiveFormsModule, RouterLink, Pager, ErrorPanel],
  templateUrl: './ledger.html',
})
export class Ledger {
  private readonly api = inject(DispensesApi);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly retryRequests = new Subject<void>();

  /**
   * The patient and medicine the boxes and the URL last agreed on. Boxes are copied from the URL only
   * when it names something else (back/forward, a link from the dispense page); paging leaves them
   * alone, so something typed but not yet submitted is not thrown away by a click on Next.
   */
  private shown = { patientRef: '', medicine: '' };

  protected readonly form = new FormGroup({
    patient_ref: new FormControl('', { nonNullable: true }),
    medicine: new FormControl('', { nonNullable: true }),
  });
  protected readonly firstPage = CursorStack.FIRST.toParams();
  protected readonly formatInstant = formatInstantSast;

  /** A ledger needs a patient: without one the state is `idle` and nothing is requested. */
  protected readonly view = toSignal(
    loadLatest(
      this.route.queryParamMap.pipe(
        map(ledgerRequestFrom),
        distinctUntilChanged(sameLedgerRequest),
      ),
      (request) =>
        request.patientRef === ''
          ? null
          : this.api.list({
              patient_ref: request.patientRef,
              medicine: request.medicine,
              cursor: request.stack.cursor,
            }),
      this.retryRequests,
    ),
    { requireSync: true },
  );

  constructor() {
    effect(() => {
      const { patientRef, medicine } = this.view().request;
      if (patientRef !== this.shown.patientRef || medicine !== this.shown.medicine) {
        this.shown = { patientRef, medicine };
        this.form.setValue({ patient_ref: patientRef, medicine });
      }
    });
  }

  /** Looks up what was typed, from the first page; asking again for the same thing reloads it. */
  protected submit(): void {
    const { patient_ref, medicine } = this.form.getRawValue();
    const asked = { patientRef: patient_ref.trim(), medicine: medicine.trim() };
    const { request } = this.view();
    if (asked.patientRef === request.patientRef && asked.medicine === request.medicine) {
      this.retry();
      return;
    }
    this.shown = asked;
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: {
        patient_ref: asked.patientRef || null,
        medicine: asked.medicine || null,
        ...CursorStack.FIRST.toParams(),
      },
      queryParamsHandling: 'merge',
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
