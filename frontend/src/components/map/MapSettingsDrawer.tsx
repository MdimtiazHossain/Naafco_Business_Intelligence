/**
 * The Map Settings drawer: which design, which layers, which is active.
 *
 * Two audiences share it. A **reader** picks a design, switches layers on and
 * off for the view in front of them — a temporary choice that lives in the
 * URL and saves nothing — and chooses the layer the legend and tables
 * describe. Somebody holding **Map Settings** additionally composes: creates,
 * edits, duplicates, activates and deletes designs, and adds, edits and
 * reorders their layers, every one of which is saved for everybody. The two
 * halves are drawn apart, and a control whose only outcome would be a refusal
 * is absent rather than disabled: nobody sees a Delete on the system default.
 */

import {
  ArrowDown, ArrowUp, Copy, Pencil, Plus, Settings2, Star, Trash2,
} from 'lucide-react';
import { useState } from 'react';
import { useAuth } from '../../contexts/AuthContext';
import { useT } from '../../contexts/I18nContext';
import type { MapConfig, MapDesign, MapLayerConfig, MapLayerData } from '../../types/api';
import { ConfirmDialog } from '../ConfirmDialog';
import { Modal } from '../Modal';
import { DesignEditor } from './DesignEditor';
import { LayerEditor } from './LayerEditor';
import { refusalMessage } from './mapErrors';
import { designLayersInput, useDesignMutations } from './mapMutations';

export interface MapSettingsDrawerProps {
  open: boolean;
  onClose: () => void;
  config: MapConfig;
  /** Every design the caller may see; inactive ones only for a composer. */
  designs: MapDesign[];
  design: MapDesign | undefined;
  onDesignChange: (designId: number | null) => void;
  /** The levels drawn right now, and how a reader changes them. */
  levels: string[];
  onLevelsChange: (levels: string[]) => void;
  drawn: MapLayerData[];
  activeLevel: string;
  onActiveLevel: (level: string) => void;
}

