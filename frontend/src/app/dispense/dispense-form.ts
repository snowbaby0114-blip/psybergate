import { Component, ElementRef, inject, signal } from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import {
  catchError,
  debounceTime,
  distinctUntilChanged,
  filter,
  finalize,
  map,
  of,
  switchMap,
} from 'rxjs';

import { ApiError, toApiError } from '../api/api-error';
import { DispenseResult, DispensesApi } from '../api/dispenses-api';
import { MedicinesApi } from '../api/medicines-api';
import { MedicineOut } from '../api/models';
import { FieldErrors } from '../shared/field-errors';
import { formatInstantSast } from '../shared/time';
import { notBlank, optionalSastDateTime, positiveInteger } from '../shared/validators';
import { ViolationList } from '../shared/violation-list';
import { applyServerErrors, ariaInvalid, discardServerErrors } from '../shared/violations';
import { toDispensePayload } from './dispense-payload';
import { IdempotencyKeyManager } from './idempotency-key';

/** How long typing in the medicine field must pause before suggestions are requested. */
export const SUGGESTION_DEBOUNCE_MS = 250;
const SUGGESTION_LIMIT = 8;
const NO_SUGGESTIONS: readonly MedicineOut[] = [];

type Outcome =
  | { readonly kind: 'created'; readonly result: DispenseResult }
  | { readonly kind: 'failed'; readonly error: ApiError };

@Component({
  selector: 'app-dispense-form',
  imports: [ReactiveFormsModule, RouterLink, FieldErrors, ViolationList],
  templateUrl: './dispense-form.html',
})
export class DispenseForm {
  private readonly dispenses = inject(DispensesApi);
  private readonly medicines = inject(MedicinesApi);
  private readonly route = inject(ActivatedRoute);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef).nativeElement;
  private readonly keys = new IdempotencyKeyManager();

  /** Control names are the request's field names, which is how violations find their control. */
  protected readonly form = new FormGroup({
    medicine_code: new FormControl('', {
      nonNullable: true,
      validators: [notBlank('Medicine code')],
    }),
    patient_ref: new FormControl('', {
      nonNullable: true,
      validators: [notBlank('Patient reference')],
    }),
    quantity: new FormControl<number | null>(null, [positiveInteger('Quantity')]),
    dispensed_at: new FormControl('', { nonNullable: true, validators: [optionalSastDateTime] }),
    authorisation_ref: new FormControl('', { nonNullable: true }),
  });

  protected readonly submitting = signal(false);
  protected readonly outcome = signal<Outcome | null>(null);

  /** Suggestions are a convenience: a failed lookup is silent, and a newer keystroke cancels the old one. */
  protected readonly suggestions = toSignal(
    this.form.controls.medicine_code.valueChanges.pipe(
      debounceTime(SUGGESTION_DEBOUNCE_MS),
      map((value) => value.trim()),
      distinctUntilChanged(),
      switchMap((q) =>
        q === ''
          ? of(NO_SUGGESTIONS)
          : this.medicines.search({ q, limit: SUGGESTION_LIMIT }).pipe(
              map((page) => page.items),
              catchError(() => of(NO_SUGGESTIONS)),
            ),
      ),
    ),
    { initialValue: NO_SUGGESTIONS },
  );

  protected readonly formatInstant = formatInstantSast;
  protected readonly ariaInvalid = ariaInvalid;

  constructor() {
    // Prefill from a link such as /dispense?medicine_code=MED-0001. Following the URL (rather than
    // reading it once) keeps this right when the route is reused for a different code.
    this.route.queryParamMap
      .pipe(
        map((params) => params.get('medicine_code')),
        filter((code): code is string => !!code),
        distinctUntilChanged(),
        takeUntilDestroyed(),
      )
      .subscribe((code) => this.form.controls.medicine_code.setValue(code, { emitEvent: false }));
  }

  protected submit(): void {
    if (this.submitting()) {
      return;
    }
    discardServerErrors(this.form);
    const { quantity, ...values } = this.form.getRawValue();
    if (this.form.invalid || quantity === null) {
      this.form.markAllAsTouched();
      this.focusFirstInvalid();
      return;
    }

    const payload = toDispensePayload({ ...values, quantity });
    const idempotencyKey = this.keys.keyFor(payload);
    this.outcome.set(null);
    this.submitting.set(true);
    this.dispenses
      .create({ ...payload, idempotency_key: idempotencyKey })
      .pipe(finalize(() => this.submitting.set(false)))
      .subscribe({
        next: (result) => this.created(result),
        error: (error: unknown) => this.failed(toApiError(error)),
      });
  }

  private created(result: DispenseResult): void {
    this.keys.succeeded();
    this.form.reset();
    this.outcome.set({ kind: 'created', result });
  }

  private failed(error: ApiError): void {
    this.keys.failed(error);
    applyServerErrors(this.form, error.violations);
    this.outcome.set({ kind: 'failed', error });
  }

  private focusFirstInvalid(): void {
    const first = Object.entries(this.form.controls).find(([, control]) => control.invalid);
    if (first) {
      this.host.querySelector<HTMLElement>(`#field-${first[0]}`)?.focus();
    }
  }
}
