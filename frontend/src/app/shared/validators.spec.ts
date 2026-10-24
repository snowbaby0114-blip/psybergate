import { FormControl } from '@angular/forms';

import { notBlank, optionalSastDateTime, positiveInteger } from './validators';

describe('notBlank', () => {
  it.each(['', '   ', '\t\n'])('rejects %j with the field name in the message', (value) => {
    expect(notBlank('Patient reference')(new FormControl(value))).toEqual({
      message: 'Patient reference is required.',
    });
  });

  it('accepts text', () => {
    expect(notBlank('Patient reference')(new FormControl(' PT-1 '))).toBeNull();
  });
});

describe('positiveInteger', () => {
  const validate = positiveInteger('Quantity');

  it('asks for a quantity when there is none', () => {
    expect(validate(new FormControl(null))).toEqual({ message: 'Quantity is required.' });
    expect(validate(new FormControl(''))).toEqual({ message: 'Quantity is required.' });
  });

  it.each([0, -3, 1.5, Number.NaN])('rejects %s', (value) => {
    expect(validate(new FormControl(value))).toEqual({
      message: 'Quantity must be a whole number of at least 1.',
    });
  });

  it.each([1, 20, 1_000_000])('accepts %s', (value) => {
    expect(validate(new FormControl(value))).toBeNull();
  });
});

describe('optionalSastDateTime', () => {
  it('accepts an empty value, since the field is optional', () => {
    expect(optionalSastDateTime(new FormControl(''))).toBeNull();
  });

  it('accepts a real date and time', () => {
    expect(optionalSastDateTime(new FormControl('2026-03-01T00:30'))).toBeNull();
  });

  it.each(['2026-02-30T10:00', 'tomorrow', '2026-03-01'])('rejects %j', (value) => {
    expect(optionalSastDateTime(new FormControl(value))).toEqual({
      message: 'Enter a valid date and time.',
    });
  });
});
