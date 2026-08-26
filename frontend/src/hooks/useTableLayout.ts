/**
 * Per-table column order and column widths.
 *
 * `DataTable` already lets a reader hide a column; this adds the other two
 * arrangements a report reader asks for — moving a column left or right, and
 * making one narrower or wider. Both are presentation only: nothing here
 * changes what was queried, what a column means, or what an export contains.
 *
 * The arrangement is remembered in `localStorage` under
 * `bi.table-layout.<tableId>`, the same way the language and theme preferences
 * are, because a reader who arranged a report once should not have to arrange
 * it again after a refresh. A table that passes no `tableId` still works — its
 * layout simply lives as long as the component is mounted, which is what an
 * ad-hoc table (a chat answer, an upload's rejected rows) wants.
 */

import { useCallback, useMemo, useState } from 'react';

const STORAGE_PREFIX = 'bi.table-layout.';

/**
 * The separator that turns the column keys into one memo dependency. A NUL
 * cannot occur in a column key, so no pair of key lists can collide on it.
 */
const KEY_SEPARATOR = '\u0000';

/** Below this a column stops being readable and becomes a drag handle. */
export const MIN_COLUMN_WIDTH = 60;

export interface TableLayout {
  /** Column keys in the order the reader arranged them; empty means untouched. */
  order: string[];
  /** Pinned pixel widths. A column with no entry sizes itself to its content. */
  widths: Record<string, number>;
}

const EMPTY: TableLayout = { order: [], widths: {} };

function storageKey(tableId: string) {
  return STORAGE_PREFIX + tableId;
}

function readStored(tableId?: string): TableLayout {
  if (!tableId) return EMPTY;
  try {
    const raw = localStorage.getItem(storageKey(tableId));
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<TableLayout>;
    // Storage holds data of unknown age written by an unknown version, so it is
    // validated rather than trusted: a truncated or hand-edited entry has to
    // degrade to the default layout, never to a table that renders nothing.
    const order = Array.isArray(parsed.order)
      ? parsed.order.filter((key): key is string => typeof key === 'string')
      : [];
    const widths: Record<string, number> = {};
    for (const [key, value] of Object.entries(parsed.widths ?? {})) {
      if (typeof value === 'number' && Number.isFinite(value) && value >= MIN_COLUMN_WIDTH) {
        widths[key] = value;
      }
    }
    return { order, widths };
  } catch {
    return EMPTY;
  }
}

function writeStored(tableId: string | undefined, layout: TableLayout) {
  if (!tableId) return;
  try {
    // An untouched layout is removed rather than written as an empty object, so
    // a reset leaves no trace and a later change to the report's own column
    // order is picked up instead of being overridden by a stale arrangement.
    if (layout.order.length === 0 && Object.keys(layout.widths).length === 0) {
      localStorage.removeItem(storageKey(tableId));
    } else {
      localStorage.setItem(storageKey(tableId), JSON.stringify(layout));
    }
  } catch {
    /* the in-memory layout still applies */
  }
}

/**
 * Fit a remembered order to the columns a table actually has today.
 *
 * A stored name must not outlive what it names: a column the report no longer
 * returns is dropped. The half that matters more is the reverse — a column the
 * report has *gained* is inserted rather than lost, because an order that
 * silently omitted a key would hide a column with no control anywhere to bring
 * it back. A new column lands beside the column it was declared after, so
 * adding one in the middle of a report does not surface it at the far right.
 */
export function reconcileOrder(stored: string[], keys: string[]): string[] {
  const known = new Set(keys);
  const result = stored.filter(
    (key, index) => known.has(key) && stored.indexOf(key) === index,
  );
  const placed = new Set(result);
  keys.forEach((key, index) => {
    if (placed.has(key)) return;
    let at = 0;
    for (let before = index - 1; before >= 0; before -= 1) {
      if (placed.has(keys[before])) {
        at = result.indexOf(keys[before]) + 1;
        break;
      }
    }
    result.splice(at, 0, key);
    placed.add(key);
  });
  return result;
}

