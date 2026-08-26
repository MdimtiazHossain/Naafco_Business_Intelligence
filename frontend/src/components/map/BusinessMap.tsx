/**
 * The business map: MapLibre GL JS over an OpenFreeMap basemap.
 *
 * One engine, one component, one data flow. Everything drawn here is either a
 * local administrative file (`public/geo/`) or a row the API already returned
 * under the caller's permissions — this component fetches no business data of
 * its own and holds no business logic, which is what keeps the map and the
 * dashboard from ever disagreeing about a number.
 *
 * The map instance is created once and then *mutated*. Re-creating it when a
 * filter changes would throw away the tile cache, the user's pan and zoom and
 * about a second of GPU work on every keystroke, so props flow into
 * `setData` / `setPaintProperty` / `setLayoutProperty` calls instead. The only
 * thing that rebuilds the style is a theme change, and even then the sources and
 * layers are re-applied by the same function that applied them the first time.
 */

import {
  FullscreenControl,
  GeoJSONSource,
  LngLat,
  Map as MapLibreMap,
  NavigationControl,
  Popup,
  ScaleControl,
  setWorkerUrl,
  type ExpressionSpecification,
  type MapLayerMouseEvent,
  type MapGeoJSONFeature,
  type StyleSpecification,
} from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { AreaStyle } from '../../types/api';
import {
  BANGLADESH_BOUNDS,
  BASEMAP_REFINEMENTS,
  BOUNDARY_LEVELS,
  FALLBACK_AREA_STYLES,
  FIT_PADDING,
  IDS,
  MASK_PAINT,
  type BoundaryLevelKey,
} from './mapConfig';
import { GeoDataUnavailable, loadGeoJson, type GeoCollection } from './geoData';
import {
  collectionBounds,
  registerMarkerImages,
  type EntityCollection,
  type EntityFeatureProperties,
} from './businessGeoJson';
import { MapControls } from './MapControls';
import { MapPopup, type PopupSubject } from './MapPopup';
import type { ResolvedMarkerConfig } from '../../types/api';

/** One aggregated point, ready to draw. */
export interface BubbleDatum {
  code: string;
  label: string;
  latitude: number;
  longitude: number;
  /** Pixel radius, already scaled against the largest value present. */
  radius: number;
  /** Achievement band colour, or the unscored grey. */
  colour: string;
  netSales: number;
  targetAmount: number;
  /** `null` where the server could not score it - never rendered as zero. */
  achievement: number | null;
}

export interface AreaMetric {
  stock: number;
  stock_source?: 'measured' | 'default';
}

export interface BusinessMapProps {
  /** OpenFreeMap style URL for the active theme. */
  styleUrl: string;
  theme: 'light' | 'dark';
  /** Administrative levels currently drawn, outermost first. */
  activeLevels: readonly BoundaryLevelKey[];
  /** Appearance per level, from `map_area_styles` via the API. */
  areaStyles: Partial<Record<BoundaryLevelKey, AreaStyle>>;
  /** Business entities as GeoJSON, from the business data adapter. */
  entities: EntityCollection;
  /** Marker artwork per entity type, from the Marker Designer. */
  markers: Record<string, ResolvedMarkerConfig> | undefined;
  /** Server-computed metric per administrative code. Geometry stays local. */
  areaMetrics: Record<string, AreaMetric>;
  /**
   * A colour per administrative code, for the level being measured.
   *
   * Handed in already resolved rather than computed here, for the same reason
   * every other number on this map is: the component draws what it is given and
   * decides nothing about the data. `null` means no mode is colouring areas, and
   * the boundaries fall back to the styles `map_area_styles` holds.
   */
  choropleth: { level: BoundaryLevelKey; colours: Record<string, string> } | null;
  /**
   * Aggregated business points, already encoded as size and colour.
   *
   * `null` when the mode measures nothing. Handed in fully resolved for the
   * same reason everything else is: this component draws, and decides nothing
   * about what a number means.
   */
  bubbles: readonly BubbleDatum[] | null;
  /**
   * How strongly the business markers are drawn, 0..1.
   *
   * A choropleth and 846 markers are two readings competing for the same
   * pixels, and the fill loses: the markers sit on top and are the higher
   * contrast. Muting them lets the area colour be read as the answer while the
   * markers stay as texture showing where the customers actually are — which is
   * still worth seeing, so they are dimmed rather than removed. The layers, the
   * click targets and the popups are untouched at any value.
   */
  markerEmphasis: number;
  /** Dim everything outside Bangladesh. */
  showMask: boolean;
  showCapitals: boolean;
  showAdminLines: boolean;
  onEntitySelect: (properties: EntityFeatureProperties) => void;
  onAreaSelect: (level: BoundaryLevelKey, code: string, name: string) => void;
  /** Non-fatal problems, surfaced by the page rather than swallowed. */
  onError: (message: string | null) => void;
  formatValue: (value: number) => string;
  /** Bumping this refits the view to Bangladesh — used after a drill reset. */
  fitToken?: number;
  className?: string;
}

