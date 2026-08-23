/**
 * Loading the local administrative GeoJSON, once per session.
 *
 * These files are static assets under `public/geo/`, so every tab that shows the
 * map wants the same bytes and none of them ever change while the tab is open.
 * The cache below is keyed on the file and holds the *promise*, not the result,
 * which is what makes two components mounting in the same tick share one request
 * rather than race to issue two.
 *
 * A failure is cached as a rejection and then evicted, so a network blip does not
 * permanently poison the map: the next mount retries, while a burst of callers
 * during the failure still shares the one attempt.
 */

import { GEO_FILES, type GeoFileKey } from './mapConfig';

/** Minimal GeoJSON shapes — enough to type what this app reads and draws. */
export interface GeoFeature<P = Record<string, unknown>> {
  type: 'Feature';
  id?: string | number;
  properties: P;
  geometry: { type: string; coordinates: unknown };
}

export interface GeoCollection<P = Record<string, unknown>> {
  type: 'FeatureCollection';
  features: GeoFeature<P>[];
  /** Written by the build script: `[west, south, east, north]`. */
  bbox?: [number, number, number, number];
}

/** Properties every administrative polygon carries. */
export interface AreaFeatureProperties {
  code: string;
  name: string;
  parent_code?: string;
  district_name?: string;
  division_name?: string;
}

export class GeoDataUnavailable extends Error {}

const cache = new Map<GeoFileKey, Promise<GeoCollection>>();

/**
 * Fetch and validate one local layer.
 *
 * Validated rather than trusted: a truncated or wrongly-deployed file would
 * otherwise surface as an opaque MapLibre error somewhere inside the render
 * loop, and the dashboard must not fall over because one boundary file is
 * missing. An invalid file becomes a rejected promise the caller reports.
 */
export function loadGeoJson<P = Record<string, unknown>>(
  key: GeoFileKey,
): Promise<GeoCollection<P>> {
  const cached = cache.get(key);
  if (cached) return cached as Promise<GeoCollection<P>>;

  const request = fetch(GEO_FILES[key])
    .then(async (response) => {
      if (!response.ok) {
        throw new GeoDataUnavailable(
          `${GEO_FILES[key]} returned ${response.status}. Run ` +
            '`python scripts/build_map_geojson.py` to generate the map assets.',
        );
      }
      const body: unknown = await response.json();
      if (
        !body ||
        typeof body !== 'object' ||
        (body as GeoCollection).type !== 'FeatureCollection' ||
        !Array.isArray((body as GeoCollection).features)
      ) {
        throw new GeoDataUnavailable(`${GEO_FILES[key]} is not a GeoJSON FeatureCollection.`);
      }
      return body as GeoCollection;
    })
    .catch((error: unknown) => {
      // Evict so the next mount retries. Keeping a rejected promise would make
      // one bad load permanent for the life of the tab.
      cache.delete(key);
      throw error instanceof GeoDataUnavailable
        ? error
        : new GeoDataUnavailable(
            `${GEO_FILES[key]} could not be loaded: ${
              (error as Error)?.message ?? 'unknown error'
            }`,
          );
    });

  cache.set(key, request);
  return request as Promise<GeoCollection<P>>;
}

/** Only for tests, which need each case to start from a cold cache. */
export function clearGeoCache(): void {
  cache.clear();
}
