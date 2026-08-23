/**
 * The global filter bar: one period and one set of hierarchy filters, shared by
 * every report page.
 *
 * Selections are kept in the URL query string so a filtered report can be
 * bookmarked, shared, or reached by the browser's back button — and so a
 * drill-down is just a link.
 *
 * Cascading is enforced here: choosing a zone clears any region, area, unit,
 * territory and sub-territory beneath it, because those selections may no
 * longer be valid.
 */

import {
  createContext, useCallback, useContext, useMemo, useState, type ReactNode,
} from 'react';
import { useSearchParams } from 'react-router-dom';
import { masterDataService } from '../services';
import type { FilterLevel, GlobalFilters } from '../types/api';

/** Hierarchy order, shallowest first — the cascade follows this. */
export const HIERARCHY_ORDER: FilterLevel[] = [
  'company_code',
  'bu_code',
  'sales_line_code',
  'zone_code',
  'region_code',
  'area_code',
  'unit_code',
  'territory_code',
  'sub_territory_code',
];

/**
 * Where a stock position is held: Company -> Plant -> Storage Location.
 *
 * Shares its head with `HIERARCHY_ORDER` because a plant's company code *is*
 * the enterprise company code the sales hierarchy uses — the one level the two
 * surfaces genuinely have in common. Everything below it is stock-only.
 */
export const PLANT_CASCADE: FilterLevel[] = [
  'company_code',
  'plant_code',
  'storage_location_key',
];

/**
 * How the Material Master classifies goods: Group -> Brand -> Material.
 *
 * **The item chain, and since revision 0022 the only one.** It narrows a sales
 * report, a target report and a stock report alike, because all three name a
 * Material Code and resolve it against the same master. It used to be
 * stock-only, with a separate SKU / brand / category trio standing beside it
 * for the sales side; those named a second item master that no longer exists.
 *
 * A **narrowing, not a hierarchy**, going down. A brand can appear under more
 * than one material group, so choosing a group shortens the brand list rather
 * than determining it — one group takes 235 brands down to 53.
 *
 * Going *up* it resolves as far as the master is unambiguous, which is the
 * behaviour `setFilterResolved` gives every chain: choosing a material selects
 * both its brand and its group, because a material's own row states each
 * exactly. Choosing a brand selects a group only when every material of that
 * brand agrees on one — the server returns nothing rather than picking, since
 * choosing either would silently narrow the report to half the brand.
 */
export const MATERIAL_CASCADE: FilterLevel[] = [
  'material_group_code',
  'material_brand',
  'material_code',
];

/**
 * The sales hierarchy as a *cascade*, which reaches one level further than the
 * row of controls does.
 *
 * A customer is not an organisational level — it has no children and it is not
 * in the ETL's `LEVEL_BINDINGS` — but it does have a parent:
 * `dim_customer.sub_territory_code`, the authoritative mapping added in
 * revision 0011. Putting it at the foot of the chain is what makes both
 * directions work for it: choosing a territory narrows the customer list, and
 * choosing a customer selects the nine levels above it.
 *
 * It stays out of `HIERARCHY_ORDER` because that list decides which controls a
 * page draws and in what order, and the customer control belongs with the other
 * independent filters.
 */
export const SALES_CASCADE: FilterLevel[] = [...HIERARCHY_ORDER, 'customer_code'];

/**
 * Every cascade chain, so `setFilter` and `parentValueFor` work from a list
 * rather than from the one hierarchy this bar used to have.
 *
 * A level may appear in more than one chain — `company_code` heads both the
 * sales hierarchy and the plant chain — and choosing it correctly invalidates
 * the descendants in both: a different company means a different region tree
 * *and* a different set of plants.
 */
export const CASCADES: FilterLevel[][] = [
  SALES_CASCADE,
  PLANT_CASCADE,
  MATERIAL_CASCADE,
];

/**
 * Levels whose parents can be resolved from master data, and the chain each
 * belongs to.
 *
 * This is the browser's half of the bidirectional behaviour. The authoritative
 * declaration lives on the server (`GET /api/master-data/levels` publishes it
 * as `parents`); what is needed here is only *whether* a level has parents
 * worth asking about, so that choosing a company — which has none — does not
 * cost a request.
 */
export function hasAncestors(level: FilterLevel): boolean {
  return CASCADES.some((chain) => chain.indexOf(level) > 0);
}

