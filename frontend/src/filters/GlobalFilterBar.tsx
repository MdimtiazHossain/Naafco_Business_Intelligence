/**
 * The global filter bar with hierarchy-aware cascading dropdowns.
 *
 * Options for each level are fetched from the backend for the selected parent
 * (`/api/master-data/options/{level}?parent_code=…`), which does two things at
 * once: the browser never downloads the whole master data, and the options a
 * user sees are already limited to what their role permits.
 */

import { Filter, X } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { SearchableSelect } from './SearchableSelect';
import {
  FILTER_LABELS,
  GLOBAL_FILTERS,
  HIERARCHY_ORDER,
  INDEPENDENT_FILTERS,
  STATIC_OPTIONS,
  TEXT_FILTERS,
  useFilters,
  type FilterGroup,
} from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { masterDataService } from '../services';
import type { FilterLevel } from '../types/api';
import { DateFilter } from './DateFilter';

/**
 * A filter with no master list: the value is typed.
 *
 * Committed on blur or Enter rather than on every keystroke, because each
 * change rewrites the URL and refetches every report on the page.
 */
function FilterText({ level }: { level: FilterLevel }) {
  const t = useT();
  const { filters, setFilter } = useFilters();
  const [draft, setDraft] = useState(filters[level] ?? '');

  useEffect(() => setDraft(filters[level] ?? ''), [filters, level]);

  const commit = () => setFilter(level, draft.trim() || undefined);

  return (
    <div className="min-w-[9rem] flex-1">
      <label className="label" htmlFor={`filter-${level}`}>
        {t(FILTER_LABELS[level])}
      </label>
      <input
        id={`filter-${level}`}
        className="input py-1.5"
        value={draft}
        placeholder={t('common.all')}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') commit();
        }}
      />
    </div>
  );
}

// There is no unit-of-measure filter. A transaction line records one Total
// Volume and no unit, so there is no KG or LTR subset of a report to ask for.

/**
 * Call `fn` only once the caller has stopped for `delay` ms.
 *
 * Typing "ABC" must not be three requests. The timer is kept in a ref rather
 * than in state so re-arming it does not re-render, and the pending call is
 * dropped on unmount — a stale response arriving after a filter has been closed
 * would otherwise repopulate a list nobody is looking at.
 */
function useDebouncedCallback(fn: (text: string) => void, delay: number) {
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const latest = useRef(fn);
  latest.current = fn;

  useEffect(() => () => clearTimeout(timer.current), []);

  return useCallback(
    (text: string) => {
      clearTimeout(timer.current);
      timer.current = setTimeout(() => latest.current(text), delay);
    },
    [delay],
  );
}

/**
 * A filter whose options are a fixed set rather than a master-data lookup.
 *
 * The shelf-life bucket is the only one: it is derived by the backend and has
 * no table to read, so fetching options for it would be a request that could
 * only ever return the same four values.
 */
function FilterStatic({ level, values }: { level: FilterLevel; values: string[] }) {
  const t = useT();
  const { filters, setFilter } = useFilters();

  return (
    <div className="min-w-[9rem] flex-1">
      <label className="label" htmlFor={`filter-${level}`}>
        {t(FILTER_LABELS[level])}
      </label>
      <select
        id={`filter-${level}`}
        className="input py-1.5"
        value={filters[level] ?? ''}
        onChange={(event) => setFilter(level, event.target.value || undefined)}
      >
        <option value="">{t('common.all')}</option>
        {values.map((value) => (
          <option key={value} value={value}>
            {t(`stock.bucket.${value}`)}
          </option>
        ))}
      </select>
    </div>
  );
}

/**
 * One master-data filter: a searchable dropdown over the options this level's
 * current parent allows.
 *
 * Search is *inside* the list this component already has, never a second query
 * against the whole master. The parent narrows the options; the text narrows
 * those. Where the server truncated the list — thousands of customers — the
 * typed text is handed back to it, debounced, so the narrowing still happens
 * before the search rather than after it.
 *
 * `inline` puts the caption beside the control instead of above it, so a filter
 * drawn in the bar's always-visible row sits on the period selector's baseline
 * rather than standing a line taller than it. One component either way: the
 * options, the search, the resolution and the state are identical, and a filter
 * must not behave differently for being drawn somewhere else.
 */