export function MapSettingsDrawer({
  open, onClose, config, designs, design, onDesignChange, levels, onLevelsChange,
  drawn, activeLevel, onActiveLevel,
}: MapSettingsDrawerProps) {
  const t = useT();
  const { hasSection, can } = useAuth();
  const mutations = useDesignMutations();
  const composer = hasSection('map_settings');
  const mayCreate = composer && can('map_settings', 'CREATE');
  const mayEdit = composer && can('map_settings', 'EDIT');
  const mayDelete = composer && can('map_settings', 'DELETE');

  const [editor, setEditor] = useState<'closed' | 'create' | 'edit'>('closed');
  const [layerEditor, setLayerEditor] = useState<{ open: boolean; layer?: MapLayerConfig }>({ open: false });
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const orderedLayers = design ? [...design.layers].sort((a, b) => a.display_order - b.display_order) : [];
  const coverage = new Map(config.coverage.map((entry) => [entry.entity_type, entry]));
  const unusedLevels = config.levels.filter((level) => !orderedLayers.some((layer) => layer.point_level === level.key));

  async function run(action: () => Promise<unknown>) {
    setNotice(null);
    try {
      await action();
    } catch (failure) {
      setNotice(refusalMessage(failure, t('common.error')));
    }
  }

  function toggleLevel(level: string, on: boolean) {
    const next = on
      ? orderedLayers.map((layer) => layer.point_level).filter((key) => key === level || levels.includes(key))
      : levels.filter((key) => key !== level);
    onLevelsChange(next);
  }

  async function move(layer: MapLayerConfig, direction: -1 | 1) {
    if (!design) return;
    const inputs = designLayersInput(design);
    const index = inputs.findIndex((candidate) => candidate.point_level === layer.point_level);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= inputs.length) return;
    [inputs[index], inputs[target]] = [inputs[target], inputs[index]];
    await run(() => mutations.replaceLayers.mutateAsync({ designId: design.design_id, layers: inputs }));
  }

  return (
    <>
      <Modal
        open={open}
        placement="sheet"
        title={t('map.settingsTitle')}
        description={t('map.settingsHint')}
        onClose={onClose}
      >
        <div className="space-y-6">
          {notice && (
            <p role="alert" className="rounded border-l-4 border-l-red-500 bg-red-50 p-2 text-sm text-red-700 dark:bg-red-950/30 dark:text-red-300">
              {notice}
            </p>
          )}

          {/* ---- Design ------------------------------------------------- */}
          <section className="space-y-2">
            <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">{t('map.designSection')}</h3>
            <select
              aria-label={t('map.design')}
              className="input"
              value={design?.design_id ?? ''}
              onChange={(event) => onDesignChange(Number(event.target.value) || null)}
            >
              {designs.map((option) => (
                <option key={option.design_id} value={option.design_id}>
                  {option.name}
                  {option.is_default ? ` · ${t('map.default')}` : ''}
                  {!option.is_active ? ` · ${t('map.inactive')}` : ''}
                </option>
              ))}
            </select>
            {design && (
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {design.description ?? ''}
                {design.description ? ' · ' : ''}
                {design.is_system_default ? t('map.system') : t('map.customDesign')}
                {design.basemap_note ? ` · ${design.basemap_note}` : ''}
              </p>
            )}

            {composer && design && (
              <div className="flex flex-wrap gap-2">
                {mayCreate && (
                  <button type="button" className="btn-secondary py-1 text-xs" onClick={() => setEditor('create')}>
                    <Plus size={14} />{t('map.newDesign')}
                  </button>
                )}
                {mayEdit && (
                  <button type="button" className="btn-secondary py-1 text-xs" onClick={() => setEditor('edit')}>
                    <Pencil size={14} />{t('map.editDesign')}
                  </button>
                )}
                {mayCreate && (
                  <button
                    type="button"
                    className="btn-secondary py-1 text-xs"
                    onClick={() => void run(async () => {
                      const copy = await mutations.duplicate.mutateAsync({ designId: design.design_id });
                      onDesignChange(copy.design_id);
                    })}
                  >
                    <Copy size={14} />{t('map.duplicate')}
                  </button>
                )}
                {mayEdit && !design.is_default && design.is_active && (
                  <button
                    type="button"
                    className="btn-secondary py-1 text-xs"
                    onClick={() => void run(() => mutations.setDefault.mutateAsync(design.design_id))}
                  >
                    <Star size={14} />{t('map.setDefault')}
                  </button>
                )}
                {mayEdit && !design.is_system_default && design.is_active && (
                  <button
                    type="button"
                    className="btn-secondary py-1 text-xs"
                    onClick={() => void run(() => mutations.deactivate.mutateAsync(design.design_id))}
                  >
                    {t('map.deactivate')}
                  </button>
                )}
                {mayEdit && !design.is_active && (
                  <button
                    type="button"
                    className="btn-secondary py-1 text-xs"
                    onClick={() => void run(() => mutations.activate.mutateAsync(design.design_id))}
                  >
                    {t('map.activate')}
                  </button>
                )}
                {mayDelete && !design.is_system_default && (
                  <button type="button" className="btn-danger py-1 text-xs" onClick={() => setConfirmDelete(true)}>
                    <Trash2 size={14} />{t('map.delete')}
                  </button>
                )}
              </div>
            )}
          </section>

          {/* ---- Layers ------------------------------------------------- */}
          {design && (
            <section className="space-y-2">
              <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">{t('map.layersSection')}</h3>
              <p className="text-xs text-slate-500 dark:text-slate-400">{t('map.layersHint')}</p>
              <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 dark:divide-slate-700 dark:border-slate-700">
                {orderedLayers.map((layer, index) => {
                  const on = levels.includes(layer.point_level);
                  const placed = coverage.get(layer.point_level);
                  return (
                    <li key={layer.layer_id} className="flex items-center gap-2 px-3 py-2 text-sm">
                      <label className="flex flex-1 items-center gap-2">
                        <input
                          type="checkbox"
                          checked={on}
                          aria-label={layer.layer_name}
                          onChange={(event) => toggleLevel(layer.point_level, event.target.checked)}
                        />
                        <span className="flex-1">
                          <span className="font-medium text-slate-800 dark:text-slate-100">{layer.layer_name}</span>
                          <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
                            {layer.level_label}
                            {placed ? ` · ${t('map.placedOf', { placed: String(placed.placed), total: String(placed.total) })}` : ''}
                          </span>
                        </span>
                      </label>
                      {mayEdit && (
                        <span className="flex items-center gap-1">
                          <button type="button" className="btn-ghost p-1" aria-label={t('map.moveUp')}
                                  disabled={index === 0} onClick={() => void move(layer, -1)}>
                            <ArrowUp size={14} />
                          </button>
                          <button type="button" className="btn-ghost p-1" aria-label={t('map.moveDown')}
                                  disabled={index === orderedLayers.length - 1} onClick={() => void move(layer, 1)}>
                            <ArrowDown size={14} />
                          </button>
                          <button type="button" className="btn-ghost p-1" aria-label={`${t('map.editLayer')}: ${layer.layer_name}`}
                                  onClick={() => setLayerEditor({ open: true, layer })}>
                            <Settings2 size={14} />
                          </button>
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
              {mayEdit && unusedLevels.length > 0 && (
                <button type="button" className="btn-secondary py-1 text-xs" onClick={() => setLayerEditor({ open: true })}>
                  <Plus size={14} />{t('map.addLayer')}
                </button>
              )}
            </section>
          )}

          {/* ---- Active layer ------------------------------------------- */}
          {drawn.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">{t('map.activeLayer')}</h3>
              <select
                aria-label={t('map.activeLayer')}
                className="input"
                value={activeLevel}
                onChange={(event) => onActiveLevel(event.target.value)}
              >
                {drawn.map((layer) => (
                  <option key={layer.level} value={layer.level}>{layer.layer.layer_name}</option>
                ))}
              </select>
              <p className="text-xs text-slate-500 dark:text-slate-400">{t('map.activeLayerHint')}</p>
            </section>
          )}
        </div>
      </Modal>

      {composer && (
        <DesignEditor
          open={editor !== 'closed'}
          config={config}
          design={editor === 'edit' ? design : undefined}
          onClose={() => setEditor('closed')}
          onSaved={(saved) => {
            setEditor('closed');
            onDesignChange(saved.design_id);
          }}
        />
      )}

      {composer && design && (
        <LayerEditor
          open={layerEditor.open}
          config={config}
          design={design}
          layer={layerEditor.layer}
          onClose={() => setLayerEditor({ open: false })}
          onSaved={() => setLayerEditor({ open: false })}
        />
      )}

      {composer && design && (
        <ConfirmDialog
          open={confirmDelete}
          title={t('map.deleteDesignTitle')}
          consequence={t('map.deleteDesignConsequence')}
          recordLabel={design.name}
          confirmLabel={t('map.delete')}
          tone="danger"
          busy={mutations.remove.isPending}
          error={mutations.remove.error ? refusalMessage(mutations.remove.error, t('common.error')) : null}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => void run(async () => {
            await mutations.remove.mutateAsync(design.design_id);
            setConfirmDelete(false);
            onDesignChange(null);
          })}
        />
      )}
    </>
  );
}
