import { ValidatorFn } from '@angular/forms';

import { sastLocalToUtcIso } from './time';

/**
 * Client-side checks only catch what needs no server state (missing or malformed input) so the user is
 * told at once; every business rule stays on the server. Each validator reports a ready-to-show
 * `message`, so one component can render them all.
 */

export function notBlank(label: string): ValidatorFn {
  return (control) => {
    const value: unknown = control.value;
    return typeof value === 'string' && value.trim() !== ''
      ? null
      : { message: `${label} is required.` };
  };
}

export function positiveInteger(label: string): ValidatorFn {
  return (control) => {
    const value: unknown = control.value;
    if (value === null || value === '') {
      return { message: `${label} is required.` };
    }
    return typeof value === 'number' && Number.isInteger(value) && value >= 1
      ? null
      : { message: `${label} must be a whole number of at least 1.` };
  };
}

/** An empty value is fine (the field is optional); anything else must be a real date and time. */
export const optionalSastDateTime: ValidatorFn = (control) => {
  const value: unknown = control.value;
  return typeof value !== 'string' || value === '' || sastLocalToUtcIso(value) !== null
    ? null
    : { message: 'Enter a valid date and time.' };
};