export interface TableLayoutControls {
  /** Every column key, reconciled against what the table declares today. */
  order: string[];
  widths: Record<string, number>;
  /** Move a column one place towards the front (-1) or the back (1). */
  moveColumn: (key: string, direction: -1 | 1) => void;
  /** Drop one column onto another's position — what a header drag means. */
  moveColumnTo: (key: string, targetKey: string) => void;
  setWidth: (key: string, width: number) => void;
  /** Freeze a measured set of widths, as a resize does before its first drag. */
  pinWidths: (measured: Record<string, number>) => void;
  clearWidth: (key: string) => void;
  reset: () => void;
  /** True once the reader has arranged something — what enables "Reset layout". */
  isCustomised: boolean;
}

export function useTableLayout(
  columnKeys: string[],
  tableId?: string,
): TableLayoutControls {
  const [store, setStore] = useState<{ id?: string; layout: TableLayout }>(() => ({
    id: tableId,
    layout: readStored(tableId),
  }));

  // A page can point one table at a different `tableId` (Master Data switches
  // entity under the same component). Re-reading during render rather than in
  // an effect avoids a frame in which the previous table's arrangement is
  // applied to this one's columns.
  const layout = store.id === tableId ? store.layout : readStored(tableId);
  if (store.id !== tableId) setStore({ id: tableId, layout });

  // The dependency is the joined keys rather than the array itself, which every
  // caller rebuilds on each render.
  const signature = columnKeys.join(KEY_SEPARATOR);
  const order = useMemo(
    () =>
      reconcileOrder(layout.order, signature ? signature.split(KEY_SEPARATOR) : []),
    [layout.order, signature],
  );

  const commit = useCallback(
    (next: TableLayout) => {
      setStore({ id: tableId, layout: next });
      writeStored(tableId, next);
    },
    [tableId],
  );

  const moveColumn = useCallback(
    (key: string, direction: -1 | 1) => {
      // The step is one place in the *full* order, hidden columns included,
      // because this is driven from the column panel, which lists every column
      // in that same order — a step that skipped a hidden neighbour would not
      // match what the reader is looking at.
      const from = order.indexOf(key);
      const to = from + direction;
      if (from < 0 || to < 0 || to >= order.length) return;
      const next = [...order];
      next[from] = next[to];
      next[to] = key;
      commit({ order: next, widths: layout.widths });
    },
    [order, layout.widths, commit],
  );

  const moveColumnTo = useCallback(
    (key: string, targetKey: string) => {
      if (key === targetKey) return;
      const from = order.indexOf(key);
      const to = order.indexOf(targetKey);
      if (from < 0 || to < 0) return;
      const next = order.filter((each) => each !== key);
      const at = next.indexOf(targetKey);
      // Dragging towards the back lands after the column dropped on and towards
      // the front lands before it, which is the only reading of a drop that can
      // reach both ends of the table.
      next.splice(from < to ? at + 1 : at, 0, key);
      commit({ order: next, widths: layout.widths });
    },
    [order, layout.widths, commit],
  );

  // A width change stores `layout.order`, not the reconciled `order`: a reader
  // who only resized has expressed no opinion about arrangement, and writing
  // today's order would freeze it against a later change to the report itself.
  const setWidth = useCallback(
    (key: string, width: number) => {
      commit({
        order: layout.order,
        widths: {
          ...layout.widths,
          [key]: Math.max(MIN_COLUMN_WIDTH, Math.round(width)),
        },
      });
    },
    [layout, commit],
  );

  const pinWidths = useCallback(
    (measured: Record<string, number>) => {
      const widths = { ...layout.widths };
      for (const [key, width] of Object.entries(measured)) {
        widths[key] = Math.max(MIN_COLUMN_WIDTH, Math.round(width));
      }
      commit({ order: layout.order, widths });
    },
    [layout, commit],
  );

  const clearWidth = useCallback(
    (key: string) => {
      if (!(key in layout.widths)) return;
      const widths = { ...layout.widths };
      delete widths[key];
      commit({ order: layout.order, widths });
    },
    [layout, commit],
  );

  const reset = useCallback(() => commit(EMPTY), [commit]);

  return {
    order,
    widths: layout.widths,
    moveColumn,
    moveColumnTo,
    setWidth,
    pinWidths,
    clearWidth,
    reset,
    isCustomised: layout.order.length > 0 || Object.keys(layout.widths).length > 0,
  };
}
