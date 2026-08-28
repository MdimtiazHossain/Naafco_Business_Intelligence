/**
 * What a click on the map says.
 *
 * Rendered as React through a portal into MapLibre's popup container rather than
 * as an HTML string, for two reasons: a string would have to escape a customer
 * name by hand — and Bangla names go through here — and it would need its own
 * copies of the number formatting and the translations the rest of the dashboard
 * already has.
 *
 * Only fields the data model actually carries are shown. A metric the backend
 * could not compute is absent rather than rendered as zero, which is the same
 * rule every other report on this platform follows.
 */

import { useT } from '../../contexts/I18nContext';
import { formatCount } from '../../utils/format';
import type { AreaMetric } from './BusinessMap';
import type { EntityFeatureProperties } from './businessGeoJson';
import type { BoundaryLevelKey } from './mapConfig';

export type PopupSubject =
  | { kind: 'entity'; entity: EntityFeatureProperties }
  | {
      kind: 'area';
      level: BoundaryLevelKey;
      code: string;
      name: string;
      properties: Record<string, unknown>;
      metric?: AreaMetric;
    };

export interface MapPopupProps {
  subject: PopupSubject;
  formatValue: (value: number | null) => string;
}

export function MapPopup({ subject, formatValue }: MapPopupProps) {
  const t = useT();

  if (subject.kind === 'entity') {
    const entity = subject.entity;
    return (
      <div className="min-w-[11rem] space-y-1 p-1 text-slate-900 dark:text-slate-100">
        <p className="text-[10px] uppercase tracking-wide text-slate-400">
          {entity.entityType?.replace(/_/g, ' ')}
        </p>
        <p className="text-sm font-semibold leading-tight">{entity.name}</p>
        <p className="font-mono text-[11px] text-slate-500">{entity.code}</p>

        {entity.parentCode && (
          <Row
            label={entity.parentType?.replace(/_/g, ' ') ?? t('map.parent')}
            value={entity.parentCode}
          />
        )}

        <p className="pt-1 text-base font-semibold tabular-nums">
          {formatValue(entity.value)}
        </p>

        <p className="text-[10px] tabular-nums text-slate-400">
          {entity.latitude.toFixed(4)}, {entity.longitude.toFixed(4)}
        </p>
        {entity.locationSource && (
          <p className="text-[10px] text-slate-400">
            {t('map.locationSource', { source: entity.locationSource })}
          </p>
        )}
      </div>
    );
  }

  const { name, code, properties, metric } = subject;
  return (
    <div className="min-w-[11rem] space-y-1 p-1 text-slate-900 dark:text-slate-100">
      <p className="text-[10px] uppercase tracking-wide text-slate-400">
        {t(`map.level${capitalise(subject.level)}`)}
      </p>
      <p className="text-sm font-semibold leading-tight">{name}</p>
      <p className="font-mono text-[11px] text-slate-500">{code}</p>

      {typeof properties.district_name === 'string' && (
        <Row label={t('map.levelDistrict')} value={properties.district_name} />
      )}
      {typeof properties.division_name === 'string' && (
        <Row label={t('map.levelDivision')} value={properties.division_name} />
      )}

      {/* The metric comes from the API keyed on this code; the geometry came
          from a local file. An area the server has nothing to say about shows
          no metric rather than a zero it would be read as. */}
      {metric ? (
        <>
          <div className="mt-1 flex justify-between gap-3 border-t border-slate-200 pt-1 dark:border-slate-700">
            <span className="text-slate-500">{t('map.stock')}</span>
            <span className="font-semibold tabular-nums">{formatCount(metric.stock)}</span>
          </div>
          {metric.stock_source === 'default' && (
            <p className="text-[10px] italic leading-snug text-slate-400">
              {t('map.stockDefault')}
            </p>
          )}
        </>
      ) : (
        <p className="mt-1 border-t border-slate-200 pt-1 text-[10px] text-slate-400 dark:border-slate-700">
          {t('map.noAreaMetric')}
        </p>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3 text-[11px]">
      <span className="text-slate-500">{label}</span>
      <span className="truncate">{value}</span>
    </div>
  );
}

function capitalise(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
