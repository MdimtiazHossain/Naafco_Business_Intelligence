/**
 * The Business Map: `/map`.
 *
 * Performance by place, drilled from zone down to sub-territory, over a
 * Bangladesh-focused MapLibre map. Clicking a marker drills into it; the
 * breadcrumb walks back out.
 *
 * The data flow is one direction and one path:
 *
 *     master / transaction data
 *       → the dashboard's own filter state (`FilterContext`)
 *       → /api/map/entities, permission-filtered like every other report
 *       → the business data adapter (`components/map/businessGeoJson.ts`)
 *       → one GeoJSON source, three MapLibre layers
 *
 * Administrative geometry does not travel that path at all: it is served with
 * the frontend from `public/geo/`, because a boundary changes only when somebody
 * imports a new release. What the API still answers for those areas is the
 * *metric* — geometry local, numbers from the server, joined on the P-code both
 * sides already use.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  AlertTriangle,
  ChevronRight,
  MapPin,
  RefreshCw,
  Settings2,
} from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, EmptyState, QueryState } from '../components/States';
import { BusinessMap, type AreaMetric } from '../components/map/BusinessMap';
import { MapBandChips } from '../components/map/MapBandChips';
import { MapLayerPanel } from '../components/map/MapLayerPanel';
import { MapLegend } from '../components/map/MapLegend';
import { MapRanking, bandColor, type RankRow } from '../components/map/MapRanking';
import {
  ACHIEVEMENT_BANDS,
  BASEMAP_STYLES,
  MAP_MODES,
  MODE_BY_KEY,
  type MapModeKey,
  BOUNDARY_LEVELS,
  type BoundaryLevelKey,
} from '../components/map/mapConfig';
import {
  buildEntityCollection,
  type EntityFeatureProperties,
} from '../components/map/businessGeoJson';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { useAuth } from '../contexts/AuthContext';
import { FILTER_LABELS, useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { useTheme } from '../contexts/ThemeContext';
import { mapService, markerService, performanceService } from '../services';
import { formatAmount, formatCount, formatPercent } from '../utils/format';
import type { AreaStyle, FilterLevel, MapLayer } from '../types/api';

interface Crumb {
  level: MapLayer;
  label: string;
  code: string;
}

/**
 * Layers, in hierarchy order.
 *
 * A layer decides what is *drawn*; it never changes what the filter selects.
 * Selecting a territory keeps every customer under it in scope whether the
 * customer layer is on or off — which is why the counts panel reads from
 * `counts` (the scope) rather than from what happens to be on screen.
 */
const LAYERS: { key: MapLayer; labelKey: string }[] = [
  { key: 'zone', labelKey: 'filters.zone' },
  { key: 'region', labelKey: 'filters.region' },
  { key: 'area', labelKey: 'filters.area' },
  { key: 'unit', labelKey: 'filters.unit' },
  { key: 'territory', labelKey: 'filters.territory' },
  { key: 'sub_territory', labelKey: 'filters.subTerritory' },
  { key: 'customer', labelKey: 'filters.customer' },
  { key: 'sales_force', labelKey: 'filters.salesForce' },
];

const DEFAULT_LAYERS: MapLayer[] = [
  'region', 'area', 'unit', 'territory', 'sub_territory',
  'customer', 'sales_force',
];

/**
 * The master-data table a map entity type lives in.
 *
 * Only the types that have one: an organisational level is a dimension row and
 * has a detail page, and so do customers and sales force.
 */
const TABLE_BY_TYPE: Partial<Record<MapLayer, string>> = {
  company: 'dim_company',
  bu: 'dim_business_unit',
  sales_line: 'dim_sales_line',
  zone: 'dim_zone',
  region: 'dim_region',
  area: 'dim_area',
  unit: 'dim_unit',
  territory: 'dim_territory',
  sub_territory: 'dim_sub_territory',
  customer: 'dim_customer',
  sales_force: 'dim_sales_force',
};

/** Which level each entity type drills into. */
const CHILD_OF: Partial<Record<MapLayer, MapLayer>> = {
  zone: 'region',
  region: 'area',
  area: 'unit',
  unit: 'territory',
  territory: 'sub_territory',
};

function detailRoute(code: string, entityType?: MapLayer): string | null {
  const table = entityType ? TABLE_BY_TYPE[entityType] : undefined;
  if (!table) return null;
  return `/data-management/master/${table}/${encodeURIComponent(code)}`;
}

/**
 * `?focus=territory:TR001,TR004` — what a data-management table sends when the
 * user asks to see records on the map.
 *
 * Parsed rather than assumed: an unknown layer name is ignored instead of
 * filtering the map down to nothing, because a stale bookmark should show the
 * map, not an empty one.
 */
function parseFocus(raw: string | null): { layer: MapLayer; codes: string[] } | null {
  if (!raw) return null;
  const [type, list] = raw.split(':');
  const codes = (list ?? '').split(',').map((code) => code.trim()).filter(Boolean);
  const layer = LAYERS.find((entry) => entry.key === type)?.key;
  if (!layer || codes.length === 0) return null;
  return { layer, codes };
}

const DEFAULT_BOUNDARY_LEVELS = BOUNDARY_LEVELS.filter((level) => level.defaultOn)
  .map((level) => level.key);

/**
 * The mode named in the URL, or Administrative.
 *
 * A hand-typed or stale `?mapMode=` must not break the page, so anything that
 * is not a known key falls back rather than throwing or rendering an empty
 * selector — the same treatment a hand-typed filter value gets.
 */
