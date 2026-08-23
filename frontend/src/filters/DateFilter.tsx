/**
 * Period picker.
 *
 * The named periods and their resolved dates come from `/api/period-options`,
 * so "YTD" follows the company's configured financial year. The frontend never
 * computes a financial year itself.
 */

import { CalendarDays } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { dashboardService } from '../services';

export function DateFilter() {
  const t = useT();
  const { period, setPeriod } = useFilters();
  const [customOpen, setCustomOpen] = useState(period.period === 'CUSTOM');
  const [draft, setDraft] = useState({
    from: period.date_from ?? '',
    to: period.date_to ?? '',
  });

  const { data } = useQuery({
    queryKey: ['period-options'],
    queryFn: () => dashboardService.periodOptions(),
    staleTime: 60 * 60 * 1000,
  });

  const options = data?.options ?? [];

  function choose(value: string) {
    if (value === 'CUSTOM') {
      setCustomOpen(true);
      return;
    }
    setCustomOpen(false);
    setPeriod({ period: value });
  }

  function applyCustom() {
    if (!draft.from || !draft.to) return;
    setPeriod({ period: 'CUSTOM', date_from: draft.from, date_to: draft.to });
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex items-center gap-1.5 text-slate-500 dark:text-slate-400">
        <CalendarDays size={16} />
        <span className="text-xs font-medium">{t('common.period')}</span>
      </div>

      <select
        className="input w-auto min-w-[10rem] py-1.5"
        value={period.period}
        onChange={(event) => choose(event.target.value)}
        aria-label={t('common.period')}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {t(`period.${option.value}`) === `period.${option.value}`
              ? option.label
              : t(`period.${option.value}`)}
          </option>
        ))}
        <option value="CUSTOM">{t('period.CUSTOM')}</option>
      </select>

      {customOpen && (
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <label className="label" htmlFor="date-from">
              {t('common.from')}
            </label>
            <input
              id="date-from"
              type="date"
              className="input w-auto py-1.5"
              value={draft.from}
              onChange={(event) => setDraft((d) => ({ ...d, from: event.target.value }))}
            />
          </div>
          <div>
            <label className="label" htmlFor="date-to">
              {t('common.to')}
            </label>
            <input
              id="date-to"
              type="date"
              className="input w-auto py-1.5"
              value={draft.to}
              onChange={(event) => setDraft((d) => ({ ...d, to: event.target.value }))}
            />
          </div>
          <button
            type="button"
            className="btn-primary py-1.5"
            onClick={applyCustom}
            disabled={!draft.from || !draft.to}
          >
            {t('common.apply')}
          </button>
        </div>
      )}

      {data?.current_financial_year && (
        <span className="hidden text-xs text-slate-400 lg:inline dark:text-slate-500">
          {data.current_financial_year}
        </span>
      )}
    </div>
  );
}
