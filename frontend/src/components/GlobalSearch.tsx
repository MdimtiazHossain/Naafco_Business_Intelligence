/**
 * Header search across master data.
 *
 * Results come from `/api/master-data/search`, which is permission-scoped: a
 * user simply never sees an entity outside their access. Choosing a result
 * applies it as a filter and opens the relevant report.
 */

import { Search, X } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useT } from '../contexts/I18nContext';
import { masterDataService } from '../services';
import { useDebounced } from '../hooks/useDebounced';

export function GlobalSearch() {
  const t = useT();
  const navigate = useNavigate();
  const [term, setTerm] = useState('');
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const debounced = useDebounced(term, 300);

  const { data, isFetching } = useQuery({
    queryKey: ['global-search', debounced],
    queryFn: () => masterDataService.search(debounced),
    enabled: debounced.trim().length >= 2,
    staleTime: 30 * 1000,
  });

  // Close when focus or a click leaves the search box.
  useEffect(() => {
    function onPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, []);

  const results = data?.results ?? [];

  function choose(result: (typeof results)[number]) {
    const params = new URLSearchParams();
    params.set(result.filter_level, result.code);
    navigate(`${result.route}?${params.toString()}`);
    setTerm('');
    setOpen(false);
  }

  return (
    <div ref={containerRef} className="relative w-full max-w-md">
      <Search
        size={16}
        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
      />
      <input
        type="search"
        className="input py-1.5 pl-9 pr-8"
        placeholder={t('common.searchPlaceholder')}
        value={term}
        onChange={(event) => {
          setTerm(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') setOpen(false);
        }}
        aria-label={t('common.search')}
      />
      {term && (
        <button
          type="button"
          onClick={() => setTerm('')}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
          aria-label={t('common.clear')}
        >
          <X size={14} />
        </button>
      )}

      {open && debounced.trim().length >= 2 && (
        <div className="absolute z-30 mt-1 max-h-80 w-full overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-900">
          {isFetching && (
            <p className="px-3 py-2 text-xs text-slate-500">{t('common.loading')}</p>
          )}
          {!isFetching && results.length === 0 && (
            <p className="px-3 py-3 text-xs text-slate-500">{t('common.noData')}</p>
          )}
          {results.map((result) => (
            <button
              key={`${result.entity_type}-${result.code}`}
              type="button"
              onClick={() => choose(result)}
              className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
            >
              <span className="min-w-0">
                <span className="block truncate font-medium text-slate-800 dark:text-slate-100">
                  {result.label}
                </span>
                <span className="block truncate text-xs text-slate-500">{result.code}</span>
              </span>
              <span className="badge shrink-0 bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                {result.type_label}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
