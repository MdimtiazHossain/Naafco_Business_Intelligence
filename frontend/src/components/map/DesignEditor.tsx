/**
 * Create or edit a map design: its name, description, basemap and default
 * metric — and, for a new design, which levels it starts with.
 *
 * Everything offered comes from `GET /api/map/config`: the basemaps the
 * deployment configured, the metrics the backend declares, the levels the
 * map can draw. A refusal from the server is shown as the server phrased it,
 * because "a design named X already exists" is the whole value of it.
 */

import { Loader2 } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useT } from '../../contexts/I18nContext';
import type {
  MapConfig, MapDesign, MapLayerInput, MapPurpose,
} from '../../types/api';
import { Modal } from '../Modal';
import { refusalMessage } from './mapErrors';
import { useDesignMutations } from './mapMutations';

export interface DesignEditorProps {
  open: boolean;
  config: MapConfig;
  /** The design being edited; absent when creating one. */
  design?: MapDesign;
  /**
   * Which map a *new* design composes — the open tab's, never a constant.
   *
   * Ignored when editing: `purpose` is fixed at creation, for the reason
   * `app.map.designs.update_design` gives. It is sent on create because the
   * server defaults it to `analysis`, so a design created from the Area
   * Demarcation drawer without it would be saved to the other map and
   * disappear from the list the composer was looking at.
   */
  purpose: MapPurpose;
  onClose: () => void;
  onSaved: (design: MapDesign) => void;
}

/** Levels an analysis design starts with drawn; the rest are listed but off. */
const INITIAL_VISIBLE = new Set(['zone', 'region', 'area', 'territory', 'sub_territory']);

/** Points above which a new customer layer clusters, matching the seed. */
const CUSTOMER_CLUSTER_AT = 200;

export function DesignEditor({ open, config, design, purpose, onClose, onSaved }: DesignEditorProps) {
  const t = useT();
  const mutations = useDesignMutations();
  const creating = design === undefined;

  /**
   * The levels a new design may start with, and which are ticked.
   *
   * **Every level for a demarcation design, not the promoted seven.** That map
   * accounts for every row in `map_entity_locations` — the acid test is
   * `drawn + derived == stored` — so a design that quietly omitted Company,
   * Business Unit, Sales Line and Sales Force would break that partition for
   * whoever opened it. Revision `0036_demarcation_all_levels` exists because
   * the seeded design did exactly that.
   *
   * The analysis map is the opposite case and keeps the shorter list: there a
   * hidden level is one fewer thing competing for the eye on a map of figures.
   */
  const offered = useMemo(
    () => (purpose === 'demarcation'
      ? config.levels.map((level) => level.key)
      : config.promoted_levels),
    [config.levels, config.promoted_levels, purpose],
  );
  const initial = useMemo(
    () => (purpose === 'demarcation'
      ? offered
      : offered.filter((level) => INITIAL_VISIBLE.has(level))),
    [offered, purpose],
  );

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [basemap, setBasemap] = useState(config.default_basemap);
  const [defaultMetric, setDefaultMetric] = useState(config.defaults.metric);
  const [levels, setLevels] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setName(design?.name ?? '');
    setDescription(design?.description ?? '');
    setBasemap(design?.basemap ?? config.default_basemap);
    setDefaultMetric(design?.default_metric ?? config.defaults.metric);
    setLevels([...initial]);
  }, [open, design, config, initial]);

  const busy = mutations.create.isPending || mutations.update.isPending;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      if (creating) {
        const layers: MapLayerInput[] = offered
          .filter((level) => levels.includes(level))
          .map((level) => ({
            point_level: level,
            is_visible: true,
            cluster_at: level === 'customer' ? CUSTOMER_CLUSTER_AT : null,
          }));
        const saved = await mutations.create.mutateAsync({
          name: name.trim(),
          description: description.trim() || null,
          basemap,
          default_metric: defaultMetric,
          purpose,
          layers,
        });
        onSaved(saved);
      } else {
        const saved = await mutations.update.mutateAsync({
          designId: design.design_id,
          body: {
            name: name.trim(),
            description: description.trim() || null,
            basemap,
            default_metric: defaultMetric,
          },
        });
        onSaved(saved);
      }
    } catch (failure) {
      setError(refusalMessage(failure, t('common.error')));
    }
  }

  const levelLabel = (key: string) => config.levels.find((level) => level.key === key)?.label ?? key;

  return (
    <Modal
      open={open}
      size="lg"
      title={creating ? t('map.newDesign') : t('map.editDesign')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" form="map-design-form" className="btn-primary" disabled={busy || !name.trim() || (creating && levels.length === 0)}>
            {busy && <Loader2 size={14} className="animate-spin" />}
            {t('map.saveDesign')}
          </button>
        </>
      }
    >
      <form id="map-design-form" onSubmit={submit} className="space-y-3" noValidate>
        {error && (
          <p role="alert" className="rounded border-l-4 border-l-red-500 bg-red-50 p-2 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300">
            {error}
          </p>
        )}

        <div>
          <label htmlFor="design-name" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
            {t('map.designName')}<span className="ml-0.5 text-red-500">*</span>
          </label>
          <input id="design-name" className="input" value={name} maxLength={128}
                 onChange={(event) => setName(event.target.value)} />
        </div>

        <div>
          <label htmlFor="design-description" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
            {t('map.designDescription')}
          </label>
          <input id="design-description" className="input" value={description} maxLength={512}
                 onChange={(event) => setDescription(event.target.value)} />
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          {config.basemaps.length > 1 && (
            <div>
              <label htmlFor="design-basemap" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
                {t('map.basemap')}
              </label>
              <select id="design-basemap" className="input" value={basemap}
                      onChange={(event) => setBasemap(event.target.value)}>
                {config.basemaps.map((option) => (
                  <option key={option.key} value={option.key}>{option.label}</option>
                ))}
              </select>
            </div>
          )}
          <div>
            <label htmlFor="design-metric" className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('map.defaultMetric')}
            </label>
            <select id="design-metric" className="input" value={defaultMetric}
                    onChange={(event) => setDefaultMetric(event.target.value)}>
              {config.metrics.map((metric) => (
                <option key={metric.key} value={metric.key}>{metric.label}</option>
              ))}
            </select>
          </div>
        </div>

        {creating && (
          <fieldset>
            <legend className="mb-1 text-xs font-medium text-slate-600 dark:text-slate-300">
              {t('map.initialLayers')}
            </legend>
            <div className="grid gap-1 sm:grid-cols-2">
              {offered.map((level) => (
                <label key={level} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={levels.includes(level)}
                    onChange={(event) =>
                      setLevels((previous) =>
                        event.target.checked
                          ? [...previous, level]
                          : previous.filter((candidate) => candidate !== level),
                      )
                    }
                  />
                  {levelLabel(level)}
                </label>
              ))}
            </div>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{t('map.initialLayersHint')}</p>
          </fieldset>
        )}
      </form>
    </Modal>
  );
}
