/**
 * Add or edit one layer of a design: what it draws, how, and what it shows.
 *
 * The form offers only what the level can honour. View modes come from the
 * level's own `view_modes` — a level with no boundary source gets Point and a
 * sentence saying why, not two disabled buttons — and every metric list is
 * the catalogue minus the metrics that have no meaning at this level. The
 * whole layer list is sent back on save, in order, so the server validates
 * the design as it will stand; a refusal names the field to fix.
 */

import { Loader2, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useT } from '../../contexts/I18nContext';
import type {
  MapConfig,
  MapDesign,
  MapLayerConfig,
  MapLayerInput,
  MapViewMode,
} from '../../types/api';
import { Modal } from '../Modal';
import { refusalMessage } from './mapErrors';
import { designLayersInput, useDesignMutations } from './mapMutations';

export interface LayerEditorProps {
  open: boolean;
  config: MapConfig;
  design: MapDesign;
  /** The layer being edited; absent when adding one. */
  layer?: MapLayerConfig;
  onClose: () => void;
  onSaved: (design: MapDesign) => void;
}

const INHERIT = '';
const MAX_ZOOM = 22;

function parseThresholds(values: [string, string, string]): number[] | null {
  if (values.every((value) => value.trim() === '')) return null;
  return values.map((value) => Number(value));
}