/**
 * Filters that stand outside the *organisational* hierarchy.
 *
 * "Independent" means independent of company → … → sub-territory, not that a
 * filter has no parent of its own: the three material levels lead this list and
 * they cascade among themselves. They are here because every item-wise report
 * has to answer to them, and because a brand drill-down is a link that sets one.
 *
 * Where `sku_code`, `brand` and `category` used to sit. All three read the SKU
 * master revision 0022 removed, and the material levels answer the same three
 * questions — which item, which brand, which classification — from the master
 * that survived.
 */
export const INDEPENDENT_FILTERS: FilterLevel[] = [
  ...MATERIAL_CASCADE,
  'customer_code',
  'sales_force_code',
  'batch_code',
];

/**
 * The material-stock filter set — every filter that page offers, and the only
 * ones it offers.
 *
 * Same filter system as the rest: same URL state, same chips, same "clear
 * all". What makes it a set of its own is that these are the only filters
 * `vw_material_stock_detail` can honour. The sales hierarchy below company, the
 * customer, the sales force and the batch name columns a stock position does
 * not have — the backend skips them, so offering them would advertise a filter
 * that silently does nothing.
 *
 * The material levels are *not* what distinguishes this set any more: they are
 * global since revision 0022 and a sales page draws them too. What is still
 * stock-only is the plant chain and the shelf-life bucket.
 *
 * Order is the order the bar draws them: where the stock is, then what it is.
 */
export const STOCK_FILTERS: FilterLevel[] = [
  ...PLANT_CASCADE,
  ...MATERIAL_CASCADE,
  'expiry_status',
];

/**
 * Every filter the URL may carry.
 *
 * This is what the provider reads out of the query string and what "clear
 * everything" empties. It is a superset of what any one page draws — a page
 * names the levels it manages, and the bar shows a control and a chip for
 * those alone.
 *
 * De-duplicated, because levels legitimately belong to more than one set:
 * `company_code` heads both the sales hierarchy and the plant chain, and the
 * three material levels are in `INDEPENDENT_FILTERS` and `STOCK_FILTERS` alike.
 * A duplicate here would delete the same parameter twice and count it twice.
 */
export const ALL_FILTERS: FilterLevel[] = [
  ...new Set<FilterLevel>([
    ...HIERARCHY_ORDER,
    ...INDEPENDENT_FILTERS,
    ...STOCK_FILTERS,
  ]),
];

/**
 * Filters with no master-data list behind them.
 *
 * A batch code is transaction data — there can be millions of them and they
 * belong to no dimension table — so it is typed rather than chosen.
 *
 * A material code is **not** here any more. It was typed because the master
 * holds thousands and an unfiltered dropdown is unusable; now that it cascades
 * from material group and brand the list is short enough to choose from, and
 * choosing beats typing a code from memory.
 */
export const TEXT_FILTERS: FilterLevel[] = ['batch_code'];

/**
 * Filters drawn in the bar's **always-visible** row, beside the period, rather
 * than in the collapsible grid below it.
 *
 * Company is the only one. It heads both `SALES_CASCADE` and `PLANT_CASCADE`,
 * which is to say it is the one filter that narrows *every* surface at once:
 * the sales hierarchy beneath it, the plants the stock sits in, and — through
 * the material's own company — what may be reported against either. A filter
 * with that reach should not need a panel opened to see, and an executive who
 * has narrowed to one company should be able to tell at a glance.
 *
 * This is **layout only**. Company remains an ordinary member of both cascades,
 * of `ALL_FILTERS`, of `STOCK_FILTERS`, and of whatever set a page manages, so
 * its chip, its share of the active count, "clear all", the cascade it triggers
 * and the parameter it contributes are all unchanged. What the bar does with
 * this list is draw those levels once, up top, and skip them when it lays out
 * the groups — which is what keeps Company from appearing twice.
 */
export const GLOBAL_FILTERS: FilterLevel[] = ['company_code'];

/**
 * A titled run of filters inside one bar.
 *
 * The bar is one component and one piece of state; a group only says how the
 * controls are laid out and what to call the run. Two groups on the Executive
 * Dashboard are still one filter section, one chip row and one "clear all".
 */
export interface FilterGroup {
  /** Translation key for the heading. */
  labelKey: string;
  levels: FilterLevel[];
}

