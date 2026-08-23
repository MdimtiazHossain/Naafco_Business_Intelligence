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
  Eye,
  EyeOff,
  Layers,
  MapPin,
  RefreshCw,
  Settings2,
} from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, EmptyState, QueryState } from '../components/States';
import { BusinessMap, type AreaMetric } from '../components/map/BusinessMap';
import { MapLegend } from '../components/map/MapLegend';
import {
  BASEMAP_STYLES,
  BOUNDARY_LEVELS,
  type BoundaryLevelKey,
} from '../components/map/mapConfig';
import {
  buildEntityCollection,
  type EntityFeatureProperties,
} from '../components/map/businessGeoJson';
import { GlobalFilterBar } from '../filters/GlobalFilterBar';
import { useAuth } from '../contexts/AuthContext';
import { useFilters } from '../contexts/FilterContext';
import { useT } from '../contexts/I18nContext';
import { useTheme } from '../contexts/ThemeContext';
import { mapService, markerService } from '../services';
import { formatAmount, formatCount } from '../utils/format';
import type { AreaStyle, MapLayer } from '../types/api';

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

export default function MapPage() {
  const t = useT();
  const { hasSection } = useAuth();
  const { resolved: theme } = useTheme();
  const [searchParams, setSearchParams] = useSearchParams();
  const focus = useMemo(() => parseFocus(searchParams.get('focus')), [searchParams]);
  // `query` already carries the global filters plus the resolved period, which
  // is exactly what every other report sends.
  const { query: filterQuery } = useFilters();

  const [metric, setMetric] = useState('net_sales');
  const [crumbs, setCrumbs] = useState<Crumb[]>([]);
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

  const visibleLayers = useMemo(() => new Set(layers), [layers]);
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

  function toggleLayer(layer: MapLayer) {
    setLayers((previous) =>
      previous.includes(layer)
        ? previous.filter((entry) => entry !== layer)
        : [...previous, layer],
    );
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

      <GlobalFilterBar />

      {/* ---- metric, breadcrumb ---- */}
      <div className="card mb-3 flex flex-wrap items-center gap-3 p-3">
        <select
          className="input max-w-[13rem]"
          value={metric}
          onChange={(event) => setMetric(event.target.value)}
          aria-label={t('map.metric')}
        >
          {(config.data?.metrics ?? []).map((option) => (
            <option key={option.key} value={option.key}>
              {option.label}
            </option>
          ))}
        </select>

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

      {/* ---- entity layer toggles: visibility only, never the filter ---- */}
      <div className="card mb-4 flex flex-wrap items-center gap-2 p-3">
        <span className="flex items-center gap-1.5 text-xs font-medium text-slate-500">
          <Layers size={14} />
          {t('map.layers')}
        </span>
        {LAYERS.map((layer) => {
          const on = layers.includes(layer.key);
          const inScope = data.data?.counts[layer.key] ?? 0;
          return (
            <button
              key={layer.key}
              type="button"
              onClick={() => toggleLayer(layer.key)}
              aria-pressed={on}
              className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors ${
                on
                  ? 'border-brand-500 bg-brand-50 text-brand-700 dark:bg-slate-800 dark:text-brand-300'
                  : 'border-slate-200 text-slate-500 hover:border-slate-300 dark:border-slate-700'
              }`}
            >
              {on ? <Eye size={12} /> : <EyeOff size={12} />}
              {t(layer.labelKey)}
              <span className="tabular-nums text-slate-400">{inScope}</span>
            </button>
          );
        })}
        <span className="ml-auto text-[11px] text-slate-400">{t('map.adminLevelsHint')}</span>
      </div>

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

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
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
              onLevelsChange={setBoundaryLevels}
              areaStyles={areaStyles}
              entities={entityCollection}
              markers={data.data?.markers}
              areaMetrics={areaMetrics}
              showMask={reference.mask}
              showCapitals={reference.capitals}
              showAdminLines={reference.lines}
              onReferenceChange={setReference}
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
              />
            </QueryState>
          </Section>
        </div>
      </div>
    </>
  );
}
