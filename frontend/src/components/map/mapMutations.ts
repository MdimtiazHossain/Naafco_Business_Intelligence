/**
 * Every write the settings drawer makes, and what it invalidates.
 *
 * A design change invalidates the design list *and* every cached layer,
 * because a layer's configuration rides along in each data response: a
 * re-coloured layer must be redrawn from a fresh answer, not from the one
 * cached under the old design.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { mapService } from '../../services';
import type {
  MapDesign,
  MapDesignInput,
  MapDesignUpdate,
  MapLayerConfig,
  MapLayerInput,
} from '../../types/api';

/** A stored layer as the API accepts it back: inherited values stay inherited. */
export function layerInput(layer: MapLayerConfig): MapLayerInput {
  return {
    point_level: layer.point_level,
    layer_name: layer.layer_name,
    view_mode: layer.view_mode,
    metric: layer.metric,
    color_metric: layer.color_metric,
    size_metric: layer.size_metric,
    is_visible: layer.is_visible,
    min_zoom: layer.min_zoom,
    cluster_at: layer.cluster_at,
    label_field: layer.label_field,
    show_label: layer.show_label,
    label_min_zoom: layer.label_min_zoom,
    tooltip_fields: layer.tooltip_inherited ? null : layer.tooltip_fields,
    style_config: layer.style_config,
  };
}

/** The whole layer list of a design, in display order, as the API accepts it. */
export function designLayersInput(design: MapDesign): MapLayerInput[] {
  return [...design.layers]
    .sort((a, b) => a.display_order - b.display_order)
    .map(layerInput);
}

export function useDesignMutations() {
  const queryClient = useQueryClient();
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['map-designs'] });
    void queryClient.invalidateQueries({ queryKey: ['map-layer'] });
  };

  const create = useMutation({
    mutationFn: (body: MapDesignInput) => mapService.createDesign(body),
    onSuccess: invalidate,
  });
  const update = useMutation({
    mutationFn: ({ designId, body }: { designId: number; body: MapDesignUpdate }) =>
      mapService.updateDesign(designId, body),
    onSuccess: invalidate,
  });
  const replaceLayers = useMutation({
    mutationFn: ({ designId, layers }: { designId: number; layers: MapLayerInput[] }) =>
      mapService.replaceLayers(designId, layers),
    onSuccess: invalidate,
  });
  const duplicate = useMutation({
    mutationFn: ({ designId, name }: { designId: number; name?: string }) =>
      mapService.duplicateDesign(designId, name),
    onSuccess: invalidate,
  });
  const setDefault = useMutation({
    mutationFn: (designId: number) => mapService.setDefault(designId),
    onSuccess: invalidate,
  });
  const activate = useMutation({
    mutationFn: (designId: number) => mapService.activate(designId),
    onSuccess: invalidate,
  });
  const deactivate = useMutation({
    mutationFn: (designId: number) => mapService.deactivate(designId),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (designId: number) => mapService.deleteDesign(designId),
    onSuccess: invalidate,
  });

  return { create, update, replaceLayers, duplicate, setDefault, activate, deactivate, remove };
}
