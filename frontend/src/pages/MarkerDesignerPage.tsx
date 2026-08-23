/**
 * Marker / Shape Designer: `/admin/map-settings/marker-designer[/:id]`.
 *
 * Three panes: entity and design type on the left, a live canvas in the middle,
 * properties on the right. The canvas is **SVG**, not raster, for two reasons —
 * the output is a vector definition, and points can be dragged as real elements
 * rather than hit-tested against pixels.
 *
 * The preview is rendered by the *server*, debounced, from the same renderer the
 * map uses. That is what makes "what you see is what the map draws" true rather
 * than aspirational: there is no second drawing implementation in the browser to
 * drift. The Shape Builder overlay on top of it is editing chrome only — grid,
 * handles, guides — and never contributes to the saved artwork.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft,
  Grid3x3,
  Map as MapIcon,
  Redo2,
  RotateCcw,
  Save,
  Trash2,
  Undo2,
  Upload,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { MarkerPreview } from '../components/MarkerPreview';
import { PageHeader, Section } from '../components/PageHeader';
import { CardSkeleton, QueryState } from '../components/States';
import { useDebounced } from '../hooks/useDebounced';
import { useT } from '../contexts/I18nContext';
import { markerService } from '../services';
import type {
  MarkerDefinition,
  MarkerDesignType,
  MarkerPreview as MarkerPreviewData,
} from '../types/api';

// ---------------------------------------------------------------------------
// Defaults and helpers
// ---------------------------------------------------------------------------

const EMPTY: MarkerDefinition = {
  shape: {
    kind: 'builtin',
    builtin: 'circle',
    points: [],
    path: null,
    closed: true,
    fill: '#2563EB',
    stroke: '#FFFFFF',
    stroke_width: 2,
    opacity: 1,
    rotation: 0,
    size: 36,
    shadow: { enabled: false, colour: '#00000040', blur: 3, offset_x: 0, offset_y: 2 },
  },
  icon: {
    enabled: false,
    source: 'library',
    name: null,
    asset_id: null,
    size: 16,
    colour: '#FFFFFF',
    offset_x: 0,
    offset_y: 0,
    opacity: 1,
  },
  label: {
    enabled: false,
    template: '{Code}',
    font_size: 11,
    font_weight: 'semibold',
    colour: '#FFFFFF',
    align: 'center',
    position: 'below',
    offset_x: 0,
    offset_y: 0,
    halo: true,
    halo_colour: '#000000',
  },
  badge: {
    enabled: false,
    kind: 'active',
    position: 'top-right',
    fill: '#16A34A',
    stroke: '#FFFFFF',
    size: 10,
    text: null,
  },
  background: {
    enabled: false,
    fill: '#FFFFFF',
    stroke: '#CBD5E1',
    stroke_width: 1,
    corner_radius: 6,
    padding: 4,
    opacity: 1,
  },
  notes: null,
};

/** The design surface, in the same origin-centred units the backend uses. */
const CANVAS = 240;
const HALF = CANVAS / 2;

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** A regular star, matching the backend's `star_points` so the two agree. */
function starPoints(size: number, points = 5, innerRatio = 0.4): [number, number][] {
  const outer = size / 2;
  const inner = outer * innerRatio;
  const result: [number, number][] = [];
  for (let index = 0; index < points * 2; index += 1) {
    const radius = index % 2 === 0 ? outer : inner;
    const angle = -Math.PI / 2 + (Math.PI * index) / points;
    result.push([
      Number((radius * Math.cos(angle)).toFixed(2)),
      Number((radius * Math.sin(angle)).toFixed(2)),
    ]);
  }
  return result;
}

function polygonPoints(size: number, sides: number): [number, number][] {
  const radius = size / 2;
  return Array.from({ length: sides }, (_, index) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * index) / sides;
    return [
      Number((radius * Math.cos(angle)).toFixed(2)),
      Number((radius * Math.sin(angle)).toFixed(2)),
    ] as [number, number];
  });
}

// ---------------------------------------------------------------------------
// Small controls
// ---------------------------------------------------------------------------

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

