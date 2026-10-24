/** The part of Angular's ParamMap used here, so this module needs no framework imports. */
export interface ParamReader {
  get(name: string): string | null;
  getAll(name: string): string[];
}

/** Query parameters for the router; null removes a parameter. */
export interface CursorParams {
  readonly cursor: string | null;
  readonly trail: readonly string[] | null;
}

/**
 * Where a keyset-paginated listing currently is.
 *
 * The API only gives a `next_cursor`, so going back needs the cursors of the pages already visited.
 * They live in the URL: `cursor` is the current page's cursor and a repeated `trail` parameter holds
 * the cursors of the pages between the first page and this one, oldest first. The first page has
 * neither parameter. Reload, deep links and the browser's back/forward buttons then reproduce exactly
 * the same page, and the page number is derived from the depth, never stored separately.
 *
 * Cursors are treated as opaque strings: nothing here depends on their alphabet.
 */
export class CursorStack {
  static readonly FIRST = new CursorStack(null, []);

  private constructor(
    /** Cursor of the current page; null on the first page. */
    readonly cursor: string | null,
    /** Cursors of the pages between the first page and the current one, oldest first. */
    readonly trail: readonly string[],
  ) {}

  static fromParams(params: ParamReader): CursorStack {
    const cursor = params.get('cursor');
    if (!cursor) {
      // A trail without a current cursor describes nothing; the first page wins.
      return CursorStack.FIRST;
    }
    return new CursorStack(
      cursor,
      params.getAll('trail').filter((earlier) => earlier !== ''),
    );
  }

  get pageNumber(): number {
    return this.cursor === null ? 1 : this.trail.length + 2;
  }

  get hasPrevious(): boolean {
    return this.cursor !== null;
  }

  next(nextCursor: string): CursorStack {
    return new CursorStack(nextCursor, this.cursor === null ? [] : [...this.trail, this.cursor]);
  }

  previous(): CursorStack {
    const earlier = this.trail.at(-1);
    return earlier === undefined
      ? CursorStack.FIRST
      : new CursorStack(earlier, this.trail.slice(0, -1));
  }

  toParams(): CursorParams {
    return { cursor: this.cursor, trail: this.trail.length > 0 ? this.trail : null };
  }

  equals(other: CursorStack): boolean {
    return (
      this.cursor === other.cursor &&
      this.trail.length === other.trail.length &&
      this.trail.every((cursor, index) => cursor === other.trail[index])
    );
  }
}
