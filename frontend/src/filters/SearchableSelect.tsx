/**
 * The one dropdown every master-data filter in the application uses.
 *
 * A native `<select>` cannot be searched, and several of these lists are long:
 * 405 materials, 267 sub-territories, thousands of customers. Scrolling to find
 * one is not a way to use a filter, so this replaces the native control with a
 * button, a popup and a search box — while keeping the parts that matter to the
 * rest of the system identical.
 *
 * **Search never widens the list.** It is applied to the options the current
 * filter context already produced, never to the database at large: the backend
 * narrows by parent first and `ilike`s the result, and this component's
 * client-side pass filters the array it was handed. Choosing a company and then
 * searching for a material therefore searches that company's materials, which is
 * the whole point of having a hierarchy.
 *
 * **Client-side until it cannot be.** When the server says it returned the whole
 * list (`truncated: false`), typing filters the array already in memory — no
 * request per keystroke, instant results. Only a truncated list falls back to
 * asking the server, and then only after the caller's debounce. That is what
 * keeps a 4-option company filter from making network calls at all.
 *
 * Selection and search text are separate state by construction: `search` lives
 * here and dies with the popup, `value` belongs to the URL. Clearing the search
 * cannot clear a selection because the two never touch.
 */

import { Check, ChevronDown, Search, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useT } from '../contexts/I18nContext';

export interface SelectOption {
  code: string;
  label: string;
}

interface SearchableSelectProps {
  id: string;
  /** The selected codes. One entry unless `multiple`. */
  value: string[];
  onChange: (next: string[]) => void;
  options: SelectOption[];
  multiple?: boolean;
  disabled?: boolean;
  placeholder?: string;
  /**
   * Total the server holds when it returned only a page of them, so the search
   * box can say what it is searching and offer to ask the server for more.
   */
  total?: number;
  truncated?: boolean;
  /** Called with the typed text when the list is truncated and the server must
   *  be asked. Already debounced by the caller. */
  onSearch?: (text: string) => void;
}

/** Case-insensitive, trimmed, partial, and against the code as well as the label. */
function matches(option: SelectOption, needle: string): boolean {
  const text = needle.trim().toLowerCase();
  if (!text) return true;
  return (
    option.code.toLowerCase().includes(text) ||
    option.label.toLowerCase().includes(text)
  );
}

