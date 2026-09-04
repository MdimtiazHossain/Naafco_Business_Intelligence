/**
 * Top and bottom performers of the active layer, by the reader's metric.
 *
 * Two ordinary `DataTable`s, so hiding, moving and resizing a column work as
 * they do on every report table, each remembering its own arrangement. The
 * rows are the backend's ranking — nothing is sorted or ranked here — and an
 * entity with no coordinate is listed like any other, marked as not being on
 * the map: it performed, and a ranking that skipped it would misstate the
 * bottom of the league.
 */

import { MapPinOff } from 'lucide-react';
import { useMemo } from 'react';
import { useT } from '../../contexts/I18nContext';
import { DataTable, type Column } from '../../tables/DataTable';
import type { MapLayerData, MapMetricInfo, MapRankingRow } from '../../types/api';
import { Section } from '../PageHeader';
import { formatMetric, metricByKey } from './mapMetrics';
import type { MapSelection } from './useLayerRenderer';

export interface TopBottomTableProps {
  layer: MapLayerData | undefined;
  metrics: MapMetricInfo[];
  onSelect: (selection: MapSelection) => void;
}

interface RankedRow extends MapRankingRow {
  rank: number;
  value: number | null;
}

export function TopBottomTable({ layer, metrics, onSelect }: TopBottomTableProps) {
  const t = useT();
  const ranking = layer?.ranking;
  const metric = ranking ? metricByKey(metrics, ranking.metric) : undefined;
  const achievement = metricByKey(metrics, 'achievement');

  const columns = useMemo<Column<RankedRow>[]>(() => [
    { key: 'rank', header: '#', width: '3rem', align: 'right' },
    {
      key: 'name',
      header: t('map.name'),
      render: (row) => (
        <span className="flex items-center gap-1.5">
          {row.name}
          {!row.placed && (
            <span title={t('map.notPlaced')} className="text-amber-600 dark:text-amber-400">
              <MapPinOff size={12} />
            </span>
          )}
        </span>
      ),
    },
    {
      key: 'value',
      header: metric?.label ?? ranking?.metric ?? '',
      align: 'right',
      render: (row) => formatMetric(metric, row.value),
    },
    {
      key: 'achievement_percent',
      header: achievement?.label ?? 'Achievement %',
      align: 'right',
      render: (row) => formatMetric(achievement, row.achievement_percent),
    },
  ], [achievement, metric, ranking?.metric, t]);

  const toRows = (rows: MapRankingRow[]): RankedRow[] =>
    rows.map((row, index) => ({
      ...row,
      rank: index + 1,
      value: ranking ? (row[ranking.field as keyof MapRankingRow] as number | null) : null,
    }));

  if (!layer || !ranking) return null;
  const levelLabel = layer.layer.level_label;
  const select = (row: RankedRow) => onSelect({ level: layer.level, code: row.code, source: 'table' });

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Section title={t('map.top', { level: levelLabel })}>
        <DataTable<RankedRow>
          rows={toRows(ranking.top)}
          columns={columns}
          tableId="map.top"
          onRowClick={select}
          emptyMessage={t('map.noRanking')}
        />
      </Section>
      <Section title={t('map.bottom', { level: levelLabel })}>
        <DataTable<RankedRow>
          rows={toRows(ranking.bottom)}
          columns={columns}
          tableId="map.bottom"
          onRowClick={select}
          emptyMessage={t('map.noBottom')}
        />
      </Section>
    </div>
  );
}