function ColourInput({
  value,
  onChange,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
}) {
  return (
    <Field label={label}>
      <div className="flex items-center gap-2">
        <input
          type="color"
          className="h-9 w-10 shrink-0 cursor-pointer rounded border border-slate-300 bg-transparent dark:border-slate-600"
          // A colour input only understands #rrggbb; the model also permits
          // #rgb and #rrggbbaa, so the swatch shows a best effort and the text
          // box remains authoritative.
          value={/^#[0-9a-fA-F]{6}$/.test(value) ? value : '#000000'}
          onChange={(event) => onChange(event.target.value.toUpperCase())}
          aria-label={label}
        />
        <input
          className="input font-mono text-xs"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          spellCheck={false}
        />
      </div>
    </Field>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
  suffix,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
  suffix?: string;
}) {
  return (
    <Field label={`${label} — ${value}${suffix ?? ''}`}>
      <input
        type="range"
        className="w-full accent-brand-600"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </Field>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 py-1 text-sm">
      <input
        type="checkbox"
        className="h-4 w-4 accent-brand-600"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Shape builder canvas
// ---------------------------------------------------------------------------

function ShapeBuilderCanvas({
  points,
  snap,
  gridSize,
  onChange,
  selected,
  onSelect,
}: {
  points: [number, number][];
  snap: boolean;
  gridSize: number;
  onChange: (points: [number, number][]) => void;
  selected: number | null;
  onSelect: (index: number | null) => void;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const dragging = useRef<number | null>(null);

  const toLocal = useCallback(
    (event: React.MouseEvent | MouseEvent): [number, number] => {
      const svg = ref.current;
      if (!svg) return [0, 0];
      const rect = svg.getBoundingClientRect();
      const scale = CANVAS / rect.width;
      let x = (event.clientX - rect.left) * scale - HALF;
      let y = (event.clientY - rect.top) * scale - HALF;
      if (snap) {
        x = Math.round(x / gridSize) * gridSize;
        y = Math.round(y / gridSize) * gridSize;
      }
      return [Number(x.toFixed(2)), Number(y.toFixed(2))];
    },
    [snap, gridSize],
  );

  // Dragging is bound to the window, not the handle: a fast drag that leaves
  // the small circle must keep moving the point rather than silently stopping.
  useEffect(() => {
    function move(event: MouseEvent) {
      if (dragging.current === null) return;
      const next = [...points];
      next[dragging.current] = toLocal(event);
      onChange(next);
    }
    function up() {
      dragging.current = null;
    }
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    return () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
  }, [points, onChange, toLocal]);

  const path = points.length
    ? `M ${points.map(([x, y]) => `${x} ${y}`).join(' L ')} Z`
    : '';

  return (
    <svg
      ref={ref}
      viewBox={`${-HALF} ${-HALF} ${CANVAS} ${CANVAS}`}
      className="h-full w-full touch-none"
      onClick={(event) => {
        if (event.target === ref.current) onChange([...points, toLocal(event)]);
      }}
      role="application"
      aria-label="Shape builder canvas"
    >
      <defs>
        <pattern id="mk-grid" width={gridSize} height={gridSize} patternUnits="userSpaceOnUse">
          <path
            d={`M ${gridSize} 0 L 0 0 0 ${gridSize}`}
            fill="none"
            stroke="currentColor"
            strokeWidth="0.5"
            className="text-slate-300 dark:text-slate-700"
          />
        </pattern>
      </defs>
      <rect x={-HALF} y={-HALF} width={CANVAS} height={CANVAS} fill="url(#mk-grid)" />
      <line x1={-HALF} y1={0} x2={HALF} y2={0} className="stroke-slate-400/50" strokeWidth="0.75" />
      <line x1={0} y1={-HALF} x2={0} y2={HALF} className="stroke-slate-400/50" strokeWidth="0.75" />

      {path && (
        <path d={path} fill="#2563EB33" stroke="#2563EB" strokeWidth="1.5" strokeDasharray="4 3" />
      )}

      {points.map(([x, y], index) => (
        <g key={`${index}-${x}-${y}`}>
          <circle
            cx={x}
            cy={y}
            r={selected === index ? 6 : 4.5}
            className={
              selected === index
                ? 'cursor-move fill-brand-600 stroke-white'
                : 'cursor-move fill-white stroke-brand-600'
            }
            strokeWidth="2"
            onMouseDown={(event) => {
              event.stopPropagation();
              dragging.current = index;
              onSelect(index);
            }}
          />
          <text
            x={x + 8}
            y={y - 6}
            className="pointer-events-none fill-slate-500 text-[8px]"
          >
            {index + 1}
          </text>
        </g>
      ))}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function MarkerDesignerPage() {
  const t = useT();
  const { id } = useParams<{ id: string }>();
  const designId = id ? Number(id) : null;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const options = useQuery({ queryKey: ['marker-options'], queryFn: markerService.options });
  const existing = useQuery({
    queryKey: ['marker-design', designId],
    queryFn: () => markerService.get(designId as number),
    enabled: designId !== null,
  });

  const [name, setName] = useState('');
  const [entityType, setEntityType] = useState('territory');
  const [designType, setDesignType] = useState<MarkerDesignType>('BUILTIN_SHAPE');
  const [assetId, setAssetId] = useState<number | null>(null);
  const [definition, setDefinition] = useState<MarkerDefinition>(clone(EMPTY));
  const [history, setHistory] = useState<MarkerDefinition[]>([]);
  const [future, setFuture] = useState<MarkerDefinition[]>([]);
  const [selectedPoint, setSelectedPoint] = useState<number | null>(null);
  const [snap, setSnap] = useState(true);
  const [gridSize, setGridSize] = useState(10);
  const [showMap, setShowMap] = useState(false);
  const [tab, setTab] = useState<'shape' | 'icon' | 'label' | 'badge'>('shape');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploadNote, setUploadNote] = useState<string | null>(null);

  // Load an existing design into the editor.
  useEffect(() => {
    if (!existing.data) return;
    setName(existing.data.name);
    setEntityType(existing.data.entity_type);
    setDesignType(existing.data.design_type);
    setAssetId(existing.data.asset_id);
    setDefinition(clone(existing.data.definition));
    setHistory([]);
    setFuture([]);
  }, [existing.data]);

  /** Every mutation goes through here so undo/redo is never bypassed. */
  const apply = useCallback(
    (mutate: (draft: MarkerDefinition) => void) => {
      setDefinition((current) => {
        const next = clone(current);
        mutate(next);
        setHistory((stack) => [...stack.slice(-49), current]);
        setFuture([]);
        return next;
      });
      setMessage(null);
    },
    [],
  );

  function undo() {
    setHistory((stack) => {
      if (!stack.length) return stack;
      const previous = stack[stack.length - 1];
      setFuture((forward) => [definition, ...forward]);
      setDefinition(previous);
      return stack.slice(0, -1);
    });
  }

  function redo() {
    setFuture((stack) => {
      if (!stack.length) return stack;
      const [next, ...rest] = stack;
      setHistory((back) => [...back, definition]);
      setDefinition(next);
      return rest;
    });
  }

  function reset() {
    setHistory((stack) => [...stack, definition]);
    setFuture([]);
    setDefinition(clone(EMPTY));
    setSelectedPoint(null);
  }

  // The preview is the server's, debounced so a slider drag is one request per
  // pause rather than one per pixel.
  const debounced = useDebounced(definition, 250);
  const previewQuery = useQuery({
    queryKey: ['marker-preview', debounced, assetId],
    queryFn: () =>
      markerService.preview({
        definition: debounced as unknown as Record<string, unknown>,
        asset_id: assetId,
        context: { Code: 'T001', Name: 'Sample', Type: 'Territory', Level: 'Territory' },
      }),
    retry: false,
  });
  const preview = previewQuery.data as MarkerPreviewData | undefined;

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: name.trim(),
        entity_type: entityType,
        design_type: designType,
        definition: definition as unknown as Record<string, unknown>,
        asset_id: assetId,
      };
      return designId === null
        ? markerService.create(body)
        : markerService.update(designId, body);
    },
    onSuccess: (saved) => {
      setError(null);
      setMessage(t('marker.saved'));
      void queryClient.invalidateQueries({ queryKey: ['marker-designs'] });
      void queryClient.invalidateQueries({ queryKey: ['marker-legend'] });
      if (designId === null) {
        navigate(`/admin/map-settings/marker-designer/${saved.design_id}`, {
          replace: true,
        });
      }
    },
    onError: (caught) => {
      setMessage(null);
      setError((caught as Error).message);
    },
  });

  const upload = useMutation({
    mutationFn: (file: File) => markerService.uploadAsset(file),
    onSuccess: (asset) => {
      setAssetId(asset.asset_id);
      setDesignType('CUSTOM_IMAGE');
      apply((draft) => {
        draft.shape.kind = 'image';
      });
      setError(null);
      setUploadNote(
        asset.sanitised?.changed
          ? t('marker.uploadSanitised', {
              items: [
                ...(asset.sanitised.removed_elements ?? []),
                ...(asset.sanitised.removed_attributes ?? []),
              ]
                .slice(0, 6)
                .join(', '),
            })
          : t('marker.uploadClean'),
      );
    },
    onError: (caught) => {
      setUploadNote(null);
      setError((caught as Error).message);
    },
  });

  const iconCategories = options.data?.icons.categories ?? [];
  const shape = definition.shape;
  const isBuilder = designType === 'SHAPE_BUILDER';

  // Switching design type rewrites the geometry kind, so the two never disagree.
  function changeDesignType(next: MarkerDesignType) {
    setDesignType(next);
    apply((draft) => {
      if (next === 'BUILTIN_SHAPE') {
        draft.shape.kind = 'builtin';
        draft.shape.builtin = draft.shape.builtin ?? 'circle';
      } else if (next === 'SHAPE_BUILDER') {
        draft.shape.kind = 'polygon';
        if (draft.shape.points.length < 3) draft.shape.points = polygonPoints(40, 3);
      } else {
        draft.shape.kind = 'image';
      }
    });
  }

  return (
    <>
      <PageHeader
        title={designId ? t('marker.editTitle') : t('marker.designerTitle')}
        description={
          existing.data
            ? `${t('marker.version')} ${existing.data.version} · ${existing.data.status}`
            : t('marker.designerSubtitle')
        }
        actions={
          <Link to="/admin/map-settings/markers" className="btn-ghost">
            <ArrowLeft size={14} />
            {t('marker.backToLibrary')}
          </Link>
        }
      />

      {message && (
        <p className="mb-3 rounded bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300">
          {message}
        </p>
      )}
      {error && (
        <p className="mb-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </p>
      )}

      <QueryState
        isLoading={options.isLoading || (designId !== null && existing.isLoading)}
        error={options.error ?? existing.error}
        onRetry={() => void options.refetch()}
        skeleton={<CardSkeleton rows={8} />}
      >
        <div className="grid gap-4 xl:grid-cols-[18rem_minmax(0,1fr)_20rem]">
          {/* ---------------- Left: identity ---------------- */}
          <div className="space-y-4">
            <Section title={t('marker.design')}>
              <div className="space-y-3">
                <Field label={t('marker.designName')}>
                  <input
                    className="input"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    placeholder={t('marker.namePlaceholder')}
                  />
                </Field>
                <Field label={t('marker.entity')}>
                  <select
                    className="input"
                    value={entityType}
                    onChange={(event) => setEntityType(event.target.value)}
                  >
                    {(options.data?.entity_types ?? []).map((entity) => (
                      <option key={entity.key} value={entity.key}>
                        {entity.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <fieldset>
                  <legend className="label">{t('marker.designType')}</legend>
                  {(options.data?.design_types ?? []).map((option) => (
                    <label
                      key={option.key}
                      className="flex cursor-pointer items-center gap-2 py-1 text-sm"
                    >
                      <input
                        type="radio"
                        name="design-type"
                        className="h-4 w-4 accent-brand-600"
                        checked={designType === option.key}
                        onChange={() => changeDesignType(option.key)}
                      />
                      <span>{option.label}</span>
                    </label>
                  ))}
                </fieldset>

                {designType === 'CUSTOM_IMAGE' && (
                  <div>
                    <label className="btn-secondary w-full cursor-pointer justify-center">
                      <Upload size={14} />
                      {t('marker.uploadImage')}
                      <input
                        type="file"
                        className="hidden"
                        accept=".svg,.png,.webp"
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          if (file) upload.mutate(file);
                        }}
                      />
                    </label>
                    <p className="mt-1 text-[11px] text-slate-400">
                      {t('marker.uploadHint')}
                    </p>
                    {uploadNote && (
                      <p className="mt-1 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
                        {uploadNote}
                      </p>
                    )}
                  </div>
                )}
              </div>
            </Section>

            {isBuilder && (
              <Section title={t('marker.shapeBuilder')}>
                <div className="space-y-2">
                  <div className="flex flex-wrap gap-1">
                    <button
                      type="button"
                      className="btn-secondary text-xs"
                      onClick={() => apply((d) => { d.shape.points = polygonPoints(40, 3); })}
                    >
                      {t('marker.createPolygon')}
                    </button>
                    <button
                      type="button"
                      className="btn-secondary text-xs"
                      onClick={() => apply((d) => { d.shape.points = starPoints(44); })}
                    >
                      {t('marker.createStar')}
                    </button>
                    <button
                      type="button"
                      className="btn-secondary text-xs"
                      onClick={() => apply((d) => { d.shape.points = []; })}
                    >
                      {t('marker.clearPoints')}
                    </button>
                  </div>
                  <Toggle label={t('marker.snapToGrid')} checked={snap} onChange={setSnap} />
                  <Slider
                    label={t('marker.gridSize')}
                    value={gridSize}
                    min={4}
                    max={40}
                    onChange={setGridSize}
                  />
                  <p className="text-[11px] text-slate-500">{t('marker.builderHint')}</p>

                  {selectedPoint !== null && shape.points[selectedPoint] && (
                    <div className="rounded border border-slate-200 p-2 dark:border-slate-700">
                      <p className="mb-1 text-xs font-medium">
                        {t('marker.point')} {selectedPoint + 1}
                      </p>
                      <div className="flex gap-2">
                        {(['x', 'y'] as const).map((axis, axisIndex) => (
                          <input
                            key={axis}
                            className="input text-xs"
                            type="number"
                            value={shape.points[selectedPoint][axisIndex]}
                            onChange={(event) =>
                              apply((draft) => {
                                draft.shape.points[selectedPoint][axisIndex] = Number(
                                  event.target.value,
                                );
                              })
                            }
                            aria-label={axis.toUpperCase()}
                          />
                        ))}
                        <button
                          type="button"
                          className="btn-ghost px-2 text-red-600"
                          title={t('marker.deletePoint')}
                          onClick={() => {
                            apply((draft) => {
                              draft.shape.points.splice(selectedPoint, 1);
                            });
                            setSelectedPoint(null);
                          }}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </Section>
            )}
          </div>

          {/* ---------------- Middle: canvas ---------------- */}
          <div className="space-y-4">
            <Section
              title={t('marker.canvas')}
              actions={
                <div className="flex gap-1">
                  <button
                    type="button"
                    className="btn-ghost px-2"
                    onClick={undo}
                    disabled={!history.length}
                    title={t('marker.undo')}
                  >
                    <Undo2 size={14} />
                  </button>
                  <button
                    type="button"
                    className="btn-ghost px-2"
                    onClick={redo}
                    disabled={!future.length}
                    title={t('marker.redo')}
                  >
                    <Redo2 size={14} />
                  </button>
                  <button
                    type="button"
                    className="btn-ghost px-2"
                    onClick={reset}
                    title={t('marker.reset')}
                  >
                    <RotateCcw size={14} />
                  </button>
                </div>
              }
            >
              <div className="relative mx-auto aspect-square w-full max-w-md overflow-hidden rounded-lg border border-slate-200 bg-[repeating-conic-gradient(#f1f5f9_0%_25%,#ffffff_0%_50%)] bg-[length:16px_16px] dark:border-slate-700 dark:bg-[repeating-conic-gradient(#1e293b_0%_25%,#0f172a_0%_50%)]">
                {isBuilder ? (
                  <ShapeBuilderCanvas
                    points={shape.points}
                    snap={snap}
                    gridSize={gridSize}
                    onChange={(points) => apply((draft) => { draft.shape.points = points; })}
                    selected={selectedPoint}
                    onSelect={setSelectedPoint}
                  />
                ) : (
                  <div className="flex h-full w-full items-center justify-center p-6">
                    {previewQuery.isError ? (
                      <p className="max-w-xs text-center text-xs text-red-600">
                        {(previewQuery.error as Error).message}
                      </p>
                    ) : (
                      <MarkerPreview svg={preview?.svg} size={180} title={name} />
                    )}
                  </div>
                )}
              </div>

              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
                <span className="inline-flex items-center gap-1">
                  <Grid3x3 size={12} />
                  {preview
                    ? `${Math.round(preview.width)} × ${Math.round(preview.height)} px · ${
                        preview.vector_only ? t('marker.vectorSymbol') : t('marker.imageMarker')
                      }`
                    : t('common.loading')}
                </span>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => setShowMap((value) => !value)}
                >
                  <MapIcon size={14} />
                  {t('marker.previewOnMap')}
                </button>
              </div>
            </Section>

            {/* Live preview variants (marker only / with label / tooltip) */}
            <Section title={t('marker.livePreview')}>
              <div className="flex flex-wrap items-end gap-6">
                {[
                  [t('marker.markerOnly'), false],
                  [t('marker.withTooltip'), true],
                ].map(([caption, tooltip]) => (
                  <div key={String(caption)} className="text-center">
                    <div className="relative inline-block">
                      <MarkerPreview svg={preview?.svg} size={72} />
                      {tooltip && (
                        <span className="absolute -top-2 left-1/2 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded bg-slate-900 px-2 py-1 text-[10px] text-white shadow">
                          {name || t('marker.sampleTooltip')}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-[11px] text-slate-500">{caption}</p>
                  </div>
                ))}
              </div>
            </Section>

            {showMap && <SampleMapPreview svg={preview?.svg} name={name} />}
          </div>

          {/* ---------------- Right: properties ---------------- */}
          <div className="space-y-4">
            <Section title={t('marker.properties')}>
              <div className="mb-3 flex flex-wrap gap-1">
                {(
                  [
                    ['shape', t('marker.shape')],
                    ['icon', t('marker.icon')],
                    ['label', t('marker.label')],
                    ['badge', t('marker.badge')],
                  ] as const
                ).map(([key, caption]) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setTab(key)}
                    className={`rounded-lg px-2.5 py-1 text-xs font-medium ${
                      tab === key
                        ? 'bg-brand-600 text-white'
                        : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
                    }`}
                  >
                    {caption}
                  </button>
                ))}
              </div>

              {tab === 'shape' && (
                <div className="space-y-3">
                  {designType === 'BUILTIN_SHAPE' && (
                    <Field label={t('marker.shape')}>
                      <div className="grid grid-cols-6 gap-1">
                        {(options.data?.shapes ?? []).map((option) => (
                          <button
                            key={option.key}
                            type="button"
                            title={option.label}
                            onClick={() =>
                              apply((draft) => {
                                draft.shape.builtin = option.key;
                                draft.shape.kind = 'builtin';
                              })
                            }
                            className={`flex aspect-square items-center justify-center rounded border p-1 ${
                              shape.builtin === option.key
                                ? 'border-brand-600 bg-brand-50 dark:bg-slate-800'
                                : 'border-slate-200 hover:border-brand-400 dark:border-slate-700'
                            }`}
                          >
                            <svg viewBox="-24 -24 48 48" className="h-full w-full">
                              <path
                                d={option.sample_path}
                                className="fill-slate-500 dark:fill-slate-300"
                              />
                            </svg>
                          </button>
                        ))}
                      </div>
                    </Field>
                  )}
                  <ColourInput
                    label={t('marker.fill')}
                    value={shape.fill}
                    onChange={(value) => apply((d) => { d.shape.fill = value; })}
                  />
                  <ColourInput
                    label={t('marker.border')}
                    value={shape.stroke}
                    onChange={(value) => apply((d) => { d.shape.stroke = value; })}
                  />
                  <Slider
                    label={t('marker.borderWidth')}
                    value={shape.stroke_width}
                    min={0}
                    max={16}
                    step={0.5}
                    onChange={(value) => apply((d) => { d.shape.stroke_width = value; })}
                  />
                  <Slider
                    label={t('marker.size')}
                    value={shape.size}
                    min={8}
                    max={128}
                    onChange={(value) => apply((d) => { d.shape.size = value; })}
                    suffix="px"
                  />
                  <Slider
                    label={t('marker.opacity')}
                    value={shape.opacity}
                    min={0}
                    max={1}
                    step={0.05}
                    onChange={(value) => apply((d) => { d.shape.opacity = value; })}
                  />
                  <Slider
                    label={t('marker.rotation')}
                    value={shape.rotation}
                    min={-180}
                    max={180}
                    onChange={(value) => apply((d) => { d.shape.rotation = value; })}
                    suffix="°"
                  />
                  <Toggle
                    label={t('marker.shadow')}
                    checked={shape.shadow.enabled}
                    onChange={(value) => apply((d) => { d.shape.shadow.enabled = value; })}
                  />
                  <Toggle
                    label={t('marker.background')}
                    checked={definition.background.enabled}
                    onChange={(value) => apply((d) => { d.background.enabled = value; })}
                  />
                </div>
              )}

              {tab === 'icon' && (
                <div className="space-y-3">
                  <Toggle
                    label={t('marker.showIcon')}
                    checked={definition.icon.enabled}
                    onChange={(value) => apply((d) => { d.icon.enabled = value; })}
                  />
                  {definition.icon.enabled && (
                    <>
                      <div className="max-h-64 space-y-2 overflow-y-auto rounded border border-slate-200 p-2 dark:border-slate-700">
                        {iconCategories.map((category) => (
                          <div key={category.key}>
                            <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                              {category.key}
                            </p>
                            <div className="grid grid-cols-6 gap-1">
                              {category.icons.map((icon) => (
                                <button
                                  key={icon.key}
                                  type="button"
                                  title={icon.label}
                                  onClick={() =>
                                    apply((d) => {
                                      d.icon.name = icon.key;
                                      d.icon.source = 'library';
                                    })
                                  }
                                  className={`flex aspect-square items-center justify-center rounded border p-1 ${
                                    definition.icon.name === icon.key
                                      ? 'border-brand-600 bg-brand-50 dark:bg-slate-800'
                                      : 'border-slate-200 hover:border-brand-400 dark:border-slate-700'
                                  }`}
                                >
                                  <svg viewBox="0 0 24 24" className="h-full w-full">
                                    <path
                                      d={icon.path}
                                      className={
                                        icon.stroked
                                          ? 'fill-none stroke-slate-600 dark:stroke-slate-300'
                                          : 'fill-slate-600 dark:fill-slate-300'
                                      }
                                      strokeWidth={icon.stroked ? 2 : undefined}
                                    />
                                  </svg>
                                </button>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                      <ColourInput
                        label={t('marker.iconColour')}
                        value={definition.icon.colour}
                        onChange={(value) => apply((d) => { d.icon.colour = value; })}
                      />
                      <Slider
                        label={t('marker.iconSize')}
                        value={definition.icon.size}
                        min={4}
                        max={96}
                        onChange={(value) => apply((d) => { d.icon.size = value; })}
                      />
                      <div className="grid grid-cols-2 gap-2">
                        <Slider
                          label="X"
                          value={definition.icon.offset_x}
                          min={-32}
                          max={32}
                          onChange={(value) => apply((d) => { d.icon.offset_x = value; })}
                        />
                        <Slider
                          label="Y"
                          value={definition.icon.offset_y}
                          min={-32}
                          max={32}
                          onChange={(value) => apply((d) => { d.icon.offset_y = value; })}
                        />
                      </div>
                    </>
                  )}
                </div>
              )}

              {tab === 'label' && (
                <div className="space-y-3">
                  <Toggle
                    label={t('marker.showLabel')}
                    checked={definition.label.enabled}
                    onChange={(value) => apply((d) => { d.label.enabled = value; })}
                  />
                  {definition.label.enabled && (
                    <>
                      <Field label={t('marker.labelText')}>
                        <input
                          className="input"
                          value={definition.label.template}
                          onChange={(event) =>
                            apply((d) => { d.label.template = event.target.value; })
                          }
                        />
                      </Field>
                      <div className="flex flex-wrap gap-1">
                        {(options.data?.label_variables ?? []).map((variable) => (
                          <button
                            key={variable}
                            type="button"
                            className="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-mono hover:bg-slate-200 dark:bg-slate-800"
                            onClick={() =>
                              apply((d) => { d.label.template = `${d.label.template}${variable}`; })
                            }
                          >
                            {variable}
                          </button>
                        ))}
                      </div>
                      <Field label={t('marker.position')}>
                        <select
                          className="input"
                          value={definition.label.position}
                          onChange={(event) =>
                            apply((d) => {
                              d.label.position = event.target
                                .value as MarkerDefinition['label']['position'];
                            })
                          }
                        >
                          {['inside', 'above', 'below', 'left', 'right'].map((value) => (
                            <option key={value} value={value}>
                              {value}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label={t('marker.alignment')}>
                        <select
                          className="input"
                          value={definition.label.align}
                          onChange={(event) =>
                            apply((d) => {
                              d.label.align = event.target
                                .value as MarkerDefinition['label']['align'];
                            })
                          }
                        >
                          {['left', 'center', 'right'].map((value) => (
                            <option key={value} value={value}>
                              {value}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label={t('marker.fontWeight')}>
                        <select
                          className="input"
                          value={definition.label.font_weight}
                          onChange={(event) =>
                            apply((d) => {
                              d.label.font_weight = event.target
                                .value as MarkerDefinition['label']['font_weight'];
                            })
                          }
                        >
                          {['normal', 'medium', 'semibold', 'bold'].map((value) => (
                            <option key={value} value={value}>
                              {value}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Slider
                        label={t('marker.fontSize')}
                        value={definition.label.font_size}
                        min={6}
                        max={48}
                        onChange={(value) => apply((d) => { d.label.font_size = value; })}
                      />
                      <ColourInput
                        label={t('marker.textColour')}
                        value={definition.label.colour}
                        onChange={(value) => apply((d) => { d.label.colour = value; })}
                      />
                      <Toggle
                        label={t('marker.labelHalo')}
                        checked={definition.label.halo}
                        onChange={(value) => apply((d) => { d.label.halo = value; })}
                      />
                    </>
                  )}
                </div>
              )}

              {tab === 'badge' && (
                <div className="space-y-3">
                  <Toggle
                    label={t('marker.showBadge')}
                    checked={definition.badge.enabled}
                    onChange={(value) => apply((d) => { d.badge.enabled = value; })}
                  />
                  {definition.badge.enabled && (
                    <>
                      <Field label={t('marker.badgeType')}>
                        <select
                          className="input"
                          value={definition.badge.kind}
                          onChange={(event) =>
                            apply((d) => {
                              d.badge.kind = event.target
                                .value as MarkerDefinition['badge']['kind'];
                            })
                          }
                        >
                          {[
                            'active',
                            'inactive',
                            'alert',
                            'outstanding',
                            'low_stock',
                            'performance',
                            'custom',
                          ].map((value) => (
                            <option key={value} value={value}>
                              {value.replace('_', ' ')}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label={t('marker.badgePosition')}>
                        <select
                          className="input"
                          value={definition.badge.position}
                          onChange={(event) =>
                            apply((d) => {
                              d.badge.position = event.target
                                .value as MarkerDefinition['badge']['position'];
                            })
                          }
                        >
                          {['top-left', 'top-right', 'bottom-left', 'bottom-right'].map(
                            (value) => (
                              <option key={value} value={value}>
                                {value}
                              </option>
                            ),
                          )}
                        </select>
                      </Field>
                      <ColourInput
                        label={t('marker.badgeColour')}
                        value={definition.badge.fill}
                        onChange={(value) => apply((d) => { d.badge.fill = value; })}
                      />
                      <Slider
                        label={t('marker.badgeSize')}
                        value={definition.badge.size}
                        min={4}
                        max={32}
                        onChange={(value) => apply((d) => { d.badge.size = value; })}
                      />
                      <Field label={t('marker.badgeText')}>
                        <input
                          className="input"
                          maxLength={4}
                          value={definition.badge.text ?? ''}
                          onChange={(event) =>
                            apply((d) => { d.badge.text = event.target.value || null; })
                          }
                        />
                      </Field>
                    </>
                  )}
                </div>
              )}
            </Section>

            <button
              type="button"
              className="btn-primary w-full justify-center"
              disabled={save.isPending || !name.trim()}
              onClick={() => save.mutate()}
            >
              <Save size={14} />
              {save.isPending ? t('common.saving') : t('marker.saveDesign')}
            </button>
            {!name.trim() && (
              <p className="text-center text-[11px] text-slate-400">
                {t('marker.nameRequired')}
              </p>
            )}
          </div>
        </div>
      </QueryState>
    </>
  );
}

/**
 * A sample map preview.
 *
 * Deliberately a schematic surface rather than a second MapLibre instance: the
 * point of the preview is to judge the marker at map scale, over map-like tones,
 * with zoom and pan, and a real map here would download tiles and a WebGL
 * context to answer a question about one piece of artwork. The marker itself is
 * the real server-rendered SVG — the same bytes `/map` registers with
 * `addImage` — so what is shown is what the map draws.
 */
function SampleMapPreview({ svg, name }: { svg: string | undefined; name: string }) {
  const t = useT();
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [selected, setSelected] = useState<number | null>(null);
  const dragging = useRef<{ x: number; y: number } | null>(null);

  const spots = useMemo(
    () => [
      { id: 1, x: 30, y: 38, label: 'T001' },
      { id: 2, x: 58, y: 26, label: 'T002' },
      { id: 3, x: 47, y: 62, label: 'T003' },
      { id: 4, x: 72, y: 55, label: 'T004' },
    ],
    [],
  );

  return (
    <Section title={t('marker.mapPreview')}>
      <div
        className="relative h-72 w-full cursor-grab overflow-hidden rounded-lg border border-slate-200 bg-[#e8eef2] active:cursor-grabbing dark:border-slate-700 dark:bg-[#16212e]"
        onMouseDown={(event) => {
          dragging.current = { x: event.clientX - pan.x, y: event.clientY - pan.y };
        }}
        onMouseMove={(event) => {
          if (!dragging.current) return;
          setPan({ x: event.clientX - dragging.current.x, y: event.clientY - dragging.current.y });
        }}
        onMouseUp={() => {
          dragging.current = null;
        }}
        onMouseLeave={() => {
          dragging.current = null;
        }}
      >
        {/* Schematic basemap: roads and blocks, enough to judge contrast. */}
        <svg
          className="absolute inset-0 h-full w-full"
          style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}
          viewBox="0 0 100 70"
          preserveAspectRatio="xMidYMid slice"
        >
          <rect width="100" height="70" className="fill-[#dfe7ec] dark:fill-[#1b2836]" />
          {[12, 30, 48, 66].map((y) => (
            <rect key={y} x="0" y={y} width="100" height="2.4" className="fill-white/70 dark:fill-white/10" />
          ))}
          {[18, 42, 68, 88].map((x) => (
            <rect key={x} x={x} y="0" width="2.4" height="70" className="fill-white/70 dark:fill-white/10" />
          ))}
          <path d="M0 58 Q 30 46 55 56 T 100 50 L100 70 L0 70 Z"
                className="fill-[#bcd6e8] dark:fill-[#12303f]" />
          <circle cx="80" cy="20" r="9" className="fill-[#cfe3cf] dark:fill-[#1c3326]" />
        </svg>

        {spots.map((spot) => (
          <button
            key={spot.id}
            type="button"
            className="absolute -translate-x-1/2 -translate-y-full"
            style={{
              left: `calc(${spot.x}% + ${pan.x}px)`,
              top: `calc(${spot.y}% + ${pan.y}px)`,
              transform: `translate(-50%, -100%) scale(${zoom})`,
            }}
            onClick={(event) => {
              event.stopPropagation();
              setSelected(selected === spot.id ? null : spot.id);
            }}
          >
            <MarkerPreview svg={svg} size={44} title={spot.label} />
            {selected === spot.id && (
              <span className="absolute left-1/2 top-0 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded bg-slate-900 px-2 py-1 text-[10px] text-white shadow">
                {name || spot.label}
              </span>
            )}
          </button>
        ))}

        <div className="absolute bottom-2 right-2 flex flex-col overflow-hidden rounded border border-slate-300 bg-white text-sm shadow dark:border-slate-600 dark:bg-slate-800">
          <button
            type="button"
            className="px-2 py-1 hover:bg-slate-100 dark:hover:bg-slate-700"
            onClick={() => setZoom((value) => Math.min(2.5, value + 0.25))}
            aria-label={t('marker.zoomIn')}
          >
            +
          </button>
          <button
            type="button"
            className="border-t border-slate-200 px-2 py-1 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-700"
            onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))}
            aria-label={t('marker.zoomOut')}
          >
            −
          </button>
        </div>
      </div>
      <p className="mt-2 text-[11px] text-slate-500">{t('marker.mapPreviewNote')}</p>
    </Section>
  );
}