export function SearchableSelect({
  id,
  value,
  onChange,
  options,
  multiple = false,
  disabled = false,
  placeholder,
  total,
  truncated = false,
  onSearch,
}: SearchableSelectProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const container = useRef<HTMLDivElement>(null);
  const searchBox = useRef<HTMLInputElement>(null);

  // Closing discards the search text but never the selection: the two are
  // different questions, and a filter that forgot what you picked because you
  // typed would be unusable.
  useEffect(() => {
    if (!open) return;
    searchBox.current?.focus();
    const onDocument = (event: MouseEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocument);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocument);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // A truncated list is only part of the answer, so the server is asked. A
  // complete one is filtered where it already is.
  useEffect(() => {
    if (truncated && onSearch) onSearch(search);
  }, [search, truncated, onSearch]);

  const shown = useMemo(
    () => (truncated ? options : options.filter((o) => matches(o, search))),
    [options, search, truncated],
  );

  const selected = new Set(value);
  const label = (() => {
    if (value.length === 0) return placeholder ?? t('common.all');
    if (value.length === 1) {
      return options.find((o) => o.code === value[0])?.label ?? value[0];
    }
    return t('filters.nSelected', { count: String(value.length) });
  })();

  const choose = (code: string) => {
    if (!multiple) {
      onChange(selected.has(code) ? [] : [code]);
      setOpen(false);
      return;
    }
    const next = new Set(selected);
    if (next.has(code)) next.delete(code);
    else next.add(code);
    onChange([...next]);
  };

  return (
    <div className="relative" ref={container}>
      <button
        id={id}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((was) => !was)}
        className="input flex w-full items-center justify-between gap-2 py-1.5 text-left disabled:opacity-60"
      >
        <span className={`truncate ${value.length ? '' : 'text-slate-400'}`}>
          {label}
        </span>
        <ChevronDown size={14} className="shrink-0 text-slate-400" />
      </button>

      {open && (
        <div
          role="listbox"
          /*
            The floor only applies from `sm` up.

            `min-w-[14rem]` exists so a control in a narrow rail still opens a
            list wide enough to read a customer name in. Below `sm` it did the
            opposite: in a two-column filter grid on a 320px phone the cell was
            135px and the panel 224px, so a control in the right-hand column
            opened a list that ran off the right edge of the screen. Below `sm`
            the panel is exactly as wide as its control, which is safe at every
            width because the control is itself inside the page — and the filter
            grid is one column there, so that control is the full width of the
            card anyway.
          */
          className="absolute z-30 mt-1 w-full min-w-0 rounded-lg border border-slate-200 bg-white shadow-lg sm:min-w-[14rem] dark:border-slate-700 dark:bg-slate-900"
        >
          <div className="relative border-b border-slate-100 p-2 dark:border-slate-800">
            <Search
              size={13}
              className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-slate-400"
            />
            <input
              ref={searchBox}
              className="input py-1 pl-7 pr-7 text-xs"
              value={search}
              placeholder={t('filters.searchPlaceholder')}
              aria-label={t('filters.searchPlaceholder')}
              onChange={(event) => setSearch(event.target.value)}
            />
            {search && (
              // Clears the text and nothing else — the selection is untouched,
              // which is the distinction §12 of the specification turns on.
              <button
                type="button"
                aria-label={t('filters.clearSearch')}
                onClick={() => {
                  setSearch('');
                  searchBox.current?.focus();
                }}
                className="absolute right-4 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
              >
                <X size={13} />
              </button>
            )}
          </div>

          {multiple && shown.length > 0 && (
            <div className="flex items-center justify-between border-b border-slate-100 px-2 py-1 text-[11px] dark:border-slate-800">
              {/* Names what it will do, because after a search "all" means the
                  matches and not the master. */}
              <button
                type="button"
                className="text-brand-600 hover:underline dark:text-brand-400"
                onClick={() =>
                  onChange([...new Set([...value, ...shown.map((o) => o.code)])])
                }
              >
                {search
                  ? t('filters.selectAllMatching', { count: String(shown.length) })
                  : t('filters.selectAll')}
              </button>
              {value.length > 0 && (
                <button
                  type="button"
                  className="text-slate-500 hover:underline"
                  onClick={() => onChange([])}
                >
                  {t('filters.clearSelection')}
                </button>
              )}
            </div>
          )}

          <ul className="max-h-56 overflow-y-auto py-1">
            {shown.length === 0 && (
              <li className="px-3 py-3 text-center text-xs text-slate-400">
                {t('filters.noResults')}
              </li>
            )}
            {!multiple && !search && (
              <li>
                <button
                  type="button"
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-slate-50 dark:hover:bg-slate-800"
                  onClick={() => {
                    onChange([]);
                    setOpen(false);
                  }}
                >
                  <span className="w-3.5" />
                  <span className="text-slate-500">{t('common.all')}</span>
                </button>
              </li>
            )}
            {shown.map((option) => (
              <li key={option.code}>
                <button
                  type="button"
                  role="option"
                  aria-selected={selected.has(option.code)}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-slate-50 dark:hover:bg-slate-800"
                  onClick={() => choose(option.code)}
                >
                  <span className="w-3.5 shrink-0">
                    {selected.has(option.code) && (
                      <Check size={13} className="text-brand-600 dark:text-brand-400" />
                    )}
                  </span>
                  <span className="truncate">{option.label}</span>
                </button>
              </li>
            ))}
          </ul>

          {(search || truncated) && shown.length > 0 && (
            <p className="border-t border-slate-100 px-3 py-1 text-[11px] text-slate-400 dark:border-slate-800">
              {t('common.showing')} {shown.length} {t('common.of')}{' '}
              {truncated ? (total ?? shown.length) : options.length}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
