import { formatInstantSast, localToUtcIso, previousDay, sastLocalToUtcIso } from './time';

describe('sastLocalToUtcIso', () => {
  it.each([
    // The business-day boundary: 00:30 on 1 March in SAST is still 28 February in UTC.
    ['2026-03-01T00:30', '2026-02-28T22:30:00Z'],
    ['2026-03-01T00:00', '2026-02-28T22:00:00Z'],
    ['2026-03-01T01:59', '2026-02-28T23:59:00Z'],
    ['2026-03-01T02:00', '2026-03-01T00:00:00Z'],
    ['2026-03-15T12:00', '2026-03-15T10:00:00Z'],
    ['2027-01-01T01:00', '2026-12-31T23:00:00Z'],
    ['2028-02-29T10:00', '2028-02-29T08:00:00Z'],
  ])('reads %s as SAST wall-clock time: %s', (local, utc) => {
    expect(sastLocalToUtcIso(local)).toBe(utc);
  });

  it('keeps seconds and non-zero milliseconds, and drops a zero fraction', () => {
    expect(sastLocalToUtcIso('2026-03-15T12:00:30')).toBe('2026-03-15T10:00:30Z');
    expect(sastLocalToUtcIso('2026-03-15T12:00:30.250')).toBe('2026-03-15T10:00:30.250Z');
    expect(sastLocalToUtcIso('2026-03-15T12:00:30.5')).toBe('2026-03-15T10:00:30.500Z');
    expect(sastLocalToUtcIso('2026-03-15T12:00:00.000')).toBe('2026-03-15T10:00:00Z');
  });

  it('tolerates surrounding whitespace', () => {
    expect(sastLocalToUtcIso('  2026-03-15T12:00 ')).toBe('2026-03-15T10:00:00Z');
  });

  it.each([
    ['empty', ''],
    ['blank', '   '],
    ['free text', 'next tuesday'],
    ['date only', '2026-03-01'],
    ['space instead of T', '2026-03-01 10:00'],
    ['a trailing Z, which a datetime-local value never has', '2026-03-01T10:00Z'],
    ['an explicit offset', '2026-03-01T10:00+02:00'],
    ['a day that does not exist', '2026-02-30T10:00'],
    ['a leap day in a common year', '2027-02-29T10:00'],
    ['month 13', '2026-13-01T10:00'],
    ['hour 24', '2026-03-01T24:00'],
    ['minute 60', '2026-03-01T10:60'],
    ['a two digit year', '26-03-01T10:00'],
    ['year zero, which Date.UTC would silently turn into 1900', '0000-03-01T10:00'],
  ])('returns null for %s', (_description, value) => {
    expect(sastLocalToUtcIso(value)).toBeNull();
  });

  describe('on a machine in another time zone', () => {
    afterEach(() => vi.unstubAllEnvs());

    it('gives the same answers whatever the zone of the machine', () => {
      const machineOffsets = new Set<number>();
      for (const zone of ['UTC', 'America/Los_Angeles', 'Asia/Kolkata', 'Pacific/Auckland']) {
        vi.stubEnv('TZ', zone);
        machineOffsets.add(new Date(2026, 5, 1, 12).getTimezoneOffset());

        expect(sastLocalToUtcIso('2026-03-01T00:30'), zone).toBe('2026-02-28T22:30:00Z');
        expect(formatInstantSast('2026-02-28T22:30:00Z'), zone).toBe('2026-03-01 00:30 SAST');
      }
      // Guards the test itself: the machine zone really changed on every iteration.
      expect(machineOffsets.size).toBe(4);
    });
  });
});

describe('localToUtcIso', () => {
  it('looks the offset up per instant, so daylight saving is honoured (not a fixed offset)', () => {
    expect(localToUtcIso('2026-01-15T12:00', 'America/New_York')).toBe('2026-01-15T17:00:00Z');
    expect(localToUtcIso('2026-07-01T12:00', 'America/New_York')).toBe('2026-07-01T16:00:00Z');
  });

  it('gets both sides of a daylight saving change right', () => {
    // New York springs forward at 02:00 local on 2026-03-08.
    expect(localToUtcIso('2026-03-08T01:30', 'America/New_York')).toBe('2026-03-08T06:30:00Z');
    expect(localToUtcIso('2026-03-08T03:30', 'America/New_York')).toBe('2026-03-08T07:30:00Z');
  });

  it('handles zones that are not a whole number of hours from UTC', () => {
    expect(localToUtcIso('2026-03-01T12:00', 'Asia/Kolkata')).toBe('2026-03-01T06:30:00Z');
  });
});

describe('formatInstantSast', () => {
  it.each([
    ['2026-02-28T22:30:00Z', '2026-03-01 00:30 SAST'],
    // Exactly SAST midnight must read 00:00, not 24:00.
    ['2026-02-28T22:00:00Z', '2026-03-01 00:00 SAST'],
    ['2026-02-28T21:59:59Z', '2026-02-28 23:59 SAST'],
    ['2026-03-15T10:00:00Z', '2026-03-15 12:00 SAST'],
    ['2026-03-15T10:00:00.123456Z', '2026-03-15 12:00 SAST'],
    ['2026-12-31T23:00:00Z', '2027-01-01 01:00 SAST'],
  ])('shows %s as %s', (instant, formatted) => {
    expect(formatInstantSast(instant)).toBe(formatted);
  });

  it('returns unparseable input unchanged rather than showing "Invalid Date"', () => {
    expect(formatInstantSast('not a date')).toBe('not a date');
  });

  it('shows the same wall-clock time that sastLocalToUtcIso was given', () => {
    for (const local of ['2026-03-01T00:30', '2026-06-30T23:59', '2026-09-15T08:05']) {
      const utc = sastLocalToUtcIso(local);
      expect(utc).not.toBeNull();
      expect(formatInstantSast(utc ?? '')).toBe(`${local.replace('T', ' ')} SAST`);
    }
  });
});

describe('previousDay', () => {
  it.each([
    ['2026-03-15', '2026-03-14'],
    ['2026-03-01', '2026-02-28'],
    ['2028-03-01', '2028-02-29'],
    ['2027-01-01', '2026-12-31'],
  ])('is the day before %s: %s', (date, expected) => {
    expect(previousDay(date)).toBe(expected);
  });

  it('returns malformed input unchanged', () => {
    expect(previousDay('soon')).toBe('soon');
  });
});
