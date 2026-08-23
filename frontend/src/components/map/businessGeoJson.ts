/**
 * The business data adapter: application state → GeoJSON → MapLibre.
 *
 * This is the only place business records become map geometry, and it is a pure
 * function of what the API already returned. The map does not fetch business
 * data of its own — `/api/map/entities` is the same permission-filtered query
 * the rest of the dashboard reads, so a regional manager's map is their region
 * for the same reason their sales page is.
 *
 * Everything is emitted as one `FeatureCollection` for one source. The previous
 * implementation created a DOM node per marker, which is fine for a dozen and
 * unusable for two thousand customers; a GeoJSON source with a symbol layer
 * draws the same set on the GPU in one pass.
 */

import type { MapEntityPoint, MapLayer, ResolvedMarkerConfig } from '../../types/api';
import type { GeoCollection, GeoFeature } from './geoData';

/** What a business feature carries into the layers, the popup and the filters. */
export interface EntityFeatureProperties {
  code: string;
  name: string;
  entityType: MapLayer;
  parentType: MapLayer | null;
  parentCode: string | null;
  value: number;
  locationSource: string | null;
  latitude: number;
  longitude: number;
  /** `icon-image` name, registered from the Marker Designer's artwork. */
  icon: string;
  /** Set by the caller's filter; drives opacity rather than removal. */
  inFocus: boolean;
}

export type EntityFeature = GeoFeature<EntityFeatureProperties>;
export type EntityCollection = GeoCollection<EntityFeatureProperties>;

/** The `addImage` name for one entity type's marker. */
export function iconName(entityType: string): string {
  return `marker-${entityType}`;
}

const EMPTY: EntityCollection = { type: 'FeatureCollection', features: [] };

export interface BuildOptions {
  /** Entity types currently switched on. An empty set draws nothing. */
  visibleLayers: ReadonlySet<MapLayer>;
  /**
   * Codes the dashboard filter singled out, if any.
   *
   * Present only when the user arrived from a table asking to see specific
   * records. Matching features stay fully drawn and the rest are dimmed rather
   * than dropped, so the selection is read in the context it came from — which
   * is the whole reason to look at it on a map.
   */
  focusCodes?: ReadonlySet<string> | null;
  focusLayer?: MapLayer | null;
}

/**
 * Turn the API's entities into one collection.
 *
 * An entity with no coordinate produces no feature. That is not a silent drop:
 * the API counts them and the page lists them under "Not placed", because a
 * customer without a location is a mapping gap somebody has to fix, not a
 * customer to invent a position for.
 */
export function buildEntityCollection(
  entities: readonly MapEntityPoint[] | undefined,
  options: BuildOptions,
): EntityCollection {
  if (!entities?.length) return EMPTY;

  const features: EntityFeature[] = [];
  for (const entity of entities) {
    if (!options.visibleLayers.has(entity.type)) continue;
    if (!isDrawable(entity.latitude, entity.longitude)) continue;

    const focused =
      !options.focusCodes ||
      (options.focusLayer === entity.type && options.focusCodes.has(entity.code));

    features.push({
      type: 'Feature',
      properties: {
        code: entity.code,
        name: entity.name,
        entityType: entity.type,
        parentType: entity.parent_type,
        parentCode: entity.parent_code,
        value: entity.value ?? 0,
        locationSource: entity.location_source,
        latitude: entity.latitude as number,
        longitude: entity.longitude as number,
        icon: iconName(entity.type),
        inFocus: focused,
      },
      geometry: {
        type: 'Point',
        coordinates: [entity.longitude as number, entity.latitude as number],
      },
    });
  }

  return { type: 'FeatureCollection', features };
}

/**
 * Is this pair a position a map can draw?
 *
 * Rejects null, NaN, out-of-range values and the null island. `0, 0` is in the
 * Gulf of Guinea and is what an empty spreadsheet cell becomes when something
 * coerces it to a number — drawing it would put Bangladeshi customers in the
 * Atlantic, which is worse than not drawing them, because the page reports
 * unplaced entities and would then under-report them.
 */
export function isDrawable(
  latitude: number | null | undefined,
  longitude: number | null | undefined,
): boolean {
  if (latitude === null || latitude === undefined) return false;
  if (longitude === null || longitude === undefined) return false;
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return false;
  if (latitude < -90 || latitude > 90) return false;
  if (longitude < -180 || longitude > 180) return false;
  if (latitude === 0 && longitude === 0) return false;
  return true;
}

/**
 * `[west, south, east, north]` around a collection, or `null` if it is empty.
 *
 * Used to fit the view to what is actually drawn. A single point yields a
 * zero-width box, which MapLibre handles by zooming to its maximum — hence the
 * caller passes `maxZoom` to `fitBounds`.
 */
export function collectionBounds(
  collection: EntityCollection,
): [number, number, number, number] | null {
  if (!collection.features.length) return null;

  let west = 180;
  let south = 90;
  let east = -180;
  let north = -90;

  for (const feature of collection.features) {
    const { latitude, longitude } = feature.properties;
    west = Math.min(west, longitude);
    east = Math.max(east, longitude);
    south = Math.min(south, latitude);
    north = Math.max(north, latitude);
  }
  return [west, south, east, north];
}

/**
 * Register each entity type's marker artwork with the map.
 *
 * The artwork is the Marker Designer's, rendered to SVG by the backend and
 * delivered as a data URI — so what the designer previews is exactly what the
 * map draws, and swapping the map engine did not fork the marker definitions.
 * Returns the names that failed, which the caller reports rather than silently
 * falling back to a circle nobody chose.
 */
export async function registerMarkerImages(
  map: {
    hasImage: (name: string) => boolean;
    addImage: (name: string, image: ImageBitmap | HTMLImageElement) => void;
  },
  markers: Record<string, ResolvedMarkerConfig> | undefined,
): Promise<string[]> {
  if (!markers) return [];
  const failed: string[] = [];

  await Promise.all(
    Object.entries(markers).map(async ([entityType, config]) => {
      const name = iconName(entityType);
      if (map.hasImage(name)) return;
      try {
        map.addImage(name, await loadSvgImage(config.preview_svg, config.size));
      } catch {
        failed.push(entityType);
      }
    }),
  );
  return failed;
}

/**
 * An `<img>` decoded from inline SVG.
 *
 * A blob URL rather than a `data:` URI: Safari refuses to decode SVG data URIs
 * larger than a few kilobytes, and a decorated marker with an embedded icon
 * clears that easily. The URL is revoked as soon as the bitmap exists.
 */
function loadSvgImage(
  svg: string,
  size: { width: number; height: number },
): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
    const image = new Image();
    // Explicit dimensions: an SVG without an intrinsic size decodes to 0×0 in
    // Firefox, and MapLibre rejects a zero-sized image.
    image.width = Math.max(1, Math.round(size.width));
    image.height = Math.max(1, Math.round(size.height));
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('marker artwork could not be decoded'));
    };
    image.src = url;
  });
}