/**
 * Point MapLibre at its own worker, explicitly.
 *
 * MapLibre derives the worker URL at *runtime* from `import.meta.url` and a
 * ternary that picks between `maplibre-gl-worker.mjs` and its `-dev` twin. A
 * bundler cannot statically analyse that, so Vite never emits the worker as an
 * asset: in the built app the derived URL resolved against the hashed MapPage
 * chunk, asked for `/assets/maplibre-gl-worker.mjs`, and got a 404. Every
 * control still rendered because controls are main-thread, while the canvas
 * stayed empty because tile and GeoJSON parsing both live in the worker — the
 * basemap and the boundaries failed together, with nothing logged.
 *
 * `?url` is what makes Vite emit the file and hand back its final hashed path;
 * `setWorkerUrl` replaces the guess with it. Module scope, so this runs before
 * any `new MapLibreMap(...)` below. Dev never showed the bug — there the module
 * is served from `node_modules/.vite/deps/`, where the worker sits beside it.
 */
setWorkerUrl(maplibreWorkerUrl);

/**
 * How long to wait for the basemap before drawing the local layers anyway.
 *
 * Only unblocks the wait; it never cancels the basemap, so erring long costs
 * nothing but a later first paint of the boundaries, and erring short costs
 * nothing at all. Short enough that a user is not left watching an empty panel.
 */
const STYLE_LOAD_GRACE_MS = 6000;

/**
 * Fill opacity for a measured area.
 *
 * Well above the 0.05 a boundary rests at, because in a choropleth the fill
 * *is* the reading; well below opaque, because the basemap underneath is what
 * tells the reader which part of the country they are looking at. The boundary
 * strokes stay exactly as configured on top, so the hierarchy survives the fill.
 */
const CHOROPLETH_OPACITY = 0.68;

/** A style with no layers, so a failed basemap still yields a usable map. */
const BLANK_STYLE: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [],
  glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
};

