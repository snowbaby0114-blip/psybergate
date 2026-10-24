import { Component, inject, input, output, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { finalize } from 'rxjs';

import { ApiError, toApiError } from '../api/api-error';
import { MedicinesApi } from '../api/medicines-api';
import { RuleSupersedeResult } from '../api/models';
import { FieldErrors } from '../shared/field-errors';
import { notBlank, positiveInteger } from '../shared/validators';
import { ViolationList } from '../shared/violation-list';
import { applyServerErrors, ariaInvalid, discardServerErrors } from '../shared/violations';

/**
 * Introduces a rule from a business date, superseding whatever is in force then (the server decides
 * what that means). A conflict such as a rule already starting that day comes back as violations on
 * `effective_from`, shown under that field and in the summary; on success the parent is told, so it can
 * reload the timeline and report what changed.
 */
@Component({
  selector: 'app-new-rule-form',
  imports: [ReactiveFormsModule, FieldErrors, ViolationList],
  templateUrl: './new-rule-form.html',
})
export class NewRuleForm {
  private readonly api = inject(MedicinesApi);

  readonly code = input.required<string>();
  readonly introduced = output<RuleSupersedeResult>();

  /** Control names are the request's field names, which is how violations find their control. */
  protected readonly form = new FormGroup({
    effective_from: new FormControl('', {
      nonNullable: true,
      validators: [notBlank('Start date')],
    }),
    max_quantity_per_dispense: new FormControl<number | null>(null, [
      positiveInteger('Maximum per dispense'),
    ]),
    max_quantity_per_30_days: new FormControl<number | null>(null, [
      positiveInteger('Maximum per 30 days'),
    ]),
    requires_authorisation: new FormControl(false, { nonNullable: true }),
  });

  protected readonly submitting = signal(false);
  protected readonly failure = signal<ApiError | null>(null);
  protected readonly ariaInvalid = ariaInvalid;

  protected submit(): void {
    if (this.submitting()) {
      return;
    }
    discardServerErrors(this.form);
    const { max_quantity_per_dispense, max_quantity_per_30_days, ...rest } =
      this.form.getRawValue();
    if (
      this.form.invalid ||
      max_quantity_per_dispense === null ||
      max_quantity_per_30_days === null
    ) {
      this.form.markAllAsTouched();
      return;
    }

    this.failure.set(null);
    this.submitting.set(true);
    this.api
      .introduceRule(this.code(), {
        effective_from: rest.effective_from.trim(),
        max_quantity_per_dispense,
        max_quantity_per_30_days,
        requires_authorisation: rest.requires_authorisation,
      })
      .pipe(finalize(() => this.submitting.set(false)))
      .subscribe({
        next: (result) => {
          this.form.reset();
          this.introduced.emit(result);
        },
        error: (error: unknown) => this.failed(toApiError(error)),
      });
  }

  private failed(error: ApiError): void {
    applyServerErrors(this.form, error.violations);
    this.failure.set(error);
  }
}
