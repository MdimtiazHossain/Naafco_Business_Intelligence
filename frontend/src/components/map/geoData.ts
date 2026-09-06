/**
 * Loading an administrative boundary file, once per session.
 *
 * These are static assets under `public/geo/`, so every component that draws a
 * backdrop wants the same bytes and none of them change while the tab is open.
 * The cache holds the *promise*, not the result, which is what makes two
 * components mounting in the same tick share one request rather than race to
 * issue two — and the upazila file is 1.7 MB, so that matters.
 *
 * A failure is cached as a rejection and then evicted, so a network blip does
 * not permanently poison the map: the next attempt retries, while a burst of
 * callers during the failure still shares the one attempt.
 *
 * **Keyed on the URL the server sent, not on a table of filenames here.** The
 * earlier version of this module imported a `GEO_FILES` map from `mapConfig`,
 * which put a list of asset names in the browser — a list that would outlive
 * what it named the moment a file was renamed or dropped. The catalogue now
 * comes from `GET /api/map/config`, so this module never needs to know that
 * districts live in `bgd_admin2.geojson`.
 */

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

/** What every administrative outline carries; pinned by `test_map_boundaries`. */
export interface BoundaryProperties {
  code: string;
  name: string;
  parent_code?: string;
  district_name?: string;
  division_name?: string;
}

export class GeoDataUnavailable extends Error {}

const cache = new Map<string, Promise<GeoCollection>>();

/**
 * Fetch and validate one boundary file by its published URL.
 *
 * Validated rather than trusted: a truncated or wrongly-deployed file would
 * otherwise surface as an opaque MapLibre error inside the render loop, and a
 * missing backdrop must not take the map down with it. An invalid file becomes
 * a rejected promise the caller reports as a note.
 */
export function loadGeoJson<P = BoundaryProperties>(
  url: string,
): Promise<GeoCollection<P>> {
  const cached = cache.get(url);
  if (cached) return cached as Promise<GeoCollection<P>>;

  const request = fetch(url)
    .then(async (response) => {
      if (!response.ok) {
        throw new GeoDataUnavailable(`${url} returned ${response.status}.`);
      }
      const body: unknown = await response.json();
      if (
        !body
        || typeof body !== 'object'
        || (body as GeoCollection).type !== 'FeatureCollection'
        || !Array.isArray((body as GeoCollection).features)
      ) {
        throw new GeoDataUnavailable(`${url} is not a GeoJSON FeatureCollection.`);
      }
      return body as GeoCollection;
    })
    .catch((error: unknown) => {
      // Evict so the next attempt retries. Keeping a rejected promise would
      // make one bad load permanent for the life of the tab.
      cache.delete(url);
      throw error instanceof GeoDataUnavailable
        ? error
        : new GeoDataUnavailable(
          `${url} could not be loaded: ${(error as Error)?.message ?? 'unknown error'}`,
        );
    });

  cache.set(url, request);
  return request as Promise<GeoCollection<P>>;
}

/** Only for tests, which need each case to start from a cold cache. */
export function clearGeoCache(): void {
  cache.clear();
}