/**
 * Marker opacity while a choropleth is painted.
 *
 * Low enough that the area colour is what the eye lands on, high enough that
 * the customer distribution is still legible as texture — hiding the markers
 * outright was the alternative and it costs the one thing the choropleth cannot
 * show, which is *where inside the area* the business actually is.
 */
const MUTED_MARKERS = 0.22;

/** Bubble radius range, in pixels, at the scale the map is read at. */
const BUBBLE_MIN = 5;
const BUBBLE_MAX = 34;

/** Every achievement band, derived so the two can never fall out of step. */
const ALL_BANDS = ACHIEVEMENT_BANDS.map((band) => band.labelKey);

/** A point the server could not score: drawn, but in no achievement colour. */
const UNSCORED_BUBBLE = '#94A3B8';

/** The eight business levels the Sales Level control offers, outermost first. */
const SALES_LEVELS: readonly { key: MapLayer; labelKey: string }[] = [
  { key: 'company', labelKey: 'map.layerCompany' },
  { key: 'bu', labelKey: 'map.layerBu' },
  { key: 'sales_line', labelKey: 'map.layerSalesLine' },
  { key: 'zone', labelKey: 'map.layerZone' },
  { key: 'region', labelKey: 'map.layerRegion' },
  { key: 'area', labelKey: 'map.layerArea' },
  { key: 'unit', labelKey: 'map.layerUnit' },
  { key: 'territory', labelKey: 'map.layerTerritory' },
  { key: 'customer', labelKey: 'map.layerCustomer' },
];

function parseSalesLevel(raw: string | null): MapLayer {
  return SALES_LEVELS.some((l) => l.key === raw) ? (raw as MapLayer) : 'customer';
}

function parseMode(raw: string | null): MapModeKey {
  return raw && raw in MODE_BY_KEY ? (raw as MapModeKey) : 'administrative';
}