export function LayerEditor({ open, config, design, layer, onClose, onSaved }: LayerEditorProps) {
  const t = useT();
  const mutations = useDesignMutations();
  const adding = layer === undefined;

  const usedLevels = useMemo(
    () => new Set(design.layers.map((candidate) => candidate.point_level)),
    [design],
  );
  const availableLevels = useMemo(
    () => config.levels.filter((level) => adding ? !usedLevels.has(level.key) : level.key === layer.point_level),
    [adding, config.levels, layer, usedLevels],
  );

  const [level, setLevel] = useState('');
  const [layerName, setLayerName] = useState('');
  const [viewMode, setViewMode] = useState<MapViewMode>('point');
  const [metric, setMetric] = useState(INHERIT);
  const [colorMetric, setColorMetric] = useState(INHERIT);
  const [sizeMetric, setSizeMetric] = useState(INHERIT);
  const [labelField, setLabelField] = useState('name');
  const [showLabel, setShowLabel] = useState(false);
  const [labelMinZoom, setLabelMinZoom] = useState('8');
  const [tooltipInherit, setTooltipInherit] = useState(true);
  const [tooltipFields, setTooltipFields] = useState<string[]>([]);
  const [visible, setVisible] = useState(true);
  const [clusterAt, setClusterAt] = useState('');
  const [minZoom, setMinZoom] = useState('0');
  const [thresholds, setThresholds] = useState<[string, string, string]>(['', '', '']);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    const first = availableLevels[0]?.key ?? '';
    setLevel(layer?.point_level ?? first);
    setLayerName(layer?.layer_name ?? '');
    setViewMode(layer?.view_mode ?? 'point');
    setMetric(layer?.metric ?? INHERIT);
    setColorMetric(layer?.color_metric ?? INHERIT);
    setSizeMetric(layer?.size_metric ?? INHERIT);
    setLabelField(layer?.label_field ?? 'name');
    setShowLabel(layer?.show_label ?? false);
    setLabelMinZoom(String(layer?.label_min_zoom ?? 8));
    setTooltipInherit(layer?.tooltip_inherited ?? true);
    setTooltipFields(layer?.tooltip_fields ?? config.defaults.tooltip_fields);
    setVisible(layer?.is_visible ?? true);
    setClusterAt(layer?.cluster_at === null || layer?.cluster_at === undefined ? '' : String(layer.cluster_at));
    setMinZoom(String(layer?.min_zoom ?? 0));
    const stored = (layer?.style_config as { thresholds?: number[] } | null)?.thresholds;
    setThresholds(stored && stored.length === 3
      ? [String(stored[0]), String(stored[1]), String(stored[2])]
      : ['', '', '']);
  }, [open, layer, availableLevels, config.defaults.tooltip_fields]);

  const levelInfo = config.levels.find((candidate) => candidate.key === level);
  const metricsHere = config.metrics.filter((candidate) => !candidate.unavailable_at.includes(level));
  const metricLabel = (key: string) => config.metrics.find((candidate) => candidate.key === key)?.label ?? key;
  const busy = mutations.replaceLayers.isPending;

  function build(): MapLayerInput {
    const parsedThresholds = parseThresholds(thresholds);
    const otherStyle = { ...(layer?.style_config ?? {}) } as Record<string, unknown>;
    delete otherStyle.thresholds;
    const style = parsedThresholds ? { ...otherStyle, thresholds: parsedThresholds } : otherStyle;
    return {
      point_level: level,
      layer_name: layerName.trim() || null,
      view_mode: viewMode,
      metric: metric || null,
      color_metric: colorMetric || null,
      size_metric: sizeMetric || null,
      is_visible: visible,
      min_zoom: Number(minZoom),
      cluster_at: clusterAt.trim() === '' ? null : Number(clusterAt),
      label_field: labelField,
      show_label: showLabel,
      label_min_zoom: Number(labelMinZoom),
      tooltip_fields: tooltipInherit ? null : tooltipFields,
      style_config: Object.keys(style).length ? style : null,
    };
  }

  async function save(remove = false) {
    setError(null);
    const current = designLayersInput(design);
    let layers: MapLayerInput[];
    if (remove) {
      layers = current.filter((candidate) => candidate.point_level !== layer?.point_level);
    } else if (adding) {
      layers = [...current, build()];
    } else {
      layers = current.map((candidate) => candidate.point_level === layer?.point_level ? build() : candidate);
    }
    try {
      const saved = await mutations.replaceLayers.mutateAsync({ designId: design.design_id, layers });
      onSaved(saved);
    } catch (failure) {
      setError(refusalMessage(failure, t('common.error')));
    }
  }

  const canRemove = !adding && design.layers.length > 1;

  return (
    <Modal
      open={open}
      size="lg"
      title={adding ? t('map.addLayer') : t('map.editLayer')}
      description={levelInfo ? levelInfo.label : undefined}
      onClose={onClose}
      footer={
        <>
          {canRemove && (
            <button type="button" className="btn-danger mr-auto" disabled={busy} onClick={() => void save(true)}>
              <Trash2 size={14} />
              {t('map.removeLayer')}
            </button>
          )}
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" form="map-layer-form" className="btn-primary" disabled={busy || !level}>
            {busy && <Loader2 size={14} className="animate-spin" />}
            {t('map.saveLayer')}
          </button>
        </>
      }
    >
      <form
        id="map-layer-form"
        className="space-y-3"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        {error && (
          <p role="alert" className="rounded border-l-4 border-l-red-500 bg-red-50 p-2 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300">
            {error}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor="layer-level" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('map.pointLevel')}
            </label>
            <select id="layer-level" className="input" value={level} disabled={!adding}
                    onChange={(event) => setLevel(event.target.value)}>
              {availableLevels.map((option) => (
                <option key={option.key} value={option.key}>{option.label}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="layer-name" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('map.layerName')}
            </label>
            <input id="layer-name" className="input" value={layerName} maxLength={128}
                   placeholder={levelInfo?.label ?? ''}
                   onChange={(event) => setLayerName(event.target.value)} />
          </div>
        </div>

        <div>
          <p className="mb-1 text-xs font-medium text-slate-600 dark:text-slate-300">{t('map.viewMode')}</p>
          {levelInfo && levelInfo.view_modes.length > 1 ? (
            <div className="flex gap-2">
              {config.view_modes
                .filter((mode) => levelInfo.view_modes.includes(mode.key))
                .map((mode) => (
                  <button
                    key={mode.key}
                    type="button"
                    className={viewMode === mode.key ? 'btn-primary' : 'btn-secondary'}
                    onClick={() => setViewMode(mode.key)}
                  >
                    {mode.label}
                  </button>
                ))}
            </div>
          ) : (
            <p className="text-sm text-slate-700 dark:text-slate-200">{t('map.viewModeOnlyPoint')}</p>
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <MetricSelect id="layer-metric" label={t('map.layerMetric')} value={metric} onChange={setMetric}
                        metrics={metricsHere.map((m) => [m.key, m.label])}
                        inheritLabel={t('map.inheritDesign', { metric: metricLabel(design.default_metric) })} />
          <MetricSelect id="layer-color" label={t('map.colorBy')} value={colorMetric} onChange={setColorMetric}
                        metrics={metricsHere.map((m) => [m.key, m.label])}
                        inheritLabel={t('map.inheritDesign', { metric: metricLabel(config.defaults.color_metric) })} />
          <MetricSelect id="layer-size" label={t('map.sizeBy')} value={sizeMetric} onChange={setSizeMetric}
                        metrics={metricsHere.map((m) => [m.key, m.label])}
                        inheritLabel={t('map.inheritDesign', { metric: metricLabel(config.defaults.size_metric) })} />
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <label htmlFor="layer-label" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('map.labelField')}
            </label>
            <select id="layer-label" className="input" value={labelField}
                    onChange={(event) => setLabelField(event.target.value)}>
              <option value="name">{t('map.labelName')}</option>
              <option value="code">{t('map.labelCode')}</option>
              {metricsHere.map((m) => (
                <option key={m.key} value={m.key}>{m.label}</option>
              ))}
            </select>
          </div>
          <label className="flex items-center gap-2 self-end pb-2 text-sm">
            <input type="checkbox" checked={showLabel} onChange={(event) => setShowLabel(event.target.checked)} />
            {t('map.showLabel')}
          </label>
          <div>
            <label htmlFor="layer-label-zoom" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('map.labelMinZoom')}
            </label>
            <input id="layer-label-zoom" type="number" min={0} max={MAX_ZOOM} className="input" value={labelMinZoom}
                   onChange={(event) => setLabelMinZoom(event.target.value)} />
          </div>
        </div>

        <fieldset>
          <legend className="mb-1 text-xs font-medium text-slate-600 dark:text-slate-300">{t('map.tooltipFields')}</legend>
          <label className="mb-1 flex items-center gap-2 text-sm">
            <input type="checkbox" checked={tooltipInherit} onChange={(event) => setTooltipInherit(event.target.checked)} />
            {t('map.tooltipInherit')}
          </label>
          {!tooltipInherit && (
            <div className="grid gap-1 sm:grid-cols-3">
              {[...metricsHere.map((m) => [m.key, m.label] as const), ['code', t('map.labelCode')] as const].map(([key, label]) => (
                <label key={key} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={tooltipFields.includes(key)}
                    onChange={(event) =>
                      setTooltipFields((previous) =>
                        event.target.checked ? [...previous, key] : previous.filter((k) => k !== key),
                      )
                    }
                  />
                  {label}
                </label>
              ))}
            </div>
          )}
        </fieldset>

        <div className="grid gap-3 sm:grid-cols-3">
          <label className="flex items-center gap-2 self-end pb-2 text-sm">
            <input type="checkbox" checked={visible} onChange={(event) => setVisible(event.target.checked)} />
            {t('map.visibleByDefault')}
          </label>
          <div>
            <label htmlFor="layer-cluster" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('map.clusterAt')}
            </label>
            <input id="layer-cluster" type="number" min={1} className="input" value={clusterAt}
                   placeholder={t('map.clusterNever')}
                   onChange={(event) => setClusterAt(event.target.value)} />
          </div>
          <div>
            <label htmlFor="layer-min-zoom" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('map.minZoom')}
            </label>
            <input id="layer-min-zoom" type="number" min={0} max={MAX_ZOOM} className="input" value={minZoom}
                   onChange={(event) => setMinZoom(event.target.value)} />
          </div>
        </div>

        <fieldset>
          <legend className="mb-1 text-xs font-medium text-slate-600 dark:text-slate-300">{t('map.thresholds')}</legend>
          <div className="grid grid-cols-3 gap-2">
            {(['good', 'medium', 'low'] as const).map((band, index) => (
              <input
                key={band}
                type="number"
                min={0}
                className="input"
                aria-label={t(`map.threshold_${band}`)}
                placeholder={String(config.style.thresholds[index])}
                value={thresholds[index]}
                onChange={(event) =>
                  setThresholds((previous) => {
                    const next: [string, string, string] = [...previous] as [string, string, string];
                    next[index] = event.target.value;
                    return next;
                  })
                }
              />
            ))}
          </div>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{t('map.thresholdsHint')}</p>
        </fieldset>
      </form>
    </Modal>
  );
}

function MetricSelect({
  id, label, value, onChange, metrics, inheritLabel,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  metrics: (readonly [string, string])[];
  inheritLabel: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
        {label}
      </label>
      <select id={id} className="input" value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">{inheritLabel}</option>
        {metrics.map(([key, name]) => (
          <option key={key} value={key}>{name}</option>
        ))}
      </select>
    </div>
  );
}