/**
 * The Executive Dashboard's two groups, in the order they are drawn.
 *
 * Declared here rather than in the page, because this is filter configuration
 * and the page is a consumer of it: the bar, the chips, the count, "clear all"
 * and the query the dashboard sends are all derived from these two lists, so
 * there is one place to change and nothing to keep in step.
 *
 * Two things are deliberately **absent** from the sales run. `unit_code` is a
 * real level of the organisational hierarchy and is still walked when a
 * customer resolves its parents — it is simply not offered here, because the
 * executive view asks about areas and territories and an extra level between
 * them is noise at this altitude. `sales_force_code` and `batch_code` are
 * absent for the same reason: they answer questions this dashboard does not
 * pose. A level the bar does not manage is not sent either — the page queries
 * `queryFor(levels)` — so an unoffered filter can never narrow these numbers
 * invisibly.
 *
 * The stock run is the plant chain and the material chain, which are two
 * separate structures and stay that way: a territory is not a plant and a sales
 * line is not a material group.
 *
 * `company_code` is in **neither** run. It is the head of both cascades, so
 * putting it in one of them would have said it belongs to that one; it is drawn
 * above both as a `GLOBAL_FILTERS` control instead. It is still very much a
 * dashboard filter — `DASHBOARD_FILTERS` below puts it back at the front — it
 * is simply not a member of either group.
 */
export const DASHBOARD_FILTER_GROUPS: FilterGroup[] = [
  {
    labelKey: 'filters.groupSales',
    levels: [
      'bu_code',
      'sales_line_code',
      'zone_code',
      'region_code',
      'area_code',
      'territory_code',
      'sub_territory_code',
      'customer_code',
    ],
  },
  {
    labelKey: 'filters.groupStock',
    levels: [
      'plant_code',
      'storage_location_key',
      'material_group_code',
      'material_brand',
      'material_code',
    ],
  },
];

/**
 * Every level the dashboard manages: the global row first, then each group in
 * order.
 *
 * The page sends `queryFor(DASHBOARD_FILTERS)`, so Company has to be here or
 * choosing one would change the controls and not the numbers. It is prepended
 * rather than left in a group for the reason given above — it belongs to both,
 * so it is drawn above both.
 */
export const DASHBOARD_FILTERS: FilterLevel[] = [
  ...GLOBAL_FILTERS,
  ...DASHBOARD_FILTER_GROUPS.flatMap((group) => group.levels),
];

/**
 * Filters whose options are a fixed set rather than a master-data lookup.
 *
 * The shelf-life bucket is derived by the backend from a date against today
 * and the configured horizon — there is no table of statuses to read, and the
 * four values are the same four `queries.EXPIRY_STATUSES` defines.
 */
export const STATIC_OPTIONS: Partial<Record<FilterLevel, string[]>> = {
  expiry_status: ['EXPIRED', 'EXPIRING_SOON', 'VALID', 'NO_EXPIRY'],
};

/** Backend level name -> the translation key for its label. */
export const FILTER_LABELS: Record<FilterLevel, string> = {
  company_code: 'filters.company',
  bu_code: 'filters.businessUnit',
  sales_line_code: 'filters.salesLine',
  zone_code: 'filters.zone',
  region_code: 'filters.region',
  area_code: 'filters.area',
  unit_code: 'filters.unit',
  territory_code: 'filters.territory',
  sub_territory_code: 'filters.subTerritory',
  customer_code: 'filters.customer',
  sales_force_code: 'filters.salesForce',
  batch_code: 'filters.batch',
  material_code: 'filters.material',
  plant_code: 'filters.plant',
  storage_location_key: 'filters.storageLocation',
  material_group_code: 'filters.materialGroup',
  material_brand: 'filters.materialBrand',
  expiry_status: 'filters.expiryStatus',
};

export const DEFAULT_PERIOD = 'THIS_MONTH';

export interface PeriodSelection {
  period: string;
  date_from?: string;
  date_to?: string;
}

