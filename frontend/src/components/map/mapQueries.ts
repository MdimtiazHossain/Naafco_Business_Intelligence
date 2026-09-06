/**
 * The map's data, as React Query hooks.
 *
 * **One request per layer, in parallel.** A design with five visible layers
 * is five calls to `GET /api/map/data`, not one: each layer is three
 * aggregates over the sales and target facts, so a single call for five
 * layers waits for the slowest before the first can paint, and a reader
 * toggling one layer would refetch all five. Fetched separately, the zone
 * layer is on screen while the customer layer is still aggregating, a toggle
 * refetches the layer it toggled, and React Query's cache keeps a layer that
 * was hidden and shown again from being asked for twice.
 */

import { useQueries, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { mapService, type MapDataQuery } from '../../services';
import type {
  GlobalFilters,
  MapDataResponse,
  MapLayerData,
  MapPurpose,
} from '../../types/api';

export function useMapConfig() {
  return useQuery({
    queryKey: ['map-config'],
    queryFn: () => mapService.config(),
    staleTime: 60 * 60 * 1000,
  });
}

export function useMapDesigns(includeInactive = false,
                              purpose: MapPurpose = 'analysis') {
  return useQuery({
    queryKey: ['map-designs', includeInactive, purpose],
    queryFn: () => mapService.designs(includeInactive, purpose),
  });
}

/**
 * Every placed coordinate of the requested layers — the Area Demarcation tab.
 *
 * **One request for every level**, which is the opposite of `useMapLayers`
 * below and deliberately so: that one splits because each layer is three
 * aggregates over the sales and target facts, and this one reads an index over
 * a table with a four-figure row count. Splitting it would buy a round trip per
 * level to save nothing, and a layer toggle would refetch what is already held.
 *
 * The query key carries the level list for that reason: toggling a layer is a
 * different request here, where on the analysis map it is one more request
 * beside the ones already cached.
 */
export function useMapLocations(
  designId: number | undefined,
  levels: string[],
  query: GlobalFilters = {},
) {
  return useQuery({
    queryKey: ['map-locations', designId ?? null, [...levels].sort(), query],
    queryFn: () => mapService.locations({ ...query, design_id: designId, levels }),
    enabled: designId !== undefined && levels.length > 0,
  });
}

export interface MapLayersState {
  /** Loaded layers, in the order they were asked for. */
  layers: MapLayerData[];
  /** The first loaded response, for the period label and the design it drew. */
  first: MapDataResponse | undefined;
  /** True while any requested layer is still loading. */
  isLoading: boolean;
  /** True while some layers are in and others are not. */
  isPartial: boolean;
  /** The first error, if any layer failed. */
  error: unknown;
  /** True when every requested layer has answered and none drew a point. */
  isEmpty: boolean;
  refetch: () => void;
}

/**
 * The requested layers of one design, for one period, metric and filter set.
 *
 * `enabled` waits for a design: asking for "the default design's visible
 * layers" before the design list has arrived would fetch nothing useful and
 * then fetch again.
 */
export function useMapLayers(
  designId: number | undefined,
  levels: string[],
  query: MapDataQuery,
  metric: string | undefined,
): MapLayersState {
  const results = useQueries({
    queries: levels.map((level) => ({
      queryKey: ['map-layer', designId ?? null, level, metric ?? null, query],
      queryFn: () =>
        mapService.data({
          ...query,
          design_id: designId,
          levels: [level],
          ...(metric ? { metric } : {}),
        }),
      enabled: designId !== undefined,
    })),
  });

  return useMemo(() => {
    const loaded = results.filter((result) => result.data !== undefined);
    const layers = loaded.flatMap((result) => result.data?.layers ?? []);
    const isLoading = results.some((result) => result.isLoading);
    const failed = results.find((result) => result.error);
    return {
      layers,
      first: loaded[0]?.data,
      isLoading,
      isPartial: loaded.length > 0 && loaded.length < results.length,
      error: failed?.error,
      isEmpty:
        results.length > 0
        && loaded.length === results.length
        && layers.every((layer) => layer.features.features.length === 0),
      refetch: () => {
        results.forEach((result) => {
          void result.refetch();
        });
      },
    };
  }, [results]);
}
