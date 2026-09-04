/**
 * The hover tooltip: an entity's name and the figures its layer says to show.
 *
 * Rendered by React over the canvas rather than through MapLibre's popup, so
 * the fields, their order and their formatting come from the layer's
 * configuration and the metric catalogue exactly as everywhere else on the
 * page — nothing about the tooltip is hard-coded. It hides on any map
 * movement and reappears on the next hover, which is simpler than following
 * the point and reads as a tooltip should.
 */

import type { MapHover } from './useLayerRenderer';
import type { MapLayerData, MapMetricInfo } from '../../types/api';
import { formatMetric, metricByKey } from './mapMetrics';

const WIDTH = 240;

export function MapTooltip({
  hover,
  layers,
  metrics,
  containerWidth,
}: {
  hover: MapHover | null;
  layers: MapLayerData[];
  metrics: MapMetricInfo[];
  containerWidth: number;
}) {
  if (!hover) return null;
  const layer = layers.find((candidate) => candidate.level === hover.level);
  if (!layer) return null;
  const { properties } = hover;
  const flip = hover.point.x + WIDTH + 24 > containerWidth;
  const left = flip ? hover.point.x - WIDTH - 12 : hover.point.x + 12;
  const top = hover.point.y + 12;

  return (
    <div
      role="tooltip"
      className="pointer-events-none absolute z-20 rounded-lg border border-slate-200 bg-white/95 p-3 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-900/95"
      style={{ left, top, width: WIDTH }}
    >
      <p className="text-[10px] font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {layer.layer.level_label}
      </p>
      <p className="mb-2 text-sm font-semibold text-slate-900 dark:text-slate-50">{properties.name}</p>
      <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1">
        {layer.layer.tooltip_fields.map((field) => {
          if (field === 'code') {
            return (
              <FieldRow key={field} label="Code" value={properties.code} />
            );
          }
          const metric = metricByKey(metrics, field);
          const value = properties[(metric?.field ?? field) as keyof typeof properties];
          return (
            <FieldRow
              key={field}
              label={metric?.label ?? field}
              value={formatMetric(metric, typeof value === 'number' ? value : null)}
            />
          );
        })}
      </dl>
    </div>
  );
}

function FieldRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="text-right font-medium tabular-nums text-slate-800 dark:text-slate-100">{value}</dd>
    </>
  );
}