export function BusinessMap({
  styleUrl,
  theme,
  activeLevels,
  areaStyles,
  entities,
  markers,
  areaMetrics,
  choropleth,
  bubbles,
  markerEmphasis,
  showMask,
  showCapitals,
  showAdminLines,
  onEntitySelect,
  onAreaSelect,
  onError,
  formatValue,
  fitToken,
  className,
}: BusinessMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const popupRef = useRef<Popup | null>(null);
  const popupHostRef = useRef<HTMLDivElement | null>(null);
  const hoveredRef = useRef<{ source: string; id: string | number } | null>(null);
  const [ready, setReady] = useState(false);
  /**
   * Bumped once every source and layer is in place.
   *
   * Distinct from `ready`, and the distinction matters: `ready` means the style
   * has loaded, but the layers are added *asynchronously* after it, because each
   * one waits on its GeoJSON file. Attaching the click and hover handlers on
   * `ready` alone binds them to layers that do not exist yet, and MapLibre
   * silently ignores a handler for an unknown layer — so the map would render
   * perfectly and simply never respond to a click.
   */
  const [layerGeneration, setLayerGeneration] = useState(0);
  const [popup, setPopup] = useState<PopupSubject | null>(null);

  /**
   * Cancels a deferred `applyLayers`, when one is waiting for the style.
   *
   * `applyLayers` can only add a source to a style that has finished parsing.
   * It used to return when the style was not ready, which silently *dropped*
   * the request: nothing ever asked again, so a style that parsed a moment
   * later left the map with a basemap and no boundaries, no mask and no
   * markers, permanently, with nothing logged. Now the request is deferred
   * instead, and this holds the unsubscribe for the one deferral in flight —
   * one, because `styledata` fires repeatedly during a load and a listener per
   * dropped call would pile up.
   */
  const pendingApplyRef = useRef<(() => void) | null>(null);
  /** `applyLayers`, readable from inside itself without a circular dependency. */
  const applyLayersRef = useRef<(() => Promise<void>) | null>(null);
  /**
   * Has the current style been *parsed*? The only question that matters here.
   *
   * Deliberately not `map.isStyleLoaded()`, which was the original gate and is a
   * different question: it means "parsed **and every source's tiles are
   * loaded**". A basemap source that never finishes — a tile request that hangs,
   * a raster source the host is slow with — pins it false indefinitely on a map
   * that is rendering perfectly, and gating on it meant the boundaries, mask and
   * markers waited for a condition that never arrived. Measured on this
   * deployment: `isStyleLoaded()` stayed false for 15s while the basemap was
   * visibly drawn. Adding a source or layer only requires the style to be
   * parsed, which is exactly what `style.load` announces.
   */
  const styleParsedRef = useRef(false);

  // Props the map's own event handlers need. Handlers are attached once, so
  // reading through a ref is what lets them see current props without the
  // listener being torn down and rebuilt on every render.
  const latest = useRef({ onEntitySelect, onAreaSelect, areaMetrics, formatValue });
  latest.current = { onEntitySelect, onAreaSelect, areaMetrics, formatValue };

  const styleFor = useCallback(
    (level: BoundaryLevelKey): AreaStyle => areaStyles[level] ?? FALLBACK_AREA_STYLES[level],
    [areaStyles],
  );

  /* ---------------------------------------------------------------- create */

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    let map: MapLibreMap;
    try {
      map = new MapLibreMap({
        container: containerRef.current,
        style: styleUrl,
        // A starting view, immediately replaced by a fit to the country outline
        // once that file has loaded. Centring on Bangladesh rather than [0, 0]
        // means the first frame is never the middle of the Atlantic.
        bounds: BANGLADESH_BOUNDS,
        fitBoundsOptions: { padding: FIT_PADDING },
        attributionControl: { compact: true },
        // Nothing here is drawn in 3D, and a tilted business map is harder to
        // read, not easier.
        pitchWithRotate: false,
        dragRotate: false,
      });
    } catch (error) {
      // No WebGL, or a context the browser refused. The dashboard must survive
      // this: the page keeps its tables and reports the map as unavailable.
      onError(
        `The map could not start: ${(error as Error)?.message ?? 'WebGL is unavailable'}.`,
      );
      return;
    }

    mapRef.current = map;
    map.addControl(new NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new FullscreenControl(), 'top-right');
    map.addControl(new ScaleControl({ unit: 'metric' }), 'bottom-left');

    const host = document.createElement('div');
    popupHostRef.current = host;
    popupRef.current = new Popup({
      closeButton: false,
      closeOnClick: true,
      maxWidth: '18rem',
      offset: 14,
    }).setDOMContent(host);
    popupRef.current.on('close', () => setPopup(null));

    let styleFailed = false;
    map.on('error', (event) => {
      // Style and tile failures arrive here rather than as exceptions. They are
      // reported, not thrown: a missing tile must not take the page down.
      const message = event.error?.message;

      /* A style that *failed* — as opposed to one still arriving — is the case
         `BLANK_STYLE` exists for, and the only case that justifies discarding
         what `setStyle` discards. Recognised by the style's own URL appearing in
         the error while the style has never finished parsing; a tile or glyph
         that 404s names itself instead and must not cost us the basemap. Once
         only, or a failing style would be swapped repeatedly. */
      if (!styleFailed && !styleParsedRef.current && message?.includes(styleUrl)) {
        styleFailed = true;
        map.setStyle(BLANK_STYLE);
        setReady(true);
        onError('The basemap could not be loaded. Boundaries and markers are still shown.');
        return;
      }

      if (message) onError(`Map: ${message}`);
    });

    // `style.load` fires whenever a style finishes parsing — the first one and
    // every one `setStyle` brings in. That is the moment sources and layers may
    // be added, so it is what releases the wait in `applyLayers`.
    /* `style.load` is the signal, for both flags.
       `load` additionally waits for the first frame to be painted, which waits
       on tiles — so on a map whose basemap tiles are slow it can lag far behind
       the style being usable, and gating `ready` on it left the boundaries
       waiting for the basemap they do not depend on. `style.load` fires as soon
       as the style is parsed, which is precisely when sources and layers may be
       added, and it fires again for every style `setStyle` brings in. */
    const styleReady = () => {
      styleParsedRef.current = true;
      setReady(true);
    };
    map.on('style.load', styleReady);
    map.on('load', styleReady);



    /* `load` fires once, after the style has parsed. A style that never arrives
       therefore leaves the map permanently blank: no `load`, so nothing ever
       asks for the layers, and the boundaries, mask and markers this dashboard
       actually needs are local files that owe the basemap nothing.

       So the wait is unblocked here — but *without* touching the style. An
       earlier version swapped in `BLANK_STYLE`, and that was worse than the
       problem: `setStyle` discards every source and layer the basemap had, so a
       basemap that was merely slow was destroyed the moment the timer expired
       and could never come back, because nothing sets it again. Slow is not
       failed. Releasing `ready` is enough on its own: `applyLayers` already
       waits for the style itself, so the local layers go on as soon as it
       parses — whenever that is — and the basemap paints under them when it
       arrives. Nothing is thrown away and there is nothing to recover from.

       `BLANK_STYLE` is still the answer when the style genuinely *fails*, which
       arrives as a MapLibre `error` event and is handled below. */
    const watchdog = window.setTimeout(() => {
      if (styleParsedRef.current) return;
      setReady(true);
    }, STYLE_LOAD_GRACE_MS);

    return () => {
      window.clearTimeout(watchdog);
      popupRef.current?.remove();
      popupRef.current = null;
      map.remove();
      mapRef.current = null;
      setReady(false);
    };
    // Created once. `styleUrl` changes are handled by the theme effect below,
    // which swaps the style in place instead of rebuilding the map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ------------------------------------------------------ sources & layers */

  /**
   * Add every source and layer this map draws, in draw order.
   *
   * Idempotent and re-runnable, because `setStyle` discards all of it: the same
   * function that builds the map on load rebuilds it after a theme change, so
   * the two can never drift apart.
   */
  const applyLayers = useCallback(async () => {
    const map = mapRef.current;
    if (!map) return;

    /* The style is still parsing. Wait for it rather than dropping the request:
       this is the whole difference between a map that draws its boundaries a
       moment late and one that never draws them at all. `styledata` fires many
       times during a load, so the listener re-checks the parsed flag and only
       then unsubscribes; `idle` is the backstop for a style that finished
       before this listener was attached. */
    if (!styleParsedRef.current) {
      if (pendingApplyRef.current) return; // already waiting — one is enough
      const retry = () => {
        if (!styleParsedRef.current) return;
        pendingApplyRef.current?.();
        pendingApplyRef.current = null;
        void applyLayersRef.current?.();
      };
      map.on('styledata', retry);
      map.on('idle', retry);
      pendingApplyRef.current = () => {
        map.off('styledata', retry);
        map.off('idle', retry);
      };
      return;
    }

    // Got through, so any deferral still armed is stale.
    pendingApplyRef.current?.();
    pendingApplyRef.current = null;

    const problems: string[] = [];

    /* Quieten the basemap before anything is drawn over it. Runs here rather
       than through `transformStyle` so it happens on the first load and on every
       theme swap by the same path, and so a layer the host has renamed is simply
       skipped instead of throwing. */
    refineBasemap(map, theme);

    /* The mask, first, so it sits above every basemap layer including labels —
       which is the point: a foreign city label at full contrast would undo the
       dimming that says this dashboard is about Bangladesh. Bangladesh itself is
       a hole in the polygon, so nothing inside it is touched. */
    try {
      const mask = await loadGeoJson('mask');
      addSource(map, IDS.maskSource, mask);
      if (!map.getLayer(IDS.maskLayer)) {
        map.addLayer({
          id: IDS.maskLayer,
          type: 'fill',
          source: IDS.maskSource,
          paint: {
            'fill-color': MASK_PAINT[theme].color,
            'fill-opacity': MASK_PAINT[theme].opacity,
          },
        });
      }
    } catch (error) {
      problems.push(describe(error));
    }

    /* Administrative boundaries, outermost first so the finer levels draw over
       the broader ones rather than under them. */
    for (const level of BOUNDARY_LEVELS) {
      try {
        const collection = await loadGeoJson(level.file);
        const sourceId = IDS.boundarySource(level.key);
        // `promoteId` lifts the P-code into the feature id, which is what
        // `setFeatureState` addresses — hover and selection need a stable id and
        // GeoJSON string ids are not one without this.
        addSource(map, sourceId, collection, 'code');

        const paint = styleFor(level.key);
        if (!map.getLayer(IDS.boundaryFill(level.key))) {
          map.addLayer({
            id: IDS.boundaryFill(level.key),
            type: 'fill',
            source: sourceId,
            minzoom: level.minZoom,
            paint: {
              'fill-color': paint.fill_color,
              'fill-opacity': fillOpacity(paint),
            },
          });
        }
        if (!map.getLayer(IDS.boundaryLine(level.key))) {
          map.addLayer({
            id: IDS.boundaryLine(level.key),
            type: 'line',
            source: sourceId,
            minzoom: level.minZoom,
            layout: { 'line-join': 'round', 'line-cap': 'round' },
            paint: {
              'line-color': paint.stroke_color,
              'line-opacity': paint.stroke_opacity,
              'line-width': paint.stroke_width,
            },
          });
        }
      } catch (error) {
        problems.push(describe(error));
      }
    }

    /* Reference geography from the published release. Off by default: it is
       context, and a map already carrying business markers does not need every
       administrative line as well. */
    try {
      const lines = await loadGeoJson('lines');
      addSource(map, IDS.linesSource, lines);
      if (!map.getLayer(IDS.linesLayer)) {
        map.addLayer({
          id: IDS.linesLayer,
          type: 'line',
          source: IDS.linesSource,
          layout: { visibility: 'none', 'line-join': 'round' },
          paint: {
            'line-color': theme === 'dark' ? '#64748B' : '#94A3B8',
            'line-width': 0.6,
            'line-dasharray': [2, 2],
          },
        });
      }
    } catch (error) {
      problems.push(describe(error));
    }

    try {
      const capitals = await loadGeoJson('capitals');
      addSource(map, IDS.capitalsSource, capitals);
      if (!map.getLayer(IDS.capitalsLayer)) {
        map.addLayer({
          id: IDS.capitalsLayer,
          type: 'symbol',
          source: IDS.capitalsSource,
          layout: {
            visibility: 'none',
            'text-field': ['get', 'name'],
            'text-size': ['interpolate', ['linear'], ['zoom'], 6, 9, 11, 13],
            'text-offset': [0, 0.8],
            'text-anchor': 'top',
            'text-allow-overlap': false,
          },
          paint: {
            'text-color': theme === 'dark' ? '#E2E8F0' : '#334155',
            'text-halo-color': theme === 'dark' ? '#0F172A' : '#FFFFFF',
            'text-halo-width': 1.2,
          },
        });
      }
    } catch (error) {
      problems.push(describe(error));
    }

    /* Business entities, last, so a marker is never hidden under a boundary. */
    addSource(map, IDS.entitySource, entities as unknown as GeoCollection);

    if (!map.getLayer(IDS.entityCircles)) {
      map.addLayer({
        id: IDS.entityCircles,
        type: 'circle',
        source: IDS.entitySource,
        paint: {
          // A halo under the artwork: it keeps a marker findable over a dark
          // mask or a busy basemap, and it is what remains visible when the
          // designed icon has not finished decoding.
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 3, 12, 7],
          'circle-color': theme === 'dark' ? '#38BDF8' : '#0EA5E9',
          'circle-opacity': circleOpacity(markerEmphasis),
          'circle-stroke-width': 1,
          'circle-stroke-color': theme === 'dark' ? '#0F172A' : '#FFFFFF',
        },
      });
    }

    if (!map.getLayer(IDS.entityIcons)) {
      map.addLayer({
        id: IDS.entityIcons,
        type: 'symbol',
        source: IDS.entitySource,
        layout: {
          'icon-image': ['get', 'icon'],
          'icon-allow-overlap': true,
          'icon-ignore-placement': true,
          'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.5, 12, 1],
          'icon-anchor': 'bottom',
        },
        paint: { 'icon-opacity': iconOpacity(markerEmphasis) },
      });
    }

    if (!map.getLayer(IDS.entityLabels)) {
      map.addLayer({
        id: IDS.entityLabels,
        type: 'symbol',
        // Names only once the map is close enough for them not to collide into
        // an unreadable mat.
        minzoom: 9,
        source: IDS.entitySource,
        layout: {
          'text-field': ['get', 'name'],
          'text-size': 11,
          'text-offset': [0, 0.9],
          'text-anchor': 'top',
          'text-optional': true,
        },
        paint: {
          'text-color': theme === 'dark' ? '#F1F5F9' : '#1E293B',
          'text-halo-color': theme === 'dark' ? '#020617' : '#FFFFFF',
          'text-halo-width': 1.2,
          'text-opacity': ['case', ['get', 'inFocus'], 1, 0.35],
        },
      });
    }

    const failed = await registerMarkerImages(map, markers);
    if (failed.length) {
      problems.push(`Marker artwork failed to load for: ${failed.join(', ')}.`);
    }

    onError(problems.length ? problems.join(' ') : null);
    setLayerGeneration((generation) => generation + 1);
    // `entities` and `markers` are applied by their own effects below; including
    // them here would rebuild every layer on each filter change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, styleFor, onError]);

  applyLayersRef.current = applyLayers;

  useEffect(() => {
    if (!ready) return;
    void applyLayers();
  }, [ready, applyLayers]);

  // Drop any deferral still waiting when the component goes away, so a resolved
  // style cannot call into an unmounted map.
  useEffect(
    () => () => {
      pendingApplyRef.current?.();
      pendingApplyRef.current = null;
    },
    [],
  );

  /* -------------------------------------------------------------- theme swap */

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    // `setStyle` drops every source and layer this component added, so they are
    // re-applied once the new basemap reports itself loaded. `applyLayers` now
    // waits for the style itself, so this only has to ask again after the swap —
    // it no longer has to own the retry, and cannot drop the rebuild if the new
    // style parses more slowly than the old one did.
    const rebuild = () => {
      if (!styleParsedRef.current) return;
      map.off('styledata', rebuild);
      void applyLayers();
    };
    map.on('styledata', rebuild);
    // The outgoing style is about to be discarded; nothing may be added until
    // `style.load` announces the new one.
    styleParsedRef.current = false;
    try {
      map.setStyle(styleUrl);
    } catch {
      // A style URL that cannot be reached leaves a usable, empty map rather
      // than a blank page: local geometry and business markers still draw.
      map.setStyle(BLANK_STYLE);
      onError('The basemap could not be loaded. Boundaries and markers are still shown.');
    }
    return () => {
      map.off('styledata', rebuild);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [styleUrl]);

  /* ------------------------------------------------------------ prop updates */

  // Business data. `setData` swaps the source's contents without touching the
  // layers reading it, which is what keeps a filter change from restyling.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const source = map.getSource(IDS.entitySource) as GeoJSONSource | undefined;
    source?.setData(entities as never);
  }, [entities, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !markers) return;
    void registerMarkerImages(map, markers);
  }, [markers, ready]);

  // Level visibility. Toggling a layer is a layout property, not a re-add: the
  // geometry stays parsed on the GPU and switching it back on is instant.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    for (const level of BOUNDARY_LEVELS) {
      const visible = activeLevels.includes(level.key) ? 'visible' : 'none';
      for (const id of [IDS.boundaryFill(level.key), IDS.boundaryLine(level.key)]) {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible);
      }
    }
  }, [activeLevels, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (map.getLayer(IDS.maskLayer)) {
      map.setLayoutProperty(IDS.maskLayer, 'visibility', showMask ? 'visible' : 'none');
    }
    if (map.getLayer(IDS.capitalsLayer)) {
      map.setLayoutProperty(
        IDS.capitalsLayer, 'visibility', showCapitals ? 'visible' : 'none',
      );
    }
    if (map.getLayer(IDS.linesLayer)) {
      map.setLayoutProperty(
        IDS.linesLayer, 'visibility', showAdminLines ? 'visible' : 'none',
      );
    }
  }, [showMask, showCapitals, showAdminLines, ready]);

  // Style changes from the database reach the existing layers as paint updates.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    for (const level of BOUNDARY_LEVELS) {
      const paint = styleFor(level.key);
      const fill = IDS.boundaryFill(level.key);
      const line = IDS.boundaryLine(level.key);
      /* A mode that measures this level paints it from the data; every other
         level, and every level in a mode that measures nothing, keeps the
         configured style. The choropleth is a `match` on the feature's own code
         so MapLibre resolves it per feature on the GPU — no per-feature state to
         set and nothing to keep in step as the viewport moves. */
      const measured = choropleth?.level === level.key ? choropleth : null;
      if (map.getLayer(fill)) {
        if (measured && Object.keys(measured.colours).length) {
          // Built through `unknown`: a `match` with a spread body cannot be
          // expressed in MapLibre's tuple type, which fixes each arm's position.
          const expression = [
            'match',
            ['get', 'code'],
            ...Object.entries(measured.colours).flatMap(([code, colour]) => [code, colour]),
            // An area the metric does not cover keeps the level's own fill
            // rather than being coloured as if it had measured zero.
            paint.fill_color,
          ] as unknown as ExpressionSpecification;
          map.setPaintProperty(fill, 'fill-color', expression);
          map.setPaintProperty(fill, 'fill-opacity', CHOROPLETH_OPACITY);
        } else {
          map.setPaintProperty(fill, 'fill-color', paint.fill_color);
          map.setPaintProperty(fill, 'fill-opacity', fillOpacity(paint));
        }
      }
      if (map.getLayer(line)) {
        map.setPaintProperty(line, 'line-color', paint.stroke_color);
        map.setPaintProperty(line, 'line-width', paint.stroke_width);
        map.setPaintProperty(line, 'line-opacity', paint.stroke_opacity);
      }
    }
  }, [styleFor, ready, choropleth]);

  /* ------------------------------------------------------------ interaction */

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    const entityLayers = [IDS.entityIcons, IDS.entityCircles];
    const areaLayers = BOUNDARY_LEVELS.map((level) => IDS.boundaryFill(level.key));

    const openPopup = (
      lngLat: LngLat, subject: PopupSubject,
    ) => {
      setPopup(subject);
      popupRef.current?.setLngLat(lngLat).addTo(map);
    };

    const onEntityClick = (event: MapLayerMouseEvent) => {
      const feature = event.features?.[0];
      if (!feature) return;
      const properties = feature.properties as unknown as EntityFeatureProperties;
      latest.current.onEntitySelect(properties);
      openPopup(event.lngLat, { kind: 'entity', entity: properties });
    };

    const onAreaClick = (event: MapLayerMouseEvent) => {
      // The topmost boundary wins: clicking where an upazila and its district
      // overlap should select the finer one, which is what is under the cursor.
      const feature = event.features?.[0];
      if (!feature) return;
      const level = levelOfLayer(feature.layer.id);
      if (!level) return;
      const code = String(feature.properties.code ?? '');
      const name = String(feature.properties.name ?? code);
      latest.current.onAreaSelect(level, code, name);
      openPopup(event.lngLat, {
        kind: 'area',
        level,
        code,
        name,
        properties: feature.properties as Record<string, unknown>,
        metric: latest.current.areaMetrics[code],
      });
    };

    const setHover = (feature: MapGeoJSONFeature | undefined) => {
      const map_ = mapRef.current;
      if (!map_) return;
      if (hoveredRef.current) {
        map_.setFeatureState(hoveredRef.current, { hover: false });
        hoveredRef.current = null;
      }
      if (feature?.id !== undefined && feature.source) {
        hoveredRef.current = { source: feature.source, id: feature.id };
        map_.setFeatureState(hoveredRef.current, { hover: true });
      }
    };

    const pointerOn = () => { map.getCanvas().style.cursor = 'pointer'; };
    const pointerOff = () => {
      map.getCanvas().style.cursor = '';
      setHover(undefined);
    };
    const trackHover = (event: MapLayerMouseEvent) => {
      map.getCanvas().style.cursor = 'pointer';
      setHover(event.features?.[0]);
    };

    for (const layer of entityLayers) {
      if (!map.getLayer(layer)) continue;
      map.on('click', layer, onEntityClick);
      map.on('mouseenter', layer, pointerOn);
      map.on('mouseleave', layer, pointerOff);
    }
    for (const layer of areaLayers) {
      if (!map.getLayer(layer)) continue;
      map.on('click', layer, onAreaClick);
      map.on('mousemove', layer, trackHover);
      map.on('mouseleave', layer, pointerOff);
    }

    return () => {
      for (const layer of entityLayers) {
        map.off('click', layer, onEntityClick);
        map.off('mouseenter', layer, pointerOn);
        map.off('mouseleave', layer, pointerOff);
      }
      for (const layer of areaLayers) {
        map.off('click', layer, onAreaClick);
        map.off('mousemove', layer, trackHover);
        map.off('mouseleave', layer, pointerOff);
      }
    };
    // Re-bound whenever the layers are rebuilt — after a theme swap the old
    // layer objects are gone and the handlers with them.
  }, [ready, layerGeneration]);

  /* ------------------------------------------------------------------- fit */

  const fitBangladesh = useCallback(() => {
    const map = mapRef.current;
    if (!map) return;
    // The country file's own bbox, so the framing follows the data rather than
    // a zoom level somebody guessed. Falling back to the constant only matters
    // when that file is missing, in which case nothing is drawn to frame.
    loadGeoJson('country')
      .then((country) => {
        map.fitBounds(country.bbox ?? BANGLADESH_BOUNDS, { padding: FIT_PADDING });
      })
      .catch(() => map.fitBounds(BANGLADESH_BOUNDS, { padding: FIT_PADDING }));
  }, []);

  useEffect(() => {
    if (!ready) return;
    fitBangladesh();
  }, [ready, fitToken, fitBangladesh]);

  /** Frame whatever the filter left on screen, when there is anything. */
  const fitData = useCallback(() => {
    const map = mapRef.current;
    const bounds = collectionBounds(entities);
    if (!map || !bounds) return;
    map.fitBounds(bounds, { padding: FIT_PADDING * 2, maxZoom: 12 });
  }, [entities]);

  /* Emphasis is a prop, and the layers outlive it: switching mode must restyle
     what is already drawn rather than wait for the next rebuild. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (map.getLayer(IDS.entityIcons)) {
      map.setPaintProperty(IDS.entityIcons, 'icon-opacity', iconOpacity(markerEmphasis));
    }
    if (map.getLayer(IDS.entityCircles)) {
      map.setPaintProperty(IDS.entityCircles, 'circle-opacity', circleOpacity(markerEmphasis));
    }
  }, [markerEmphasis, ready, layerGeneration]);

  /* The bubble layer: aggregated business points, sized and coloured by the
     page. Its own source, so a mode change swaps contents with `setData` rather
     than rebuilding layers, and so it sits above the boundaries without
     disturbing the marker layers that keep their own meaning. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !styleParsedRef.current) return;

    const collection = {
      type: 'FeatureCollection' as const,
      features: (bubbles ?? []).map((bubble) => ({
        type: 'Feature' as const,
        properties: {
          code: bubble.code,
          label: bubble.label,
          radius: bubble.radius,
          colour: bubble.colour,
          netSales: bubble.netSales,
          targetAmount: bubble.targetAmount,
          // A style expression cannot carry null, so "unscored" travels as a
          // flag beside the number rather than as a zero pretending to be one.
          achievement: bubble.achievement ?? 0,
          scored: bubble.achievement !== null,
        },
        geometry: {
          type: 'Point' as const,
          coordinates: [bubble.longitude, bubble.latitude],
        },
      })),
    };

    addSource(map, IDS.bubbleSource, collection as never, 'code');
    if (!map.getLayer(IDS.bubbleCircles)) {
      map.addLayer({
        id: IDS.bubbleCircles,
        type: 'circle',
        source: IDS.bubbleSource,
        paint: {
          'circle-radius': ['get', 'radius'],
          'circle-color': ['get', 'colour'],
          'circle-opacity': 0.72,
          'circle-stroke-width': 1.5,
          'circle-stroke-color': '#FFFFFF',
          'circle-stroke-opacity': 0.9,
        },
      });
    }
    map.setLayoutProperty(
      IDS.bubbleCircles, 'visibility', bubbles?.length ? 'visible' : 'none',
    );
  }, [bubbles, ready, layerGeneration]);

  /* ------------------------------------------------------------- responsive */

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === 'undefined') return;
    // The map's canvas is sized in pixels, so it has to be told when its box
    // changes — a sidebar collapsing or a phone rotating leaves it stretched
    // otherwise.
    /* A zero-sized box is skipped rather than resized to it. A collapsed
       panel, a hidden tab and the frame before layout settles all report 0x0,
       and resizing the canvas to nothing throws away the rendered frame — the
       map then comes back blank when the box reopens, because nothing asks it
       to draw again. Ignoring the degenerate size keeps the last good frame
       until a real one arrives. */
    const observer = new ResizeObserver(() => {
      if (!container.clientWidth || !container.clientHeight) return;
      mapRef.current?.resize();
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const hasEntities = entities.features.length > 0;

  return (
    <div className={className ?? 'relative h-[32rem] w-full overflow-hidden rounded-xl'}>
      {/* Sized with `h-full`, not `absolute inset-0`.
          MapLibre stamps its own `maplibregl-map` class onto whatever element it
          is given, and that class carries `position: relative` — same
          specificity as a Tailwind utility, and its stylesheet is imported
          after, so it wins. An absolutely-positioned container therefore turns
          relative on init, `inset-0` stops applying, and the map collapses to a
          300px default canvas inside a zero-height box. Percentage height off
          the sized wrapper is what MapLibre's CSS expects. */}
      <div ref={containerRef} className="h-full w-full" data-testid="maplibre-container" />

      <MapControls
        onFitCountry={fitBangladesh}
        onFitData={hasEntities ? fitData : undefined}
      />

      {popupHostRef.current && popup
        ? createPortal(
            <MapPopup subject={popup} formatValue={formatValue} />,
            popupHostRef.current,
          )
        : null}
    </div>
  );
}

/** Add a source, or replace its data if a previous style left one behind. */
function addSource(
  map: MapLibreMap,
  id: string,
  data: GeoCollection,
  promoteId?: string,
): void {
  const existing = map.getSource(id) as GeoJSONSource | undefined;
  if (existing) {
    existing.setData(data as never);
    return;
  }
  map.addSource(id, { type: 'geojson', data: data as never, ...(promoteId ? { promoteId } : {}) });
}

/**
 * Marker opacity, scaled by how much emphasis the current mode gives them.
 *
 * The `inFocus` split is preserved at every emphasis: a focused marker stays
 * ahead of an unfocused one whether the mode is showing markers as the subject
 * or as background, so the two meanings never collapse into each other.
 */
function iconOpacity(emphasis: number): ExpressionSpecification {
  return ['case', ['get', 'inFocus'], emphasis, emphasis * 0.3] as ExpressionSpecification;
}

function circleOpacity(emphasis: number): ExpressionSpecification {
  return [
    'case', ['get', 'inFocus'], emphasis * 0.9, emphasis * 0.25,
  ] as ExpressionSpecification;
}

/**
 * Apply `BASEMAP_REFINEMENTS` to whichever of those layers this style has.
 *
 * Every id is checked before it is written: the basemap belongs to OpenFreeMap,
 * and a layer renamed or dropped in one of their releases must quietly not be
 * refined rather than throw and take the whole map down with it. Nothing here
 * adds, removes or reorders a layer — only paint on layers the style already
 * drew, so the basemap shows exactly what it showed before, more quietly.
 */
function refineBasemap(map: MapLibreMap, theme: 'light' | 'dark'): void {
  for (const rule of BASEMAP_REFINEMENTS) {
    if (!map.getLayer(rule.id)) continue;
    try {
      map.setPaintProperty(rule.id, rule.property, theme === 'dark' ? rule.dark : rule.light);
    } catch {
      // A style that has the layer but not this property. Not worth reporting:
      // the map is fully usable, it is only a shade louder than intended.
    }
  }
}

/**
 * Fill opacity, lifted for the hovered and selected feature.
 *
 * A level with no fill configured stays unfilled at rest but still lights up
 * under the cursor — otherwise a district boundary would be clickable with no
 * indication that it is.
 */
function fillOpacity(paint: AreaStyle): ExpressionSpecification {
  const base = paint.fill_opacity ?? 0;
  return [
    'case',
    ['boolean', ['feature-state', 'hover'], false],
    Math.min(0.75, Math.max(base + 0.22, 0.22)),
    base,
  ];
}

function levelOfLayer(layerId: string): BoundaryLevelKey | null {
  return (
    BOUNDARY_LEVELS.find((level) => IDS.boundaryFill(level.key) === layerId)?.key ?? null
  );
}

function describe(error: unknown): string {
  return error instanceof GeoDataUnavailable
    ? error.message
    : `A map layer could not be loaded: ${(error as Error)?.message ?? 'unknown error'}`;
}

export { type EntityFeatureProperties };
