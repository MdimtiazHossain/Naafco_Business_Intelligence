/**
 * The selected entity: its name, where it sits, and its four headline figures.
 *
 * The figures are the point's own properties, already on the map; only the
 * names of the levels above it are fetched, from `GET /api/map/entities`,
 * because a feature carries its parent's code and nothing about the chain
 * beyond that. An entity chosen from a table that has no coordinate is shown
 * the same way, with a note that it is not on the map — it performed, and
 * its figures are as real as a drawn point's.
 */

import { useQuery } from '@tanstack/react-query';
import { MapPinOff, X } from 'lucide-react';
import { useT } from '../../contexts/I18nContext';
import { mapService } from '../../services';
import type { MapFeatureProperties, MapLayerData, MapMetricInfo, MapRankingRow } from '../../types/api';
import { formatMetric, metricByKey } from './mapMetrics';
import type { MapSelection } from './useLayerRenderer';

/** The four figures the card headlines, in the specification's order. */
const HEADLINE_METRICS = ['net_sales', 'target_amount', 'achievement', 'growth'];

/** Ancestors worth naming on the card: the business levels, not the company. */
const BREADCRUMB_LEVELS = new Set(['zone', 'region', 'area', 'unit', 'territory', 'sub_territory']);

export interface SelectedEntityCardProps {
  selection: MapSelection | null;
  layers: MapLayerData[];
  metrics: MapMetricInfo[];
  onClear: () => void;
}

function figuresFor(
  selection: MapSelection,
  layers: MapLayerData[],
): { figures: MapFeatureProperties | MapRankingRow | null; placed: boolean } {
  const layer = layers.find((candidate) => candidate.level === selection.level);
  const feature = layer?.features.features.find((f) => f.properties.code === selection.code);
  if (feature) return { figures: feature.properties, placed: true };
  const row = [...(layer?.ranking.top ?? []), ...(layer?.ranking.bottom ?? [])]
    .find((candidate) => candidate.code === selection.code);
  if (row) return { figures: row, placed: row.placed };
  return { figures: selection.properties ?? null, placed: selection.source === 'map' };
}

export function SelectedEntityCard({ selection, layers, metrics, onClear }: SelectedEntityCardProps) {
  const t = useT();
  const entity = useQuery({
    queryKey: ['map-entity', selection?.level ?? null, selection?.code ?? null],
    queryFn: () => mapService.entity(selection!.level, selection!.code),
    enabled: selection !== null,
    staleTime: 10 * 60 * 1000,
  });

  if (!selection) {
    return (
      <div className="card mb-4 p-4 text-sm text-slate-500 dark:text-slate-400">
        {t('map.selectHint')}
      </div>
    );
  }

  const { figures, placed } = figuresFor(selection, layers);
  const layer = layers.find((candidate) => candidate.level === selection.level);
  const levelLabel = entity.data?.label ?? layer?.layer.level_label ?? selection.level;
  const name = entity.data?.name ?? figures?.name ?? selection.code;
  const crumbs = (entity.data?.ancestors ?? [])
    .filter((ancestor) => BREADCRUMB_LEVELS.has(ancestor.level))
    .map((ancestor) => ancestor.name);

  return (
    <div className="card mb-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            {t('map.selected')}: {levelLabel}
          </p>
          <h2 className="mt-1 text-lg font-semibold text-slate-900 dark:text-slate-50">{name}</h2>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            <span className="font-mono">{selection.code}</span>
            {crumbs.length > 0 && <> · {crumbs.join(' • ')}</>}
          </p>
          {!placed && (
            <p className="mt-1 flex items-center gap-1 text-xs text-amber-700 dark:text-amber-400">
              <MapPinOff size={12} />
              {t('map.notPlaced')}
            </p>
          )}
        </div>
        <button type="button" className="btn-ghost py-1 text-xs" onClick={onClear}>
          <X size={14} />
          {t('map.clearSelection')}
        </button>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {HEADLINE_METRICS.map((key) => {
          const metric = metricByKey(metrics, key);
          const field = (metric?.field ?? key) as keyof MapFeatureProperties;
          const raw = figures ? figures[field as keyof typeof figures] : null;
          const value = typeof raw === 'number' ? raw : null;
          return (
            <div key={key} className="rounded-lg border border-slate-200 p-3 dark:border-slate-700">
              <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                {metric?.label ?? key}
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-50">
                {formatMetric(metric, value)}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
