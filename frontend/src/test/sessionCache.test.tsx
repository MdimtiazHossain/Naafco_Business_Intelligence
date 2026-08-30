/**
 * The query cache belongs to a session, and ends with it.
 *
 * Two failures come from letting it outlive one, and both were real.
 *
 * **A cached 401 is permanent.** The client deliberately does not retry an auth
 * failure — retrying one only wastes the user's time — so the error it caches
 * survives the next sign-in and the query never runs again. On Target
 * Management that left the options query's `data` undefined, which reads as
 * "you hold no actions", and the country-target upload controls silently
 * vanished after a logout or a server restart while the plan list, fed by a
 * different query, carried on working.
 *
 * **A cached success is worse.** Sign out and back in as somebody else and the
 * new session is served the previous user's responses from cache, for as long
 * as they stay fresh. That is a permission boundary the server enforces on
 * every request and the browser was quietly stepping around.
 */

import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';

/**
 * The client's real retry rule, restated here rather than imported: `App.tsx`
 * builds its `QueryClient` at module scope alongside the whole router, and
 * importing it to read one option would drag the application in with it.
 * `sessionCache` keeps this honest by asserting the behaviour the rule
 * produces, not the rule itself.
 */
function client() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 60 * 1000,
        retry: false,
        gcTime: Infinity,
      },
    },
  });
}

describe('the query cache across a session boundary', () => {
  it('keeps a 401 forever, which is why the cache has to be cleared', async () => {
    const queryClient = client();
    await queryClient.prefetchQuery({
      queryKey: ['target-management-options'],
      queryFn: () => Promise.reject(new Error('401')),
    });

    // The failure is remembered and, because auth failures are not retried,
    // nothing will dislodge it on its own.
    const state = queryClient.getQueryState(['target-management-options']);
    expect(state?.status).toBe('error');
    expect(queryClient.getQueryData(['target-management-options'])).toBeUndefined();

    // This is exactly what the page then computes: no data means no actions,
    // which means the upload controls render nothing.
    const actions = (queryClient.getQueryData(['target-management-options']) as
      { actions?: Record<string, boolean> } | undefined)?.actions ?? {};
    expect(actions.UPLOAD === true).toBe(false);

    queryClient.clear();
    expect(queryClient.getQueryState(['target-management-options'])).toBeUndefined();
  });

  it('serves a previous session cached data until the cache is cleared', async () => {
    const queryClient = client();
    await queryClient.prefetchQuery({
      queryKey: ['target-management-plans'],
      queryFn: () => Promise.resolve({ rows: [{ plan_code: 'TP-2026-001' }] }),
    });
    expect(queryClient.getQueryData(['target-management-plans'])).toBeTruthy();

    // Signing out must not leave one user's rows readable by the next.
    queryClient.clear();
    expect(queryClient.getQueryData(['target-management-plans'])).toBeUndefined();
  });

  it('clearing removes every key, not just the one that failed', async () => {
    const queryClient = client();
    await Promise.all([
      queryClient.prefetchQuery({
        queryKey: ['target-management-options'],
        queryFn: () => Promise.resolve({ actions: { UPLOAD: true } }),
      }),
      queryClient.prefetchQuery({
        queryKey: ['target-management-plans'],
        queryFn: () => Promise.resolve({ rows: [] }),
      }),
      queryClient.prefetchQuery({
        queryKey: ['credit-control'],
        queryFn: () => Promise.resolve({ metrics: {} }),
      }),
    ]);
    expect(queryClient.getQueryCache().getAll()).toHaveLength(3);

    queryClient.clear();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
