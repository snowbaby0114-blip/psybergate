import { AbstractControl } from '@angular/forms';

import { Violation } from '../api/models';
import { formatInstantSast } from './time';

/** The part of a FormGroup these helpers use. */
export interface ControlGroup {
  readonly controls: Readonly<Record<string, AbstractControl>>;
}

/**
 * Marks every control that the violations address as invalid, giving it ALL of its messages (several
 * violations may target one field). Violations without a field, or with a field that is not a control of
 * this form, mark nothing: they only appear in the form-level summary, which lists every violation.
 *
 * The errors live on the control until the user edits it: Angular re-runs the validators when the value
 * changes, and manually set errors do not survive that.
 */
export function applyServerErrors(form: ControlGroup, violations: readonly Violation[]): void {
  const messages = new Map<string, string[]>();
  for (const { field, message } of violations) {
    if (field !== null && Object.hasOwn(form.controls, field)) {
      messages.set(field, [...(messages.get(field) ?? []), message]);
    }
  }
  for (const [field, own] of messages) {
    form.controls[field]?.setErrors({ server: own });
  }
}

/** Forgets the server's verdict on the previous attempt, keeping only what the validators say now. */
export function discardServerErrors(form: ControlGroup): void {
  for (const control of Object.values(form.controls)) {
    control.updateValueAndValidity({ emitEvent: false });
  }
}

/**
 * The messages to show under a control: all server messages if there are any, otherwise the message
 * of the client-side validator.
 */
export function fieldMessages(control: AbstractControl): string[] {
  const errors = control.errors;
  if (errors === null) {
    return [];
  }
  const server: unknown = errors['server'];
  if (Array.isArray(server)) {
    return server.filter((message): message is string => typeof message === 'string');
  }
  const message: unknown = errors['message'];
  return typeof message === 'string' ? [message] : [];
}

/** Server errors show at once; a client-side error waits until the user has been to the control. */
export function showsErrors(control: AbstractControl): boolean {
  return control.invalid && (control.touched || control.hasError('server'));
}

/** The aria-invalid attribute value for a control: "true" while its errors are showing, otherwise absent. */
export function ariaInvalid(control: AbstractControl): 'true' | null {
  return showsErrors(control) ? 'true' : null;
}

export interface ContextEntry {
  readonly label: string;
  readonly value: string;
}

const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z$/;

/** A violation's machine-readable context as label/value pairs a person can read. */
export function describeContext(context: Readonly<Record<string, unknown>>): ContextEntry[] {
  return Object.entries(context).map(([key, value]) => ({
    label: humanise(key),
    value: formatValue(value),
  }));
}

function humanise(key: string): string {
  return key.replaceAll('_', ' ');
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') {
    return INSTANT.test(value) ? formatInstantSast(value) : value;
  }
  if (Array.isArray(value)) {
    return value.map(formatValue).join(', ');
  }
  if (typeof value === 'object' && value !== null) {
    return Object.entries(value)
      .map(([key, inner]) => `${humanise(key)}: ${formatValue(inner)}`)
      .join('; ');
  }
  return String(value);
}
