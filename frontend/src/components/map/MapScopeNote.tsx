/**
 * What the map is currently showing, said over the map itself.
 *
 * Two facts, and neither is calculated here: how many points are drawn, and
 * whose data they are. The count is the length of the collection the page
 * already built, and the scope is the sentence the *server* wrote in
 * `scope_description` — the same one the page header carries — so this can
 * never describe a narrower or wider slice than was actually queried.
 *
 * It sits over the map rather than beside it because it answers a question the
 * reader has while looking at the map ("is this everything?"), and because the
 * count changes as they type in the search box. It positions itself in the
 * map's own corner, the way `MapControls` and the overlay legend do.
 *
 * The count goes through `t()` inside the sentence rather than being bolded and
 * concatenated around it. Bangla puts the number in a different place —
 * `{total}টির মধ্যে {placed}টি` next door is the proof — so a component that
 * split the sentence to emphasise one half would be assuming English word
 * order and would mistranslate the moment it was read in Bangla.
 */

import { useT } from '../../contexts/I18nContext';
import { formatCount } from '../../utils/format';

export function MapScopeNote({
  count,
  scope,
}: {
  /** Features actually drawn, after every display filter. */
  count: number;
  /** The server's description of the caller's scope, if it sent one. */
  scope?: string;
}) {
  const t = useT();

  return (
    <div className="pointer-events-none absolute right-3 top-3 z-10 max-w-[16rem] rounded-lg border border-slate-200 bg-white/95 px-2.5 py-1.5 text-[11px] shadow-sm backdrop-blur dark:border-slate-700 dark:bg-slate-900/95">
      <span className="font-medium text-slate-900 dark:text-slate-100">
        {t('map.showingPoints', { count: formatCount(count) })}
      </span>
      {scope && (
        <span
          className="mt-0.5 block truncate text-slate-500 dark:text-slate-400"
          title={scope}
        >
          {scope}
        </span>
      )}
    </div>
  );
}