function FilterSelect({
  level,
  inline = false,
}: {
  level: FilterLevel;
  inline?: boolean;
}) {
  const t = useT();
  const { filters, setFilterResolved, parentFor } = useFilters();
  const parent = parentFor(level);
  const [serverSearch, setServerSearch] = useState('');

  // A short list is searched where it already is, so `serverSearch` stays empty
  // and this key never changes: no request per keystroke for the great majority
  // of filters. Only a truncated level ever puts text in the key.
  const { data, isLoading } = useQuery({
    // Keyed on the parent, so choosing one refetches this level's options once
    // and every level above it keeps the list it already had.
    queryKey: [
      'filter-options', level, parent?.level ?? null, parent?.value ?? null,
      serverSearch,
    ],
    queryFn: () =>
      masterDataService.options(level, parent?.value, serverSearch || undefined,
                                parent?.level),
    staleTime: 5 * 60 * 1000,
    // The previous page of options stays on screen while a search is in flight,
    // so the list does not blink empty between keystrokes.
    placeholderData: (previous) => previous,
  });

  const options = data?.options ?? [];
  const value = filters[level];

  // 300ms: long enough that "ABC" is one request rather than three, short
  // enough to feel immediate. Only ever armed for a truncated list.
  const debouncedSearch = useDebouncedCallback(setServerSearch, 300);

  const select = (
    <SearchableSelect
      id={`filter-${level}`}
      value={value ? [value] : []}
      // Resolving rather than a plain write: choosing a child here selects
      // the parents its master data implies, in the same update.
      onChange={(next) => setFilterResolved(level, next[0])}
      options={options}
      disabled={isLoading}
      total={data?.total}
      truncated={data?.truncated}
      onSearch={debouncedSearch}
    />
  );

  if (inline) {
    return (
      // Capped rather than free-growing: this control shares a row with the
      // period and the filter buttons, and a company name is short. `min-w`
      // keeps it wide enough to read a selected name without truncation.
      <div className="flex items-center gap-2">
        <label
          className="whitespace-nowrap text-xs font-medium text-slate-500 dark:text-slate-400"
          htmlFor={`filter-${level}`}
        >
          {t(FILTER_LABELS[level])}
        </label>
        <div className="min-w-[8rem] max-w-[16rem] flex-1 sm:min-w-[10rem]">{select}</div>
      </div>
    );
  }

  return (
    <div className="min-w-[9rem] flex-1">
      <label className="label" htmlFor={`filter-${level}`}>
        {t(FILTER_LABELS[level])}
      </label>
      {select}
    </div>
  );
}