interface FilterContextValue {
  filters: GlobalFilters;
  period: PeriodSelection;
  /** Filters plus period, ready to hand to a service call. */
  query: Record<string, string | undefined>;
  /** As `query`, but restricted to the levels a page actually understands. */
  queryFor: (
    levels: FilterLevel[],
    includePeriod?: boolean,
  ) => Record<string, string | undefined>;
  activeCount: number;
  /** Clear only the named levels, leaving the rest of the bar untouched. */
  clearLevels: (levels: FilterLevel[]) => void;
  setFilter: (level: FilterLevel, value: string | undefined) => void;
  /**
   * Set a filter and select the parents its master data implies.
   *
   * The child -> parent half of the bar. This is what a control calls on a user
   * change; `setFilter` remains the plain write for everything else.
   */
  setFilterResolved: (level: FilterLevel, value: string | undefined) => void;
  /** Levels currently filled in by resolution rather than by the user. */
  autoSelected: Set<FilterLevel>;
  setPeriod: (period: PeriodSelection) => void;
  clearFilters: () => void;
  /** The parent value a cascading dropdown should filter its options by. */
  parentValueFor: (level: FilterLevel) => string | undefined;
  /** The same parent, with the level it belongs to. */
  parentFor: (level: FilterLevel) => { level: FilterLevel; value: string } | undefined;
}

const FilterContext = createContext<FilterContextValue | null>(null);

