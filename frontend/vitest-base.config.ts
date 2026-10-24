import { defineConfig } from 'vitest/config';

// Each worker boots Angular's TestBed on jsdom, which is memory- and CPU-hungry. Inside a container
// that reports every host CPU, one worker per CPU starves the machine and tests miss the default
// 5 s timeout, so the pool is capped and the timeout leaves room for a slow environment.
export default defineConfig({
  test: {
    maxWorkers: 2,
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
