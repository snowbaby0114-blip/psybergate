import { CursorStack } from './cursor-stack';

/** What the router would put in the address bar for this stack, and read back from it. */
function roundTrip(stack: CursorStack): CursorStack {
  const query = new URLSearchParams();
  const { cursor, trail } = stack.toParams();
  if (cursor !== null) {
    query.set('cursor', cursor);
  }
  for (const earlier of trail ?? []) {
    query.append('trail', earlier);
  }
  return CursorStack.fromParams(new URLSearchParams(query.toString()));
}

function after(...cursors: string[]): CursorStack {
  return cursors.reduce((stack, cursor) => stack.next(cursor), CursorStack.FIRST);
}

describe('CursorStack', () => {
  describe('the first page', () => {
    it('is page 1, has no previous page and puts nothing in the URL', () => {
      expect(CursorStack.FIRST.pageNumber).toBe(1);
      expect(CursorStack.FIRST.hasPrevious).toBe(false);
      expect(CursorStack.FIRST.toParams()).toEqual({ cursor: null, trail: null });
    });

    it('stays the first page when asked to go back', () => {
      expect(CursorStack.FIRST.previous()).toBe(CursorStack.FIRST);
    });
  });

  describe('moving forward', () => {
    it('makes the next_cursor the current cursor and counts the pages', () => {
      const second = after('c1');
      const third = after('c1', 'c2');

      expect(second).toMatchObject({ cursor: 'c1', trail: [], pageNumber: 2, hasPrevious: true });
      expect(third).toMatchObject({ cursor: 'c2', trail: ['c1'], pageNumber: 3 });
      expect(after('c1', 'c2', 'c3', 'c4', 'c5').pageNumber).toBe(6);
    });

    it('does not modify the stack it was called on', () => {
      const second = after('c1');

      second.next('c2');

      expect(second).toMatchObject({ cursor: 'c1', trail: [] });
    });
  });

  describe('moving back', () => {
    it('pops the trail one page at a time down to the first page', () => {
      const fourth = after('c1', 'c2', 'c3');

      const third = fourth.previous();
      const second = third.previous();
      const first = second.previous();

      expect(third).toMatchObject({ cursor: 'c2', trail: ['c1'], pageNumber: 3 });
      expect(second).toMatchObject({ cursor: 'c1', trail: [], pageNumber: 2 });
      expect(first.equals(CursorStack.FIRST)).toBe(true);
      expect(first.pageNumber).toBe(1);
    });

    it('is the inverse of next', () => {
      for (const stack of [CursorStack.FIRST, after('c1'), after('c1', 'c2', 'c3')]) {
        expect(stack.next('later').previous().equals(stack)).toBe(true);
      }
    });
  });

  describe('URL round trip', () => {
    it.each([[[]], [['c1']], [['c1', 'c2']], [['c1', 'c2', 'c3', 'c4']]])(
      'reproduces the same page after reload for %j',
      (cursors) => {
        const stack = after(...cursors);

        const reloaded = roundTrip(stack);

        expect(reloaded.equals(stack)).toBe(true);
        expect(reloaded.pageNumber).toBe(stack.pageNumber);
      },
    );

    it('treats cursors as opaque: padding, separators and reserved characters survive', () => {
      const awkward = [
        'eyJrIjpbIkEiLCJCIl19==',
        'a,b.c',
        'x&y=z',
        'plus+space and%percent',
        'ünï/cödé',
      ];

      const stack = after(...awkward);

      expect(roundTrip(stack).equals(stack)).toBe(true);
      expect(roundTrip(stack).cursor).toBe('ünï/cödé');
      expect(roundTrip(stack).trail).toEqual(awkward.slice(0, -1));
    });

    it('lets the back button land on the previous stack, not on a recomputed one', () => {
      const third = after('c1', 'c2');
      const urlOfSecond = roundTrip(third.previous());

      expect(urlOfSecond.equals(after('c1'))).toBe(true);
    });
  });

  describe('reading the URL', () => {
    it('ignores a trail when there is no current cursor', () => {
      const stack = CursorStack.fromParams(new URLSearchParams('trail=a&trail=b'));

      expect(stack.equals(CursorStack.FIRST)).toBe(true);
    });

    it('treats an empty cursor as the first page', () => {
      expect(CursorStack.fromParams(new URLSearchParams('cursor=')).pageNumber).toBe(1);
    });

    it('skips empty trail entries', () => {
      const stack = CursorStack.fromParams(new URLSearchParams('cursor=c3&trail=&trail=c1&trail='));

      expect(stack).toMatchObject({ cursor: 'c3', trail: ['c1'], pageNumber: 3 });
    });
  });

  describe('equals', () => {
    it('compares the cursor and the trail in order', () => {
      expect(after('a', 'b').equals(after('a', 'b'))).toBe(true);
      expect(after('a', 'b').equals(after('b', 'a'))).toBe(false);
      expect(after('a', 'b').equals(after('c', 'b'))).toBe(false);
      expect(after('a').equals(after('a', 'b'))).toBe(false);
    });
  });
});