export function FilterProvider({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();

  const filters = useMemo<GlobalFilters>(() => {
    const current: GlobalFilters = {};
    ALL_FILTERS.forEach((level) => {
      const value = searchParams.get(level);
      if (value) current[level] = value;
    });
    return current;
  }, [searchParams]);

  const period = useMemo<PeriodSelection>(
    () => ({
      period: searchParams.get('period') ?? DEFAULT_PERIOD,
      date_from: searchParams.get('date_from') ?? undefined,
      date_to: searchParams.get('date_to') ?? undefined,
    }),
    [searchParams],
  );

  const setFilter = useCallback(
    (level: FilterLevel, value: string | undefined) => {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          if (value) next.set(level, value);
          else next.delete(level);

          // Choosing a *different* value invalidates everything beneath it: an
          // area under a different zone would otherwise silently survive. Every
          // chain the level appears in is walked, because `company_code` heads
          // two of them and changing it invalidates both.
          //
          // **Clearing does not cascade.** Removing a parent leaves its
          // children alone, because a child is still a valid narrowing on its
          // own — filters are ANDed server-side, and a customer already implies
          // its territory. This is what lets a user drop a parent that was
          // auto-selected from a child without losing the child they chose.
          if (value) {
            CASCADES.forEach((chain) => {
              const index = chain.indexOf(level);
              if (index >= 0) {
                chain.slice(index + 1).forEach((child) => next.delete(child));
              }
            });
          }
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  /**
   * Which levels the last resolution filled in, so the bar can say so.
   *
   * Deliberately **not** in the URL. It is a note about how the current
   * selection came about, not part of the selection: a shared link should
   * reproduce the same filters, and whether a colleague picked the territory
   * themselves or got it from a customer changes nothing about the report.
   */
  const [autoSelected, setAutoSelected] = useState<Set<FilterLevel>>(
    () => new Set(),
  );

  const setFilterResolved = useCallback(
    (level: FilterLevel, value: string | undefined) => {
      // Clearing resolves nothing, and must not re-derive what it just removed
      // — that is the loop, and it is also what would make a parent impossible
      // to take off. The level simply stops counting as auto-selected.
      if (!value) {
        setAutoSelected((previous) => {
          if (!previous.has(level)) return previous;
          const next = new Set(previous);
          next.delete(level);
          return next;
        });
        setFilter(level, undefined);
        return;
      }

      if (!hasAncestors(level)) {
        setAutoSelected(new Set());
        setFilter(level, value);
        return;
      }

      // One request for the whole chain, then one write. Resolution is only
      // ever started by a user changing a control — nothing watches filter
      // state and re-derives from it — so an update cannot re-trigger itself.
      void masterDataService
        .ancestors(level, value)
        // `ancestor`, not `ancestors`: this path holds one value per level, so
        // it takes the levels that resolved to exactly one parent and leaves
        // the genuinely-several ones unset rather than picking from them.
        .then((response) => response.ancestor)
        .catch(() => ({} as Record<string, string>))
        .then((ancestors) => {
          const filled = ALL_FILTERS.filter(
            (candidate) => candidate !== level && ancestors[candidate],
          );
          setSearchParams(
            (previous) => {
              const next = new URLSearchParams(previous);
              // Descendants of the chosen level go first: they belonged to
              // whatever was selected before and may not sit under it now.
              CASCADES.forEach((chain) => {
                const index = chain.indexOf(level);
                if (index >= 0) {
                  chain.slice(index + 1).forEach((child) => next.delete(child));
                }
              });
              next.set(level, value);
              filled.forEach((parent) => next.set(parent, ancestors[parent]));
              return next;
            },
            { replace: true },
          );
          setAutoSelected(new Set(filled));
        });
    },
    [setFilter, setSearchParams],
  );

  const setPeriod = useCallback(
    (selection: PeriodSelection) => {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          next.set('period', selection.period);
          if (selection.period === 'CUSTOM' && selection.date_from && selection.date_to) {
            next.set('date_from', selection.date_from);
            next.set('date_to', selection.date_to);
          } else {
            next.delete('date_from');
            next.delete('date_to');
          }
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const clearLevels = useCallback(
    (levels: FilterLevel[]) => {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          levels.forEach((level) => next.delete(level));
          return next;
        },
        { replace: true },
      );
      // Whatever was resolved is gone with the selection that produced it —
      // an auto-selected mark on a level with no value would outlive the
      // reason it existed.
      setAutoSelected((previous) => {
        const next = new Set(previous);
        levels.forEach((level) => next.delete(level));
        return next;
      });
    },
    [setSearchParams],
  );

  const clearFilters = useCallback(
    () => clearLevels(ALL_FILTERS),
    [clearLevels],
  );

  /**
   * The nearest selected ancestor of a level: its level and its value.
   *
   * Both halves are needed. A customer's nearest selected ancestor may be a
   * sub-territory or a zone, and the code alone does not say which — the
   * options endpoint has to be told, or it cannot tell which column to narrow
   * on.
   */
  const parentFor = useCallback(
    (level: FilterLevel) => {
      // The chain that owns this level. A level heads at most one chain and is
      // a descendant in at most one, so the first match is the right one.
      const chain = CASCADES.find((candidate) => candidate.indexOf(level) > 0);
      if (!chain) return undefined;
      const index = chain.indexOf(level);
      // Use the nearest ancestor that is actually selected.
      for (let position = index - 1; position >= 0; position -= 1) {
        const parent = chain[position];
        if (filters[parent]) return { level: parent, value: filters[parent]! };
      }
      return undefined;
    },
    [filters],
  );

  const parentValueFor = useCallback(
    (level: FilterLevel) => parentFor(level)?.value,
    [parentFor],
  );

  const withPeriod = useCallback(
    (params: Record<string, string | undefined>) => {
      if (period.period === 'CUSTOM' && period.date_from && period.date_to) {
        params.date_from = period.date_from;
        params.date_to = period.date_to;
      } else {
        params.period = period.period;
      }
      return params;
    },
    [period],
  );

  const query = useMemo(() => withPeriod({ ...filters }), [filters, withPeriod]);

  /**
   * The query a page should send when it only understands some of the filters.
   *
   * Filter state is global and lives in the URL, so walking from Sales to Stock
   * carries a `region_code` the stock view cannot honour. Sending it anyway
   * made the page look narrowed when it was not. Restricting the request to the
   * levels the page manages is what keeps a stale filter from another page out
   * of this one's numbers — the state is still one object, this just decides
   * which of it applies here.
   */
  const queryFor = useCallback(
    (levels: FilterLevel[], includePeriod = true) => {
      const params: Record<string, string | undefined> = {};
      levels.forEach((level) => {
        if (filters[level]) params[level] = filters[level];
      });
      // A page with no period control leaves it out entirely. Stock ignores the
      // range server-side anyway, so sending it only means a period changed on
      // another page re-fetches every stock query for an identical answer.
      return includePeriod ? withPeriod(params) : params;
    },
    [filters, withPeriod],
  );

  const value = useMemo<FilterContextValue>(
    () => ({
      filters,
      period,
      query,
      queryFor,
      activeCount: Object.values(filters).filter(Boolean).length,
      setFilter,
      setFilterResolved,
      autoSelected,
      setPeriod,
      clearFilters,
      clearLevels,
      parentFor,
      parentValueFor,
    }),
    [filters, period, query, queryFor, setFilter, setFilterResolved, autoSelected,
     setPeriod, clearFilters, clearLevels, parentFor, parentValueFor],
  );

  return <FilterContext.Provider value={value}>{children}</FilterContext.Provider>;
}

export function useFilters(): FilterContextValue {
  const context = useContext(FilterContext);
  if (!context) throw new Error('useFilters must be used inside a FilterProvider');
  return context;
}
