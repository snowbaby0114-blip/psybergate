/**
 * Time handling at the UTC / business-zone boundary.
 *
 * The API stores and returns UTC instants; every business day is a calendar date in SAST. The UI
 * therefore never uses the browser's zone: it converts with an explicit IANA zone through Intl, and
 * never calls the local-time Date getters or constructors.
 */

export const SAST_TIME_ZONE = 'Africa/Johannesburg';

const WALL_CLOCK = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/;
const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** The wall-clock fields `timeZone` shows at `utcMs`, read back as if they were UTC. */
function wallClockAsUtc(utcMs: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hourCycle: 'h23',
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: 'numeric',
    second: 'numeric',
  }).formatToParts(new Date(utcMs));
  const field = (type: Intl.DateTimeFormatPartTypes): number =>
    Number(parts.find((part) => part.type === type)?.value);
  return Date.UTC(
    field('year'),
    field('month') - 1,
    field('day'),
    field('hour'),
    field('minute'),
    field('second'),
  );
}

/** Offset of `timeZone` from UTC at an instant, in ms (positive east of Greenwich). */
function zoneOffsetMs(utcMs: number, timeZone: string): number {
  return wallClockAsUtc(utcMs, timeZone) - Math.floor(utcMs / 1000) * 1000;
}

/**
 * Reads a `datetime-local` value (`YYYY-MM-DDTHH:mm[:ss[.SSS]]`) as wall-clock time in `timeZone` and
 * returns that instant as ISO-8601 UTC (`...Z`). Null for empty or invalid input, including dates that
 * do not exist such as 30 February.
 *
 * The zone's offset is looked up for the instant in question rather than assumed, so the conversion
 * is right for any zone. Wall-clock times that a DST change skips or repeats are resolved to one of
 * the two candidate instants; SAST has no DST, so for it the answer is always exact.
 */
export function localToUtcIso(value: string, timeZone: string): string | null {
  const match = WALL_CLOCK.exec(value.trim());
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6] ?? 0);
  const millis = Number((match[7] ?? '').padEnd(3, '0'));

  const wallMs = Date.UTC(year, month - 1, day, hour, minute, second, millis);
  const wall = new Date(wallMs);
  // Date.UTC rolls impossible values over (30 Feb becomes 2 Mar), so check the fields survived.
  const exists =
    wall.getUTCFullYear() === year &&
    wall.getUTCMonth() === month - 1 &&
    wall.getUTCDate() === day &&
    wall.getUTCHours() === hour &&
    wall.getUTCMinutes() === minute;
  if (!exists) {
    return null;
  }
  const firstGuess = wallMs - zoneOffsetMs(wallMs, timeZone);
  const utcMs = wallMs - zoneOffsetMs(firstGuess, timeZone);
  return new Date(utcMs).toISOString().replace('.000Z', 'Z');
}

export function sastLocalToUtcIso(value: string): string | null {
  return localToUtcIso(value, SAST_TIME_ZONE);
}

/** `2026-03-01 00:30 SAST` for a UTC instant. Unparseable input is returned unchanged. */
export function formatInstantSast(instant: string): string {
  const date = new Date(instant);
  if (Number.isNaN(date.getTime())) {
    return instant;
  }
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: SAST_TIME_ZONE,
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).formatToParts(date);
  const field = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((part) => part.type === type)?.value ?? '';
  return `${field('year')}-${field('month')}-${field('day')} ${field('hour')}:${field('minute')} SAST`;
}

/**
 * The calendar day before a business date (`YYYY-MM-DD`). Pure date arithmetic, no zone involved.
 * Malformed input is returned unchanged.
 */
export function previousDay(date: string): string {
  const match = DATE_ONLY.exec(date);
  if (!match) {
    return date;
  }
  const previous = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) - 1);
  return new Date(previous).toISOString().slice(0, 10);
}