export default function MapPage() {
  const t = useT();
  const { hasSection } = useAuth();
  const { resolved: theme } = useTheme();
  const [searchParams, setSearchParams] = useSearchParams();
  const focus = useMemo(() => parseFocus(searchParams.get('focus')), [searchParams]);
  // `query` already carries the global filters plus the resolved period, which
  // is exactly what every other report sends.
  const { query: filterQuery, filters, setFilterResolved } = useFilters();

  /**
   * The map mode, which is what the reader is looking *for*.
   *
   * The metric follows from it rather than being chosen separately: a mode with
   * a metric colours the areas by it, and a mode without one leaves the map
   * administrative. Keeping one control instead of two is what stops the map
   * offering a "Sales Achievement" heading over a Net Sales choropleth.
   */
  const mode = parseMode(searchParams.get('mapMode'));
  const modeSpec = MODE_BY_KEY[mode];

  /**
   * The mode lives in the URL, like every other thing this page is looking at.
   *
   * Same reason the filters do: a map someone is reading should survive a
   * refresh and be shareable as what it shows, not as a starting point the
   * recipient has to rebuild. `replace` is deliberate — flipping between modes
   * is looking around, not navigating, and each flip should not become a back
   * step to walk out of. The filters are untouched, so changing mode cannot
   * reset them and changing them cannot reset the mode.
   */
  const setMode = useCallback(
    (next: MapModeKey) => {
      setSearchParams(
        (params) => {
          const updated = new URLSearchParams(params);
          if (next === 'administrative') updated.delete('mapMode');
          else updated.set('mapMode', next);
          return updated;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const metric = modeSpec.metric ?? 'net_sales';
  const [crumbs, setCrumbs] = useState<Crumb[]>([]);
  /**
   * The one business level drawn as points.
   *
   * The map used to draw seven layers at once through a row of toggles, which
   * is a *visibility* control; what a reader of a sales map actually chooses is
   * the grain they want the business aggregated at. One level at a time is that
   * question, and it is also what keeps 2,561 markers from stacking into a
   * single blue mass. Persisted in the URL beside the mode, for the same reason.
   */
  const salesLevel = parseSalesLevel(searchParams.get('salesLevel'));
  const setSalesLevel = useCallback(
    (next: MapLayer) => {
      setSearchParams(
        (params) => {
          const updated = new URLSearchParams(params);
          if (next === 'customer') updated.delete('salesLevel');
          else updated.set('salesLevel', next);
          return updated;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const [layers, setLayers] = useState<MapLayer[]>(
    // Arriving from a table with a focus, only that layer is drawn: the point
    // of "show these three customers" is to see those three, not to find them
    // among every region and territory as well.
    () => (focus ? [focus.layer] : DEFAULT_LAYERS),
  );
  const [selected, setSelected] = useState<EntityFeatureProperties | null>(null);
  const [selectedArea, setSelectedArea] = useState<
    { level: BoundaryLevelKey; code: string; name: string } | null
  >(null);
  const [mapError, setMapError] = useState<string | null>(null);
  const [fitToken, setFitToken] = useState(0);
  const [showDiagnostics, setShowDiagnostics] = useState(false);

  const [boundaryLevels, setBoundaryLevels] =
    useState<BoundaryLevelKey[]>(DEFAULT_BOUNDARY_LEVELS);
  const [reference, setReference] = useState({ mask: true, capitals: false, lines: false });

  const config = useQuery({ queryKey: ['map-config'], queryFn: mapService.config });
  const legend = useQuery({ queryKey: ['map-legend'], queryFn: () => markerService.legend() });

  /** Drill filters: each crumb pins its level's code. */
  const drillFilters = useMemo(() => {
    const result: Record<string, string> = {};
    for (const crumb of crumbs) result[`${crumb.level}_code`] = crumb.code;
    return result;
  }, [crumbs]);

  const query = {
    ...filterQuery,
    ...drillFilters,
    layers: layers.join(','),
    metric,
    diagnostics: showDiagnostics || undefined,
  };

  const data = useQuery({
    queryKey: ['map-entities', query],
    queryFn: () => mapService.entities(query),
    enabled: config.isSuccess,
    // Keep the previous points on screen while a refetch is in flight. A map
    // that blanks whenever a filter changes is far worse than one that shows
    // slightly stale pins for a moment.
    placeholderData: keepPreviousData,
  });

  /**
   * The metric behind each administrative area.
   *
   * Geometry is explicitly *not* requested: the polygons are already in the
   * browser from `public/geo/`, and asking the server to send them again on
   * every filter change would be the exact traffic this rebuild removed. What
   * comes back is properties keyed on the P-code, which is the same code the
   * local file carries.
   */
  const metricLevel = useMemo(
    () =>
      [...BOUNDARY_LEVELS]
        .reverse()
        .find((level) => boundaryLevels.includes(level.key))?.key ?? 'district',
    [boundaryLevels],
  );

  const areaMetricQuery = { ...filterQuery, level: metricLevel, geometry: false };

  const areaData = useQuery({
    queryKey: ['map-area-metrics', areaMetricQuery],
    queryFn: () => mapService.areas(areaMetricQuery),
    enabled: config.isSuccess && boundaryLevels.length > 0,
    placeholderData: keepPreviousData,
    staleTime: 60 * 1000,
  });

  const areaMetrics = useMemo(() => {
    const result: Record<string, AreaMetric> = {};
    for (const feature of areaData.data?.features ?? []) {
      const code = String(feature.id ?? feature.properties[`${metricLevel}_code`] ?? '');
      if (!code) continue;
      result[code] = {
        stock: Number(feature.properties.stock ?? 0),
        stock_source: feature.properties.stock_source,
      };
    }
    return result;
  }, [areaData.data, metricLevel]);

  /**
   * Which modes this deployment can actually run.
   *
   * A mode is available when the server publishes the metric it names — the
   * `/api/map/config` metric list is the authority, so a mode becomes real the
   * day a metric is added behind it and needs nothing here. Modes with no metric
   * at all (Administrative, Customer Coverage) are available whenever the map
   * is, and one carrying an `unavailableKey` is always shown disabled: the point
   * is to say the dashboard means to show this and has no data for it yet.
   */
  const availableModes = useMemo(() => {
    const published = new Set((config.data?.metrics ?? []).map((m) => m.key));
    return new Map(
      MAP_MODES.map((m) => [
        m.key,
        m.unavailableKey ? false : m.metric === null || published.has(m.metric),
      ]),
    );
  }, [config.data]);

  /**
   * Aggregated points for the level being read, when a mode measures anything.
   *
   * Asked of `/api/map/data`, which aggregates the level in the warehouse and
   * returns one point per code carrying `net_sales`, `target_amount` and
   * `achievement_percent` - all summed and divided server-side. The bubble's
   * size and its colour therefore come from the same query the reports read,
   * and nothing here divides one business figure by another.
   */
  const wantsPoints = modeSpec.metric !== null;
  const pointQuery = {
    ...filterQuery,
    level: salesLevel,
    metric: modeSpec.metric ?? 'net_sales',
    cluster: false,
  };
  const pointData = useQuery({
    queryKey: ['map-points', pointQuery],
    queryFn: () => mapService.points(pointQuery),
    enabled: config.isSuccess && wantsPoints && (availableModes.get(mode) ?? false),
    placeholderData: keepPreviousData,
    staleTime: 60 * 1000,
  });

  /**
   * The bubbles, already encoded.
   *
   * Size is scaled against the largest sales figure actually present so a quiet
   * period still spreads across the full range; colour comes from the agreed
   * achievement bands, which are *not* rescaled because their breaks are the
   * meaning. A point the server could not score has no achievement and is not
   * given a colour it did not earn.
   */
  const bubbles = useMemo(() => {
    if (!wantsPoints) return null;
    const points = pointData.data?.points ?? [];
    if (!points.length) return null;
    const sales = points.map((p) => Number(p.measures.net_sales ?? 0));
    const max = Math.max(...sales, 0);
    return points.map((point, index) => {
      const achieved = point.measures.achievement_percent;
      const band = achieved == null
        ? null
        : ACHIEVEMENT_BANDS.find((b) => b.max === null || Number(achieved) < b.max);
      return {
        code: point.code,
        label: point.label,
        latitude: point.latitude,
        longitude: point.longitude,
        radius: max > 0
          ? BUBBLE_MIN + (sales[index] / max) * (BUBBLE_MAX - BUBBLE_MIN)
          : BUBBLE_MIN,
        colour: band?.color ?? UNSCORED_BUBBLE,
        netSales: sales[index],
        targetAmount: Number(point.measures.target_amount ?? 0),
        achievement: achieved == null ? null : Number(achieved),
      };
    });
  }, [wantsPoints, pointData.data]);

  /**
   * No area choropleth. Deliberately, and this is the reason.
   *
   * A choropleth needs a metric attributed to the polygons it paints, and this
   * warehouse has none: `map/areas.py` returns `{}` from both `stock_by_area`
   * and `_optional_metrics`, each with a comment explaining that nothing which
   * replaced the warehouse dimension carries a coordinate or a district. An
   * earlier version of this page painted `feature.properties.stock` and so
   * coloured every area by the configured default — one flat value dressed up
   * as a measurement, which is precisely the invented figure this application
   * refuses to draw.
   *
   * The metrics are real at the *point* level, where each region, territory or
   * customer has its own coordinate, and that is where the map reads them.
   * Restoring an area choropleth needs sales attributed to admin areas, which
   * means point-in-polygon over customer coordinates — and only 846 of 2,561
   * customers are placed, so it would colour a third of the business and read
   * as all of it.
   */
  const choropleth = null;

  /**
   * Which achievement bands are drawn.
   *
   * In the URL beside the mode, because it changes what the map shows and a map
   * someone is reading should survive a refresh. Omitted while every band is on,
   * so the common case leaves no parameter behind. A stale or hand-typed value
   * naming no band we know falls back to all of them rather than emptying the
   * map — the same treatment every other parameter on this page gets.
   */
  const activeBands = useMemo(() => {
    const raw = searchParams.get('bands');
    if (!raw) return new Set(ALL_BANDS);
    const wanted = new Set(raw.split(',').filter((key) => ALL_BANDS.includes(key)));
    return wanted.size > 0 ? wanted : new Set(ALL_BANDS);
  }, [searchParams]);

  const setActiveBands = useCallback(
    (next: Set<string>) => {
      setSearchParams(
        (params) => {
          const updated = new URLSearchParams(params);
          if (next.size === ALL_BANDS.length) updated.delete('bands');
          else updated.set('bands', ALL_BANDS.filter((key) => next.has(key)).join(','));
          return updated;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  /**
   * The bubbles the band chips leave on the map.
   *
   * A filter over what is drawn, never over what was measured: the figures in
   * the panels and the ranking still describe the whole scope, because hiding a
   * band is a way of looking rather than a narrowing of the report. A point the
   * server could not score has no band and stays — it has no chip that could
   * bring it back, so nothing may hide it.
   */
  const visibleBubbles = useMemo(() => {
    if (!bubbles || modeSpec.metric !== 'achievement') return bubbles;
    if (activeBands.size === ALL_BANDS.length) return bubbles;
    return bubbles.filter((point) => {
      const achieved = point.achievement;
      if (achieved === null) return true;
      const band = ACHIEVEMENT_BANDS.find((b) => b.max === null || achieved < b.max);
      return band ? activeBands.has(band.labelKey) : true;
    });
  }, [bubbles, modeSpec.metric, activeBands]);

  /* ----------------------------------------------------------------- kpis */

  /**
   * The scope's own totals, and where the money figures on this page come from.
   *
   * `get_target_achievement` publishes them in `values`: target, actual and the
   * achievement between them, summed over every group in scope by the server —
   * `target_vs_actual` aggregates at `MAX_ROWS` and applies the caller's limit
   * only to the rows it hands back, so these are the whole scope and not the
   * top fifty of it. Nothing here is added up in the browser, which is what
   * makes these safe to show beside the Dashboard's.
   *
   * Pinned at region rather than at whatever the ranking is showing: a scope
   * total does not depend on how it was grouped, and a KPI that vanished
   * because somebody changed the ranking selector would read as missing data.
   * When the ranking is at region too, this is the same query.
   */
  const summaryQuery = { ...filterQuery, level: 'region', limit: 50 };
  const summary = useQuery({
    queryKey: ['map-ranking', summaryQuery],
    queryFn: () => performanceService.page(summaryQuery),
    placeholderData: keepPreviousData,
    staleTime: 60 * 1000,
  });

  const scopeTotals = summary.data?.achievement?.values as
    | { target?: number; actual?: number; achievement_percent?: number | null }
    | undefined;

  /* ------------------------------------------------------------- ranking */

  /**
   * The level the ranking beside the map ranks.
   *
   * In the URL beside the mode and the sales level, for the same reason: a map
   * someone is reading should still be the map they were reading after a
   * refresh, and should be shareable as what it shows.
   */
  const rankLevel = searchParams.get('rankLevel') ?? 'region';
  const setRankLevel = useCallback(
    (next: string) => {
      setSearchParams(
        (params) => {
          const updated = new URLSearchParams(params);
          if (next === 'region') updated.delete('rankLevel');
          else updated.set('rankLevel', next);
          return updated;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  /* The same endpoint the Performance page reads, with the same filters. The
     map ranks nothing itself, so the two surfaces cannot disagree about a
     number — and the drill chain comes from the response rather than from a
     list here that could fall behind the one the server walks. */
  const rankQuery = { ...filterQuery, level: rankLevel, limit: 50 };
  const ranking = useQuery({
    queryKey: ['map-ranking', rankQuery],
    queryFn: () => performanceService.page(rankQuery),
    placeholderData: keepPreviousData,
    staleTime: 60 * 1000,
  });

  const rankRows = useMemo<RankRow[]>(() => {
    const achievement = new Map<string, number | null>();
    for (const row of ranking.data?.achievement?.rows ?? []) {
      const value = row.achievement_percent;
      achievement.set(String(row.code), value === null || value === undefined
        ? null
        : Number(value));
    }
    const rows = (ranking.data?.performance?.rows ?? []).map((row) => ({
      code: String(row.code ?? ''),
      label: String(row.label ?? row.code ?? ''),
      achievement: achievement.get(String(row.code)) ?? null,
      net_sales: Number(row.net_sales ?? 0),
    }));
    // Achievement is the order the business asks for. A level no target covers
    // has none — those rows fall to the bottom rather than being read as zero,
    // and a level with no targets at all is therefore ordered by net sales.
    return rows.sort(
      (left, right) =>
        (right.achievement ?? -1) - (left.achievement ?? -1) ||
        right.net_sales - left.net_sales,
    );
  }, [ranking.data]);

  /* Every drill level is `<level>_code` as a filter, so this is derived rather
     than a second table of the same six names kept in step by hand. */
  const rankFilterLevel = `${rankLevel}_code` as FilterLevel;
  const rankChain = useMemo(
    () =>
      ranking.data?.drill_chain?.length ? ranking.data.drill_chain : [rankLevel],
    [ranking.data, rankLevel],
  );

  /**
   * Clicking a rank does what clicking the map does: it narrows the filters,
   * which is what every surface on this page already reads. The ancestors come
   * with it, so the bar shows the whole path rather than one orphaned level,
   * and the ranking itself moves one level deeper — which is the drill.
   */
  const selectRank = useCallback(
    (row: RankRow) => {
      setFilterResolved(rankFilterLevel, row.code);
      const next = rankChain[rankChain.indexOf(rankLevel) + 1];
      if (next) setRankLevel(next);
      setFitToken((token) => token + 1);
    },
    [rankFilterLevel, rankChain, rankLevel, setFilterResolved, setRankLevel],
  );

  /** Sales Level options, each carrying how many records are in scope. */
  const salesLevelOptions = useMemo(
    () =>
      SALES_LEVELS.map((level) => ({
        ...level,
        count: data.data?.counts[level.key] ?? 0,
      })),
    [data.data],
  );

  /** Boundary appearance, from the database. Never decided in the browser. */
  const areaStyles = useMemo(() => {
    const styles = config.data?.administrative_areas?.styles ?? {};
    const result: Partial<Record<BoundaryLevelKey, AreaStyle>> = {};
    for (const level of BOUNDARY_LEVELS) {
      if (styles[level.key]) result[level.key] = styles[level.key];
    }
    return result;
  }, [config.data]);

  /* ----------------------------------------------------------- map payload */

  // One level at a time now: the Sales Level control *is* the layer choice.
  const visibleLayers = useMemo(() => new Set<MapLayer>([salesLevel]), [salesLevel]);
  const focusCodes = useMemo(
    () => (focus ? new Set(focus.codes) : null),
    [focus],
  );

  const entityCollection = useMemo(
    () =>
      buildEntityCollection(data.data?.entities, {
        visibleLayers,
        focusCodes,
        focusLayer: focus?.layer ?? null,
      }),
    [data.data, visibleLayers, focusCodes, focus],
  );

  const drawnTypes = useMemo(
    () => new Set(entityCollection.features.map((f) => f.properties.entityType)),
    [entityCollection],
  );

  /**
   * The basemap for the theme that is actually on screen.
   *
   * The server offers both styles in one response rather than being asked twice,
   * so switching to dark mode swaps the basemap without a round trip. The
   * constants are the fallback for a backend that has not been updated yet.
   */
  const basemapStyle =
    theme === 'dark'
      ? config.data?.basemap?.style_url_dark ?? BASEMAP_STYLES.dark
      : config.data?.basemap?.style_url ?? BASEMAP_STYLES.light;

  const metricSpec = config.data?.metrics.find((m) => m.key === metric);
  const formatValue = useCallback(
    (value: number) =>
      metricSpec?.unit === 'currency' ? formatAmount(value) : formatCount(value),
    [metricSpec],
  );

  /* ------------------------------------------------------------ interaction */

  /**
   * Clicking a point narrows the filter to it.
   *
   * Drilling changes the *scope*, not which layers are drawn. A customer or
   * warehouse is a leaf, so clicking one selects it for the detail panel rather
   * than filtering to nothing.
   */
  const drillInto = useCallback((properties: EntityFeatureProperties) => {
    setSelected(properties);
    const type = properties.entityType;
    if (!type || !CHILD_OF[type]) return;
    setCrumbs((previous) => [
      ...previous.filter((crumb) => crumb.level !== type),
      { level: type, label: properties.name, code: properties.code },
    ]);
  }, []);

  const selectArea = useCallback(
    (level: BoundaryLevelKey, code: string, name: string) =>
      setSelectedArea({ level, code, name }),
    [],
  );

  function goToCrumb(index: number) {
    setCrumbs(crumbs.slice(0, index + 1));
    setSelected(null);
    setFitToken((token) => token + 1);
  }

  const coverage = config.data?.coverage ?? [];
  const deepest = crumbs[crumbs.length - 1]?.level ?? 'region';
  const levelCoverage = coverage.find((row) => row.entity_type === deepest);
  const selectedAreaMetric = selectedArea ? areaMetrics[selectedArea.code] : undefined;

  return (
    <>
      <PageHeader
        title={t('map.title')}
        period={data.data?.period}
        description={data.data?.scope_description}
        actions={
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => void data.refetch()}
            >
              <RefreshCw size={14} />
              {t('common.retry')}
            </button>
            {hasSection('map_settings') && (
              <Link to="/admin/map-settings/markers" className="btn-secondary">
                <Settings2 size={14} />
                {t('map.markerSettings')}
              </Link>
            )}
          </div>
        }
      />

      {/* The map reads the same global filters as every other page — and it
          always sent them: `filterQuery` goes into all three of its queries, so
          the bar is the control for a scope the endpoints have honoured all
          along rather than a new one. The period comes with it, the same URL
          parameter the dashboard uses, so a period chosen here is the period
          chosen there. The map's own drill is a different thing and stays: the
          filters say which part of the business is in scope, the breadcrumb
          says how far into it the reader has walked. */}
      <GlobalFilterBar />

      <div className="card mb-3 flex flex-wrap items-center gap-3 p-3">
        <span className="h-6 w-px bg-slate-200 dark:bg-slate-700" aria-hidden="true" />

        {/* Why, not just that. A mode greyed out with no reason reads as a bug;
            named as "no promotion data exists", it reads as a fact about the
            platform, which is what it is. */}
        {modeSpec.unavailableKey && (
          <p className="max-w-md text-xs text-slate-500 dark:text-slate-400">
            {t(modeSpec.unavailableKey)}
          </p>
        )}

        <nav className="flex flex-wrap items-center gap-1 text-sm" aria-label="Drill path">
          <button
            type="button"
            className="rounded px-2 py-0.5 text-brand-600 hover:bg-brand-50 dark:hover:bg-slate-800"
            onClick={() => goToCrumb(-1)}
          >
            {t('map.allAreas')}
          </button>
          {crumbs.map((crumb, index) => (
            <span key={`${crumb.level}-${crumb.code}`} className="flex items-center gap-1">
              <ChevronRight size={12} className="text-slate-400" />
              <button
                type="button"
                className="rounded px-2 py-0.5 text-brand-600 hover:bg-brand-50 dark:hover:bg-slate-800"
                onClick={() => goToCrumb(index)}
              >
                {crumb.label}
              </button>
            </span>
          ))}
        </nav>

        <span className="ml-auto text-xs text-slate-500">
          {t('map.entitiesPlotted', {
            placed: formatCount(data.data?.totals.placed ?? 0),
            total: formatCount(data.data?.totals.entities ?? 0),
          })}
        </span>
      </div>

      {/* ---- workspace counts ----
          Every figure here is one the server states outright. The money and the
          percentage come from `get_target_achievement`'s own totals, summed
          across the scope by the backend; the browser adds nothing up, which is
          the rule that keeps this strip and the Dashboard from ever disagreeing.
          Growth stays absent, because no endpoint on this path publishes it. */}
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
        {[
          {
            key: 'achievement',
            label: t('target.achievement'),
            value: formatPercent(scopeTotals?.achievement_percent ?? null),
            // The bar is the same figure, not a second one: capped at the full
            // width so an over-achieving scope does not draw past the card.
            bar: Math.min(Number(scopeTotals?.achievement_percent ?? 0), 100),
            hint: scopeTotals
              ? `${formatAmount(scopeTotals.actual ?? 0)} / ${formatAmount(scopeTotals.target ?? 0)}`
              : undefined,
          },
          { key: 'sales', label: t('sales.netSales'),
            value: formatAmount(scopeTotals?.actual ?? null) },
          { key: 'target', label: t('target.title'),
            value: formatAmount(scopeTotals?.target ?? null) },
          { key: 'placed', label: t('map.onTheMap'),
            value: formatCount(data.data?.totals.placed ?? 0),
            hint: `${t('common.of')} ${formatCount(data.data?.totals.entities ?? 0)}` },
          { key: 'unplaced', label: t('map.notPlaced'),
            value: formatCount(data.data?.totals.unplaced ?? 0) },
        ].map((tile) => (
          <div key={tile.key} className="card p-3">
            <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
              {tile.label}
            </p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{tile.value}</p>
            {tile.bar !== undefined && (
              <div className="mt-2 h-1 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
                <span
                  className="block h-full rounded-full"
                  style={{
                    width: `${Math.max(2, tile.bar)}%`,
                    background: bandColor(scopeTotals?.achievement_percent ?? null),
                  }}
                />
              </div>
            )}
            {tile.hint && (
              <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                {tile.hint}
              </p>
            )}
          </div>
        ))}
      </div>

      {/* ---- geographic coverage ----
          Derived organisational points are approximations, and this is where
          the map says so. Every figure is the server's own count from
          `geo.coverage` — nothing here is written down, so the day somebody
          geocodes another two hundred customers the line moves on its own. */}
      {(() => {
        const level = coverage.find((row) => row.entity_type === salesLevel);
        const customers = coverage.find((row) => row.entity_type === 'customer');
        if (!level && !customers) return null;
        return (
          <div className="card mb-4 flex flex-wrap items-center gap-x-6 gap-y-2 p-3 text-xs">
            {level && (
              <span className="flex items-center gap-1.5">
                <MapPin size={13} className="text-slate-400" />
                <span className="font-medium">{level.label}</span>
                <span className="tabular-nums">
                  {formatCount(level.placed)} / {formatCount(level.total)}
                </span>
                <span className="text-slate-500">{t('map.coveragePlaced')}</span>
                {level.derived > 0 && (
                  <span className="rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                    {formatCount(level.derived)} {t('map.coverageApproximate')}
                  </span>
                )}
                {level.missing > 0 && (
                  <span className="text-slate-500">
                    · {formatCount(level.missing)} {t('map.coverageNeedLocation')}
                  </span>
                )}
              </span>
            )}
            {customers && (
              <span className="flex items-center gap-1.5 text-slate-500">
                <span className="font-medium text-slate-600 dark:text-slate-300">
                  {customers.label}
                </span>
                <span className="tabular-nums">
                  {formatCount(customers.placed)} / {formatCount(customers.total)}
                </span>
                {t('map.coveragePlaced')}
              </span>
            )}
          </div>
        );
      })()}

      {/* ---- coverage warning ---- */}
      {config.data && !config.data.has_locations && (
        <div className="card mb-4 flex items-start gap-3 border-l-4 border-l-amber-500 p-4">
          <MapPin size={18} className="mt-0.5 shrink-0 text-amber-500" />
          <div>
            <p className="text-sm font-medium">{t('map.noLocationsTitle')}</p>
            <p className="mt-1 text-xs text-slate-500">{t('map.noLocationsBody')}</p>
            {hasSection('data_upload') && (
              <Link to="/data-upload" className="btn-secondary mt-2 inline-flex">
                {t('map.uploadLocations')}
              </Link>
            )}
          </div>
        </div>
      )}

      {focus && (
        <div className="card mb-4 flex flex-wrap items-center gap-2 border-l-4 border-l-brand-500 p-3 text-sm">
          <MapPin size={16} className="text-brand-500" />
          <span>
            {t('map.focused', {
              count: String(focus.codes.length),
              type: focus.layer.replace(/_/g, ' '),
            })}
          </span>
          <button
            type="button"
            className="btn-ghost ml-auto px-2 py-1 text-xs"
            onClick={() => {
              // Clearing the focus restores the normal layer set, otherwise the
              // map would stay narrowed to one type with nothing explaining why.
              setSearchParams({}, { replace: true });
              setLayers(DEFAULT_LAYERS);
              setFitToken((token) => token + 1);
            }}
          >
            {t('map.clearFocus')}
          </button>
        </div>
      )}

      {mapError && (
        <p className="card mb-4 border-l-4 border-l-amber-500 p-3 text-sm text-amber-700 dark:text-amber-300">
          {mapError}
        </p>
      )}

      <div className="grid gap-4 xl:grid-cols-[14rem_minmax(0,1fr)_20rem]">
        {/* ---- left rail: the map's own controls ----
            Not filters. Choosing a view or a level changes what is *drawn* and
            selects nothing away, which is why they are here and not in the
            filter bar above. They used to sit inside the map, over the country
            they were changing. */}
        <div className="space-y-4">
          <div className="card flex flex-col gap-3 p-3">
          <label
            className="flex items-center gap-2 text-xs font-medium text-slate-500"
            htmlFor="map-view-control"
          >
            {t('map.mode')}
            <select
              id="map-view-control"
              className="input w-auto min-w-[11rem] py-1.5 text-sm font-normal text-slate-900 dark:text-slate-100"
              value={mode}
              onChange={(event) => setMode(event.target.value as MapModeKey)}
            >
              {MAP_MODES.map((option) => {
                const enabled = availableModes.get(option.key);
                return (
                  <option key={option.key} value={option.key} disabled={!enabled}>
                    {t(option.labelKey)}
                    {enabled ? '' : ` — ${t('map.modeUnavailable')}`}
                  </option>
                );
              })}
            </select>
          </label>

          <label
            className="flex items-center gap-2 text-xs font-medium text-slate-500"
            htmlFor="sales-level-control"
          >
            {t('map.salesLevel')}
            <select
              id="sales-level-control"
              className="input w-auto min-w-[10rem] py-1.5 text-sm font-normal text-slate-900 dark:text-slate-100"
              value={salesLevel}
              onChange={(event) => setSalesLevel(event.target.value as MapLayer)}
            >
              {salesLevelOptions.map((level) => (
                <option key={level.key} value={level.key}>
                  {t(level.labelKey)} ({level.count})
                </option>
              ))}
            </select>
          </label>
          </div>
          {modeSpec.metric === 'achievement' && (
            <MapBandChips active={activeBands} onChange={setActiveBands} />
          )}
          <MapLayerPanel
            activeLevels={boundaryLevels}
            onLevelsChange={setBoundaryLevels}
            showMask={reference.mask}
            showCapitals={reference.capitals}
            showAdminLines={reference.lines}
            onReferenceChange={setReference}
          />
        </div>


        <Section title={data.data?.metric_label ?? t('map.title')}>
          <QueryState
            isLoading={config.isLoading || data.isLoading}
            error={config.error ?? data.error}
            onRetry={() => void data.refetch()}
            skeleton={<CardSkeleton rows={8} />}
          >
            <BusinessMap
              styleUrl={basemapStyle}
              theme={theme}
              activeLevels={boundaryLevels}
              areaStyles={areaStyles}
              entities={entityCollection}
              markers={data.data?.markers}
              areaMetrics={areaMetrics}
              choropleth={choropleth}
              bubbles={visibleBubbles}
              markerEmphasis={choropleth ? MUTED_MARKERS : 1}
              showMask={reference.mask}
              showCapitals={reference.capitals}
              showAdminLines={reference.lines}
              onEntitySelect={drillInto}
              onAreaSelect={selectArea}
              onError={setMapError}
              formatValue={formatValue}
              fitToken={fitToken}
            />

            {data.data && entityCollection.features.length === 0 && (
              <EmptyState
                message={
                  levelCoverage && levelCoverage.placed === 0
                    ? t('map.levelNotPlaced', { level: deepest.replace(/_/g, ' ') })
                    : t('common.noData')
                }
                icon={<MapPin size={32} />}
              />
            )}
          </QueryState>
        </Section>

        {/* ---- side panel ---- */}
        <div className="space-y-4">
          {/* First, because "which is ahead" is the question a reader arrives
              with; the map answers "where" and the two are read together. */}
          <MapRanking
            level={rankLevel}
            levels={rankChain}
            levelLabel={(value) =>
              t(FILTER_LABELS[`${value}_code` as FilterLevel] ?? value)
            }
            onLevelChange={setRankLevel}
            rows={rankRows}
            activeCode={filters[rankFilterLevel]}
            onSelect={selectRank}
            loading={ranking.isFetching}
          />

          <Section title={t('map.inScope')}>
            <p className="mb-2 text-[11px] text-slate-500">{t('map.countsHint')}</p>
            <dl className="space-y-1 text-sm">
              {LAYERS.filter((layer) => (data.data?.counts[layer.key] ?? 0) > 0).map(
                (layer) => (
                  <div key={layer.key} className="flex justify-between gap-2">
                    <dt
                      className={
                        layers.includes(layer.key)
                          ? 'text-slate-600 dark:text-slate-300'
                          : 'text-slate-400'
                      }
                    >
                      {t(layer.labelKey)}
                      {!layers.includes(layer.key) && (
                        <span className="ml-1 text-[10px]">({t('map.hidden')})</span>
                      )}
                    </dt>
                    <dd className="tabular-nums">
                      {formatCount(data.data?.counts[layer.key] ?? 0)}
                    </dd>
                  </div>
                ),
              )}
              <div className="mt-2 flex justify-between border-t border-slate-200 pt-2 dark:border-slate-700">
                <dt className="text-slate-500">{t('map.plotted')}</dt>
                <dd className="tabular-nums">{data.data?.totals.placed ?? 0}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-500">{t('map.unplaced')}</dt>
                <dd className="tabular-nums">{data.data?.totals.unplaced ?? 0}</dd>
              </div>
            </dl>

            {selected && (
              <div className="mt-3 rounded-lg border border-slate-200 p-3 dark:border-slate-700">
                <p className="text-[10px] uppercase tracking-wide text-slate-400">
                  {selected.entityType?.replace(/_/g, ' ')}
                </p>
                <p className="text-sm font-semibold">{selected.name}</p>
                <p className="text-[11px] text-slate-500">{selected.code}</p>
                {selected.parentCode && (
                  <p className="text-[11px] text-slate-400">
                    {selected.parentType?.replace(/_/g, ' ')}: {selected.parentCode}
                  </p>
                )}
                <p className="mt-1 text-lg font-semibold tabular-nums">
                  {formatValue(selected.value)}
                </p>
                <p className="mt-1 text-[11px] text-slate-400">
                  {selected.latitude.toFixed(4)}, {selected.longitude.toFixed(4)}
                </p>
                {/* Map → table. Clicking a marker should be able to end at the
                    record, not just at a tooltip about it. */}
                {detailRoute(selected.code, selected.entityType) && hasSection('master_data') && (
                  <Link
                    to={detailRoute(selected.code, selected.entityType) as string}
                    className="btn-secondary mt-2 w-full justify-center text-xs"
                  >
                    {t('map.viewDetails')}
                  </Link>
                )}
              </div>
            )}
          </Section>

          {/* The clicked area. Everything shown comes from the API — a metric
              the data cannot support is absent rather than zero. */}
          {selectedArea && (
            <Section
              title={t('map.areaDetails')}
              actions={
                <button
                  type="button"
                  className="btn-ghost px-2 py-1 text-xs"
                  onClick={() => setSelectedArea(null)}
                >
                  {t('common.close')}
                </button>
              }
            >
              <p className="text-base font-semibold">{selectedArea.name}</p>
              <p className="text-xs text-slate-500">
                {t(`map.level${selectedArea.level.charAt(0).toUpperCase()}${selectedArea.level.slice(1)}`)}
              </p>

              <dl className="mt-3 space-y-1 text-sm">
                <div className="flex justify-between gap-2">
                  <dt className="text-slate-500">{t('map.areaCode')}</dt>
                  <dd className="font-mono text-xs">{selectedArea.code}</dd>
                </div>
                {selectedAreaMetric ? (
                  <>
                    <div className="flex justify-between gap-2 border-t border-slate-200 pt-1 dark:border-slate-700">
                      <dt className="text-slate-500">{t('map.stock')}</dt>
                      <dd className="tabular-nums font-semibold">
                        {formatCount(selectedAreaMetric.stock)}
                      </dd>
                    </div>
                    {selectedAreaMetric.stock_source === 'default' && (
                      <p className="text-[11px] italic text-slate-400">
                        {t('map.stockDefault')}
                      </p>
                    )}
                  </>
                ) : (
                  <p className="border-t border-slate-200 pt-1 text-[11px] text-slate-400 dark:border-slate-700">
                    {t('map.noAreaMetric')}
                  </p>
                )}
              </dl>
            </Section>
          )}

          {import.meta.env.DEV && (
            <Section title={t('map.diagnostics')}>
              <label className="flex cursor-pointer items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-brand-600"
                  checked={showDiagnostics}
                  onChange={(event) => setShowDiagnostics(event.target.checked)}
                />
                {t('map.showDiagnostics')}
              </label>
              <p className="mt-1 text-[11px] text-slate-400">{t('map.diagnosticsHint')}</p>
              {data.data?.diagnostics && (
                <pre className="mt-2 max-h-64 overflow-auto rounded bg-slate-900 p-2 text-[10px] leading-relaxed text-slate-100">
                  {JSON.stringify(data.data.diagnostics, null, 1)}
                </pre>
              )}
            </Section>
          )}

          {(data.data?.unplaced.length ?? 0) > 0 && (
            <Section title={t('map.unplacedTitle')}>
              <p className="mb-2 flex items-start gap-2 text-xs text-slate-500">
                <AlertTriangle size={12} className="mt-0.5 shrink-0 text-amber-500" />
                {t('map.unplacedHint')}
              </p>
              <ul className="max-h-48 space-y-1 overflow-y-auto text-sm">
                {(data.data?.unplaced ?? []).map((row) => (
                  <li key={`${row.type}-${row.code}`} className="flex justify-between gap-2">
                    <span className="truncate">{row.name}</span>
                    <span className="shrink-0 text-[11px] text-slate-400">
                      {row.type.replace(/_/g, ' ')}
                    </span>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title={t('map.legend')}>
            <QueryState
              isLoading={legend.isLoading}
              error={legend.error}
              onRetry={() => void legend.refetch()}
              skeleton={<CardSkeleton rows={3} />}
            >
              <MapLegend
                entries={legend.data?.entries}
                activeLevels={boundaryLevels}
                areaStyles={areaStyles}
                drawnTypes={drawnTypes}
                mode={mode}
                modeAvailable={availableModes.get(mode) ?? false}
              />
            </QueryState>
          </Section>
        </div>
      </div>
    </>
  );
}
