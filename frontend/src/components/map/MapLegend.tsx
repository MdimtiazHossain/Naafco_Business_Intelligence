/**
 * The legend for one layer: what its colours mean and what its sizes mean.
 *
 * Built from the layer's effective style and its own data, so it changes with
 * the configuration by construction: a changed threshold relabels the bands
 * on the server and the legend prints what it is sent; a sequential layer's
 * classes are the quantile breaks of the points actually drawn. With several
 * layers on one map the legend explains the *active* one and offers the rest
 * by name, rather than stacking four legends over the basemap.
 */

import { useT } from '../../contexts/I18nContext';
import type { MapLayerData, MapMetricInfo } from '../../types/api';
import { formatMetric, metricByKey } from './mapMetrics';

export interface MapLegendProps {
  layers: MapLayerData[];
  activeLevel: string;
  onActiveLevel: (level: string) => void;
  metrics: MapMetricInfo[];
}

interface Swatch {
  color: string;
  label: string;
}

function colourSwatches(layer: MapLayerData, metrics: MapMetricInfo[], t: ReturnType<typeof useT>): Swatch[] {
  const { style } = layer.layer;
  const metric = metricByKey(metrics, layer.layer.effective_color_metric);
  if (layer.layer.color_mode === 'bands') {
    return style.bands.map((band) => ({ color: band.color, label: band.label }));
  }
  if (layer.layer.color_mode === 'diverging') {
    return [
      { color: style.diverging.positive, label: t('map.legendPositive') },
      { color: style.diverging.neutral, label: t('map.legendZero') },
      { color: style.diverging.negative, label: t('map.legendNegative') },
    ];
  }
  const breaks = layer.breaks[layer.layer.effective_color_metric] ?? [];
  const colours = style.sequential.slice(-(breaks.length + 1));
  if (breaks.length === 0) {
    return [{ color: colours[0], label: metric?.label ?? layer.layer.effective_color_metric }];
  }
  const swatches: Swatch[] = [];
  for (let index = breaks.length; index >= 0; index -= 1) {
    const lower = index > 0 ? breaks[index - 1] : null;
    const upper = index < breaks.length ? breaks[index] : null;
    let label: string;
    if (lower === null) label = t('map.legendBelow', { value: formatMetric(metric, upper) });
    else if (upper === null) label = t('map.legendAbove', { value: formatMetric(metric, lower) });
    else label = t('map.legendBetween', { low: formatMetric(metric, lower), high: formatMetric(metric, upper) });
    swatches.push({ color: colours[index], label });
  }
  return swatches;
}

export function MapLegend({ layers, activeLevel, onActiveLevel, metrics }: MapLegendProps) {
  const t = useT();
  const layer = layers.find((candidate) => candidate.level === activeLevel) ?? layers[layers.length - 1];
  if (!layer) return null;
  const colourMetric = metricByKey(metrics, layer.layer.effective_color_metric);
  const sizeMetric = metricByKey(metrics, layer.layer.effective_size_metric);
  const extent = layer.extents[layer.layer.effective_size_metric];
  const swatches = colourSwatches(layer, metrics, t);
  const [smallest, largest] = layer.layer.style.radius;

  return (
    <div
      className="absolute bottom-8 left-3 z-10 w-56 rounded-lg border border-slate-200 bg-white/95 p-3 text-xs shadow dark:border-slate-700 dark:bg-slate-900/95"
      aria-label={t('map.legend')}
    >
      {layers.length > 1 ? (
        <select
          aria-label={t('map.activeLayer')}
          className="input mb-2 w-full py-1 text-xs"
          value={layer.level}
          onChange={(event) => onActiveLevel(event.target.value)}
        >
          {layers.map((option) => (
            <option key={option.level} value={option.level}>
              {option.layer.layer_name}
            </option>
          ))}
        </select>
      ) : (
        <p className="mb-2 text-sm font-semibold text-slate-800 dark:text-slate-100">{layer.layer.layer_name}</p>
      )}

      <p className="mb-1 font-medium text-slate-600 dark:text-slate-300">
        {t('map.colorBy')}: {colourMetric?.label ?? layer.layer.effective_color_metric}
      </p>
      <ul className="mb-2 space-y-0.5">
        {swatches.map((swatch) => (
          <li key={swatch.label} className="flex items-center gap-2 text-slate-700 dark:text-slate-200">
            <span
              className="inline-block h-3 w-3 shrink-0 rounded-full border border-white/70"
              style={{ backgroundColor: swatch.color }}
            />
            {swatch.label}
          </li>
        ))}
        <li className="flex items-center gap-2 text-slate-500 dark:text-slate-400">
          <span
            className="inline-block h-3 w-3 shrink-0 rounded-full border border-white/70"
            style={{ backgroundColor: layer.layer.style.no_data_color }}
          />
          {t('map.noData')}
        </li>
      </ul>

      <p className="mb-1 font-medium text-slate-600 dark:text-slate-300">
        {t('map.sizeBy')}: {sizeMetric?.label ?? layer.layer.effective_size_metric}
      </p>
      {extent ? (
        <div className="flex items-center gap-2 text-slate-700 dark:text-slate-200">
          <span
            className="inline-block shrink-0 rounded-full bg-slate-400"
            style={{ width: smallest * 2, height: smallest * 2 }}
          />
          <span className="tabular-nums">{formatMetric(sizeMetric, extent.min)}</span>
          <span className="text-slate-400">–</span>
          <span
            className="inline-block shrink-0 rounded-full bg-slate-400"
            style={{ width: Math.min(largest, 14) * 2, height: Math.min(largest, 14) * 2 }}
          />
          <span className="tabular-nums">{formatMetric(sizeMetric, extent.max)}</span>
        </div>
      ) : (
        <p className="text-slate-500 dark:text-slate-400">{t('map.noData')}</p>
      )}
    </div>
  );
}