export function GlobalFilterBar({
  levels = HIERARCHY_ORDER,
  showIndependent = true,
  pageFilters = [],
  groups,
  sticky = true,
  showDate = true,
  layout = 'bar',
}: {
  levels?: FilterLevel[];
  showIndependent?: boolean;
  /**
   * Filters this page understands and the others do not — `STOCK_FILTERS` is
   * the one set of them today.
   *
   * They are ordinary filters in every other respect: the same URL state, the
   * same chips, the same "clear all". A page names the ones it can honour so
   * the bar never offers a filter that would silently do nothing.
   */
  pageFilters?: FilterLevel[];
  /**
   * Draw the controls as titled groups instead of one flat grid.
   *
   * Layout only. The groups define the order the controls appear in and what
   * each run is called; everything else — the chip row, the active count,
   * "clear all", the cascade, the resolution — reads the same single filter
   * state it always did. Passing `groups` replaces `levels`/`showIndependent`/
   * `pageFilters` as the source of what the bar manages, so a page states its
   * filter set exactly once.
   */
  groups?: FilterGroup[];
  /**
   * Whether this is the horizontal bar across the top of a page, or a column
   * in a page's own rail.
   *
   * **Layout only, and that is the whole point of it being a prop rather than a
   * second component.** The cascade, the ancestor resolution, the auto-selected
   * marks, "clear all", the chips and the single `FilterContext` state are the
   * same code in both — a rail that reimplemented any of it would be a second
   * copy of the one thing this application insists on declaring once. What
   * changes is that a rail is one column wide, is always open (there is no
   * room for a disclosure button and nothing to disclose), and never sticks,
   * because the rail it sits in scrolls on its own.
   */
  layout?: 'bar' | 'rail';
  /**
   * Keep the bar in view while the page scrolls. **On by default**, so every
   * page that renders this bar gets the behaviour from here rather than from
   * twelve copies of the same class list.
   *
   * Positioning only — this is the same component holding the same filter
   * state, so there is no second bar to keep in step and nothing to
   * synchronise. `position: sticky` does the work, so scrolling costs no
   * listener and no re-render.
   *
   * The offset is `--app-header-height` from `index.css`: the header is taller
   * below `md`, where it carries a second search row, so a single fixed offset
   * would leave a gap on a desktop or overlap on a phone.
   *
   * `z-30` sits under the header (`z-40`) and over the page, including a
   * `DataTable`'s own sticky header (`z-10`).
   *
   * **Do not wrap this in a box of its own height.** A sticky element only
   * travels within its parent, so a snug wrapper — `<div className="mb-4">` was
   * the one every page used — pins it to nothing and it scrolls away like
   * ordinary content. The bottom margin belongs here for exactly that reason;
   * the page should render `<GlobalFilterBar />` as a direct child of its
   * content root.
   */
  sticky?: boolean;
  /**
   * Draw the period selector. **On by default.**
   *
   * Material stock turns it off: the source carries no posting date, so a
   * stock figure is the same for every window, and a control that cannot
   * change the numbers is not shown beside ones that can.
   */
  showDate?: boolean;
}) {
  const t = useT();
  const { autoSelected, clearLevels, filters, setFilter } = useFilters();
  const rail = layout === 'rail';
  // A rail is always open. There is no room for a disclosure button in a column
  // this narrow, and nothing to gain by hiding controls that are the only thing
  // in it — so `expanded` is a bar-only idea and the rail ignores its state.
  const [barExpanded, setBarExpanded] = useState(false);
  const expanded = rail || barExpanded;
  const setExpanded = setBarExpanded;

  /**
   * The filters this bar is responsible for on this page.
   *
   * Everything below reads from this rather than from `ALL_FILTERS`: the count,
   * the chips and "clear all" must all describe the set the page can actually
   * honour. A page that showed a chip for a filter it ignores — the Stock page
   * did, for any region left in the URL by the page before it — claims to be
   * narrowed when it is not.
   */
  // Grouped or flat, `managed` is the one list everything else reads: the
  // count, the chips, "clear all" and the page's own query all describe exactly
  // the set the bar offers, so a filter cannot be active without being visible.
  const managed = [
    ...new Set<FilterLevel>(
      groups
        ? [...GLOBAL_FILTERS, ...groups.flatMap((group) => group.levels)]
        : [
            ...levels,
            ...(showIndependent ? INDEPENDENT_FILTERS : []),
            ...pageFilters,
          ],
    ),
  ];
  const activeChips = managed.filter((level) => filters[level]);
  const activeCount = activeChips.length;

  /**
   * The global filters this page manages, drawn in the row that is always on
   * screen — and skipped everywhere below, so none of them appears twice.
   *
   * Intersected with `managed` rather than rendered unconditionally: a page
   * states the filters it can honour, and the bar showing one it cannot would
   * be a control that silently does nothing. Every page today manages Company,
   * so in practice it is always drawn.
   */
  const globals = GLOBAL_FILTERS.filter((level) => managed.includes(level));
  /**
   * A control belongs in the grid unless the bar is drawing it somewhere else.
   *
   * In the bar that means Company, which sits in the always-visible row beside
   * the period; excluding it here is what stops it appearing twice. A rail has
   * no such row — it is one column all the way down — so there Company is an
   * ordinary grid control and appears exactly once.
   */
  const inGrid = (level: FilterLevel) => rail || !globals.includes(level);

  /** One control, chosen by what kind of filter the level is. */
  const control = (level: FilterLevel) => {
    const staticValues = STATIC_OPTIONS[level];
    if (staticValues) {
      return <FilterStatic key={level} level={level} values={staticValues} />;
    }
    return TEXT_FILTERS.includes(level) ? (
      <FilterText key={level} level={level} />
    ) : (
      <FilterSelect key={level} level={level} />
    );
  };

  /*
   * One column on a phone, and the desktop arrangement untouched.
   *
   * Two columns at 320px gave each control 135px — narrower than the dropdown
   * it opens, which is what pushed those lists off the right edge of the
   * screen, and too narrow to read a selected customer name in either. §7 asks
   * for a 1- or 2-column filter layout on mobile rather than a squeezed grid,
   * so the run starts at one and reaches the same five columns it always had by
   * `lg`, which is where the desktop bar begins.
   */
  /**
   * A bar's open panel is capped and scrolls on small screens; a rail's is
   * not, because the rail already scrolls as a column of its own.
   */
  const panelBox = rail ? '' : 'max-h-[60vh] overflow-y-auto lg:max-h-none lg:overflow-visible';

  const grid = rail
    ? 'grid grid-cols-1 gap-2'
    : 'grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5';

  return (
    <div
      className={
        rail
          ? // Never sticky: the rail this sits in scrolls on its own, and a
            // control that stuck inside a scrolling column would detach from
            // the controls above it.
            'card p-3 text-xs'
          : `card mb-4 p-3 ${
              sticky ? 'sticky top-[var(--app-header-height,3.5rem)] z-30 shadow-sm' : ''
            }`
      }
    >
      {rail && (
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="font-medium text-slate-500">{t('common.filters')}</span>
          {activeCount > 0 && (
            <button
              type="button"
              className="btn-ghost px-1.5 py-0.5 text-[11px]"
              onClick={() => clearLevels(managed)}
            >
              {t('common.clearAll')}
            </button>
          )}
        </div>
      )}

      {!rail && (
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/*
          Period, then Company, then the buttons — and this row is outside the
          `expanded` guard, so both stay on screen whether the panel is open or
          shut, at every width, and (the bar being sticky) while the page
          scrolls. `flex-wrap` is what handles a narrow screen: the two controls
          take the first row and the buttons drop below, rather than either one
          being hidden. Company follows the period and never precedes it.
        */}
        <div className="flex flex-1 flex-wrap items-center gap-x-4 gap-y-2">
          {showDate && <DateFilter />}
          {globals.map((level) => (
            <FilterSelect key={level} level={level} inline />
          ))}
        </div>

        <div className="flex items-center gap-2">
          {activeCount > 0 && (
            <button
              type="button"
              className="btn-ghost text-xs"
              onClick={() => clearLevels(managed)}
            >
              {t('common.clearAll')}
            </button>
          )}
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setExpanded((open) => !open)}
            aria-expanded={expanded}
          >
            <Filter size={14} />
            {t('common.filters')}
            {activeCount > 0 && (
              <span className="badge bg-brand-100 text-brand-700 dark:bg-brand-900 dark:text-brand-200">
                {activeCount}
              </span>
            )}
          </button>
        </div>
      </div>
      )}

      {/* In the bar, chips are what the collapsed panel says. In the rail every
          control is already on screen, so the chips are kept for the one thing
          the selects cannot show: which levels the app resolved for you, marked
          with a dashed edge, and a single click to take one off. */}
      {activeChips.length > 0 && (rail || !expanded) && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {activeChips.map((level) => (
            <button
              key={level}
              type="button"
              // A plain write, not a resolving one: removing a chip must remove
              // exactly that filter. Re-deriving it from the child that implied
              // it would make an auto-selected parent impossible to take off.
              onClick={() => setFilter(level, undefined)}
              // An auto-selected parent is marked, and says why on hover, so a
              // filter the user did not choose is never unexplained. Same chip,
              // one dotted edge — the design gains no second style.
              title={autoSelected.has(level) ? t('filters.autoSelected') : undefined}
              className={`badge gap-1 bg-slate-100 text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-200${
                autoSelected.has(level)
                  ? ' border border-dashed border-brand-400 dark:border-brand-500'
                  : ''
              }`}
            >
              {t(FILTER_LABELS[level])}:{' '}
              {/* A derived status is a code the user never typed, so the chip
                  reads it back in words; every other filter shows the code the
                  user chose, which is what they will recognise. */}
              {STATIC_OPTIONS[level]
                ? t(`stock.bucket.${filters[level]}`)
                : filters[level]}
              <X size={12} />
            </button>
          ))}
        </div>
      )}

      {expanded && groups && (
        // One section, two runs. The groups are separated by a rule and a
        // heading rather than by a box each, so they read as parts of one
        // filter area — which is what they are, since they share its state.
        //
        // `panelBox` is what keeps an open panel from filling a phone screen:
        // the bar is sticky, so fifteen stacked controls would pin the page's
        // own content out of view entirely. Bounded and scrolling, the panel is
        // the expandable area §7 asks for. The cap lifts at `lg`, where the
        // grid is five columns and the whole panel is a few rows tall.
        <div className={`mt-3 space-y-4 border-t border-slate-200 pt-3 dark:border-slate-800 ${panelBox}`}>
          {groups.map((group) => (
            <div key={group.labelKey}>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                {t(group.labelKey)}
              </h3>
              <div className={grid}>
                {group.levels.filter(inGrid).map(control)}
              </div>
            </div>
          ))}
        </div>
      )}

      {expanded && !groups && (
        <div
          className={`mt-3 border-t border-slate-200 pt-3 dark:border-slate-800 ${panelBox} ${grid}`}
        >
          {managed.filter(inGrid).map(control)}
        </div>
      )}
    </div>
  );
}
