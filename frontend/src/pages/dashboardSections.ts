/**
 * The dashboard's cards, fetched one request each and in parallel.
 *
 * They arrived in a single response until the page took nine seconds to paint:
 * eight independent aggregates run one after another because they shared a
 * database session. The business map met the same problem — five layers in one
 * call waited 7.5 s on the PostgreSQL deployment before the first could paint —
 * and answered it the same way, so the two surfaces now solve it once between
 * them.
 *
 * `useQueries` rather than one query fanning out with `Promise.all`, so a card
 * draws when *its* answer arrives instead of every card waiting for the
 * slowest. A section that fails leaves the others alone, which is also what
 * lets one empty card sit beside five full ones rather than taking the page
 * down.
 */

import { useQueries } from '@tanstack/react-query';
import { useMemo } from 'react';
import { dashboardService } from '../services';
import type { ReportQuery } from '../services';
import type { ToolResult } from '../types/api';

export interface DashboardSections {
  /** Each card's result by name, absent until that card has answered. */
  byName: Record<string, ToolResult | undefined>;
  /** True while any card is still in flight. */
  isLoading: boolean;
  /** The first failure, if a card could not be drawn. */
  error: unknown;
}

/**
 * `names` comes from the frame response rather than being listed here, so a
 * card added on the server is fetched without the browser being taught about
 * it, and one removed stops being asked for. `enabled` waits for that list:
 * asking before it arrives would fetch nothing and then fetch again.
 */
export function useDashboardSections(
  names: string[] | undefined,
  query: ReportQuery,
): DashboardSections {
  const results = useQueries({
    queries: (names ?? []).map((name) => ({
      queryKey: ['dashboard-section', name, query],
      queryFn: () => dashboardService.section(name, query),
      enabled: names !== undefined,
    })),
  });

  return useMemo(() => {
    const byName: Record<string, ToolResult | undefined> = {};
    for (const result of results) {
      if (result.data) byName[result.data.name] = result.data.section;
    }
    return {
      byName,
      isLoading: results.some((result) => result.isLoading),
      error: results.find((result) => result.error)?.error,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [results.map((r) => r.dataUpdatedAt).join(','), results.length]);
}
