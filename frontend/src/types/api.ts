/**
 * Types mirroring the backend contracts.
 *
 * Kept deliberately close to what the API returns so the compiler catches a
 * drift between frontend and backend rather than a user discovering it.
 */

export type Role =
  | 'SUPER_ADMIN'
  | 'ADMIN'
  | 'MANAGEMENT'
  | 'BUSINESS_UNIT_HEAD'
  | 'ZONE_MANAGER'
  | 'REGIONAL_MANAGER'
  | 'AREA_MANAGER'
  | 'UNIT_MANAGER'
  | 'TERRITORY_MANAGER'
  | 'SALES_OFFICER'
  | 'VIEWER';

export type Language = 'en' | 'bn';
export type ThemePreference = 'light' | 'dark' | 'system';

export type UserStatus = 'ACTIVE' | 'INACTIVE' | 'LOCKED';

/** Stable identifiers for the application sections a user can be granted. */
export type SectionKey =
  | 'dashboard'
  | 'ai_assistant'
  | 'sales'
  | 'stock'
  | 'target'
  | 'performance'
  | 'materials'
  | 'customers'
  | 'alerts'
  | 'data_quality'
  | 'data_upload'
  | 'master_data'
  | 'transaction_data'
  | 'map'
  | 'map_settings'
  | 'agent_learning'
  | 'admin'
  | 'settings';

export type SectionAccess = 'ALLOW' | 'DENY';

/** What a user may do *inside* a section they hold. */
export type SectionAction = 'VIEW' | 'CREATE' | 'EDIT' | 'DELETE' | 'EXPORT' | 'UPLOAD';

export interface User {
  user_id: number;
  username: string;
  display_name: string;
  email: string | null;
  role: Role;
  employee_id: string | null;
  data_scope: Record<string, string[]>;
  preferred_language: Language;
  theme: ThemePreference;
  is_admin: boolean;
  is_active?: boolean;
  status?: UserStatus;
  department?: string | null;
  designation?: string | null;
  mobile?: string | null;
  last_login_at?: string | null;
  scope_description?: string;
  /** Which sections the backend will serve this user. Convenience, not control. */
  sections?: Partial<Record<SectionKey, boolean>>;
  /** And what they may do inside each. Same status: it decides what is drawn. */
  actions?: Partial<Record<SectionKey, Partial<Record<SectionAction, boolean>>>>;
  company_name?: string;
  currency?: string;
  financial_year_start_month?: number;
}

// ---------------------------------------------------------------------------
// Data Management
// ---------------------------------------------------------------------------

export type EntityCategory = 'MASTER' | 'TRANSACTION';

export interface ManagedField {
  name: string;
  label: string;
  kind: string;
  required: boolean;
  editable: boolean;
  description: string;
  is_key: boolean;
  /** The master table this field must name a record in, when it has one. */
  references: string | null;
  references_column: string | null;
  default_visible: boolean;
  choices: string[];
}

export interface ManagedEntity {
  key: string;
  label: string;
  category: EntityCategory;
  description: string;
  section: SectionKey;
  key_fields: string[];
  label_field: string | null;
  fields: ManagedField[];
  default_columns: string[];
  status_field: string | null;
  status_values: string[];
  soft_delete: boolean;
  voidable: boolean;
  supports_geo: boolean;
  scope_level: string | null;
  parent_table: string | null;
  parent_column: string | null;
  filter_fields: string[];
  search_fields: string[];
  data_type: string | null;
  table: string | null;
  permissions?: Partial<Record<SectionAction, boolean>>;
  report_section?: SectionKey;
}

export interface EntityGroup {
  key: EntityCategory;
  label: string;
  route: string;
  description: string;
  entities: ManagedEntity[];
  permissions: Partial<Record<SectionAction, boolean>>;
  accessible: boolean;
}

export interface DataCatalogue {
  groups: EntityGroup[];
}

export type ManagedRow = Record<string, unknown> & { _key: string };

export interface RecordListResponse {
  entity: ManagedEntity;
  permissions: Partial<Record<SectionAction, boolean>>;
  rows: ManagedRow[];
  columns: string[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  range_from: number;
  range_to: number;
  scope_description: string;
}

export interface RecordChange {
  field: string;
  old: unknown;
  new: unknown;
}

export interface HistoryEntry {
  change_id: number;
  action: 'CREATED' | 'UPDATED' | 'DELETED' | 'RESTORED' | 'VOIDED' | 'UNVOIDED';
  username: string | null;
  role: string | null;
  reason: string | null;
  created_at: string;
  changed_fields: string[];
  changes: RecordChange[];
}

export interface Dependants {
  counts: Record<string, number>;
  total: number;
  blocking: boolean;
  description: string;
}

export interface RecordDetailResponse {
  entity: ManagedEntity;
  permissions: Partial<Record<SectionAction, boolean>>;
  record: ManagedRow;
  hierarchy?: Record<string, string> | null;
  location?: {
    entity_type: string;
    latitude: number | null;
    longitude: number | null;
    source?: string;
  } | null;
  dependants: Dependants;
  history: HistoryEntry[];
  history_total: number;
  last_change: { action: string; username: string | null; at: string } | null;
}

export interface WriteResponse {
  record: ManagedRow;
  action: string;
  changed_fields: string[];
  message: string;
}

export interface BulkResponse {
  succeeded: string[];
  failed: { code: string; reason: string }[];
  success_count: number;
  failure_count: number;
}

/** One field-level problem, in the shape the upload preview also renders. */
export interface FieldIssue {
  row: number | null;
  column: string | null;
  value: string | null;
  error_code: string;
  error: string;
  suggested_fix: string | null;
  severity: string;
  category: string;
}

export interface Section {
  key: SectionKey;
  label: string;
  route: string;
  group: string;
  description: string;
  actions: string[];
  locked_to_roles: Role[];
  api_prefixes: string[];
}

/** A section row on the permission screen: effective answer plus its reason. */
export interface SectionPermission extends Section {
  access: SectionAccess;
  allowed: boolean;
  source: 'ROLE_LOCK' | 'ROLE_DEFAULT' | 'ROLE_PERMISSION' | 'USER_PERMISSION';
  role_access: SectionAccess;
  user_access: SectionAccess | null;
  locked: boolean;
}

export interface RoleSummary {
  role: Role;
  unrestricted: boolean;
  is_admin: boolean;
  /** Absent when talking to a backend older than the section-permission work. */
  user_count?: number;
  sections?: Record<string, SectionAccess>;
}

export interface AdminSummary {
  total_users: number;
  active_users: number;
  inactive_users: number;
  locked_users: number;
  roles: number;
  roles_in_use: number;
  sections: number;
  pending_imports: number;
  failed_imports: number;
  uploads_today: number;
}

// --- Data Upload Center ----------------------------------------------------

export type UploadCategory = 'MASTER' | 'TRANSACTIONAL';
export type ImportMode = 'INSERT' | 'UPDATE' | 'UPSERT';
/**
 * `QUEUED` and `CANCELLED` arrived with background import jobs: a batch can now
 * be waiting for a worker before it starts, and a user can stop one that is
 * running. Both are returned by the API and so must be spelled here.
 */
export type UploadStatusValue =
  | 'UPLOADED'
  | 'QUEUED'
  | 'VALIDATING'
  | 'VALIDATED'
  | 'IMPORTING'
  | 'COMPLETED'
  | 'PARTIAL'
  | 'FAILED'
  | 'CANCELLED'
  | 'ROLLED_BACK';

export interface UploadColumn {
  name: string;
  target: string;
  kind: string;
  required: boolean;
  description: string;
  example: string;
  aliases: string[];
  guidance: string;
}

export interface UploadType {
  key: string;
  label: string;
  category: UploadCategory;
  description: string;
  table: string | null;
  data_type: string | null;
  parent_table: string | null;
  business_key: string[];
  business_key_description: string;
  supported_modes: ImportMode[];
  default_mode: ImportMode;
  required_columns: string[];
  column_count: number;
  pending_source: boolean;
  note: string;
  columns: UploadColumn[];
}

export interface UploadCatalogue {
  categories: {
    key: UploadCategory;
    label: string;
    description: string;
    types: UploadType[];
  }[];
  import_modes: { key: ImportMode; label: string; description: string }[];
  limits: { max_bytes: number; extensions: string[]; preview_rows: number };
}

export interface UploadBatch {
  upload_id: number;
  upload_uuid: string;
  /**
   * The Import Job ID. It *is* `upload_uuid` under the name the job endpoints
   * use — there is no second identifier — and it is what `GET /jobs/{job_id}`
   * and the cancel endpoint are keyed on.
   */
  job_id: string;
  category: UploadCategory;
  upload_type: string;
  file_name: string;
  file_size: number;
  import_mode: ImportMode;
  status: UploadStatusValue;
  user_id: number | null;
  username: string | null;
  started_at: string;
  validated_at: string | null;
  completed_at: string | null;
  totals: {
    total_rows: number;
    columns: number;
    valid_rows: number;
    invalid_rows: number;
    duplicate_rows: number;
    inserted_rows: number;
    updated_rows: number;
  };
  etl_batch_id: number | null;
  summary: Record<string, unknown> | null;
  message: string | null;
  rolled_back_at: string | null;
  rolled_back_by: string | null;
  can_commit: boolean;
  can_rollback: boolean;
}

export interface UploadIssue {
  row: number | null;
  column: string | null;
  value: string | null;
  error_code: string;
  error: string;
  suggested_fix: string | null;
  severity: string;
  category: string;
}

export interface UploadOutcome {
  upload: UploadBatch;
  warnings: string[];
  errors: UploadIssue[];
  error_count: number;
  error_truncated: boolean;
  preview?: {
    columns: string[];
    rows: Record<string, unknown>[];
    shown: number;
    limit: number;
  };
}

/**
 * What `POST /preview` and `POST /{id}/commit` answer: **202**, the batch as it
 * stands the instant it was queued, and nothing else.
 *
 * It is deliberately not an `UploadOutcome`. At this point no row has been read,
 * so there are no counts, no warnings, no errors and no preview — the totals on
 * `upload` are all zero and its status is `QUEUED`. The result is assembled by a
 * background worker and read afterwards from `GET /history/{upload_id}`.
 */
export interface UploadQueued {
  upload: UploadBatch;
  queued: boolean;
}

/**
 * The phases a file passes through on the server. `UPLOADING` is the browser's
 * own — only it can measure bytes leaving the machine — and never arrives from
 * the API.
 */
export type UploadPhase =
  | 'UPLOADING'
  | 'PREPARING'
  | 'READING'
  | 'VALIDATING'
  | 'MAPPING'
  | 'IMPORTING'
  | 'WRITING'
  | 'COMPLETED'
  | 'FAILED';

/**
 * A live progress reading for one upload.
 *
 * `known: false` means the server has no record of the job. That is not an
 * error: progress lives in the serving process's memory, so an evicted or
 * foreign-worker job simply has no detail to report while the upload request
 * itself carries on normally.
 */
export interface UploadJobProgress {
  known: boolean;
  job_id: string;
  operation?: 'VALIDATE' | 'IMPORT';
  upload_type?: string | null;
  file_name?: string | null;
  phase?: UploadPhase;
  percent?: number;
  /**
   * The batch's own status, and the only part of this payload that survives the
   * job being evicted from the server's in-memory registry. `done` and `phase`
   * come from that registry and stop being reported once it lets the job go, so
   * anything deciding whether a run has finished must read this instead.
   */
  status?: UploadStatusValue;
  done?: boolean;
  message?: string | null;
  upload_id?: number | null;
  counts?: {
    total_records: number;
    processed_records: number;
    valid_records: number;
    invalid_records: number;
    imported_records: number;
    failed_records: number;
  };
  started_at?: string;
  updated_at?: string;
}

/**
 * One entry of `GET /api/data-upload/jobs` — a batch row with live detail
 * layered on while a worker is still running it.
 *
 * This is what makes an upload survive navigation. The job runs on the server
 * and its state lives in `upload_batches`, so any page can ask for it at any
 * time; nothing about it depends on the Upload Center being mounted, and the
 * browser is a monitor rather than the owner.
 *
 * `CANCELLING` appears in `stage` but not in `UploadPhase`: it is derived by the
 * backend when a stop has been asked for and the run has not unwound yet, so the
 * UI can say "Cancelling…" instead of leaving the button live.
 */
export interface UploadJob extends UploadBatch {
  stage: string | null;
  progress_percent: number | null;
  duration_seconds: number | null;
  counts: {
    total_records: number;
    processed_records: number;
    valid_records: number;
    invalid_records: number;
    imported_records: number;
    failed_records: number;
  };
  /** True while the serving process still holds live detail for this job. */
  live: boolean;
  can_cancel: boolean;
  is_active: boolean;
}

export interface UploadJobsResponse {
  jobs: UploadJob[];
  /** How many of them are still running. Drives whether the dock polls at all. */
  active: number;
}

// --- Marker / Shape Designer -----------------------------------------------

export type MarkerDesignType = 'BUILTIN_SHAPE' | 'CUSTOM_IMAGE' | 'SHAPE_BUILDER';
export type MarkerDesignStatus = 'DRAFT' | 'ACTIVE' | 'INACTIVE';
export type ShapeKind = 'builtin' | 'polygon' | 'path' | 'image';

export interface MapEntityType {
  key: string;
  label: string;
  code_field: string;
  table: string;
  group: string;
  depth: number | null;
  has_source_data: boolean;
  promoted: boolean;
  description: string;
}

export interface BuiltinShapeOption {
  key: string;
  label: string;
  anchor: [number, number];
  description: string;
  sample: [number, number][];
  sample_path: string;
}

export interface IconOption {
  key: string;
  label: string;
  category: string;
  path: string;
  stroked: boolean;
  viewbox: number;
}

export interface ShapeConfig {
  kind: ShapeKind;
  builtin: string | null;
  points: [number, number][];
  path: string | null;
  closed: boolean;
  fill: string;
  stroke: string;
  stroke_width: number;
  opacity: number;
  rotation: number;
  size: number;
  shadow: {
    enabled: boolean;
    colour: string;
    blur: number;
    offset_x: number;
    offset_y: number;
  };
}

export interface MarkerDefinition {
  shape: ShapeConfig;
  icon: {
    enabled: boolean;
    source: 'library' | 'asset';
    name: string | null;
    asset_id: number | null;
    size: number;
    colour: string;
    offset_x: number;
    offset_y: number;
    opacity: number;
  };
  label: {
    enabled: boolean;
    template: string;
    font_size: number;
    font_weight: 'normal' | 'medium' | 'semibold' | 'bold';
    colour: string;
    align: 'left' | 'center' | 'right';
    position: 'inside' | 'above' | 'below' | 'left' | 'right';
    offset_x: number;
    offset_y: number;
    halo: boolean;
    halo_colour: string;
  };
  badge: {
    enabled: boolean;
    kind:
      | 'active'
      | 'inactive'
      | 'alert'
      | 'outstanding'
      | 'low_stock'
      | 'performance'
      | 'custom';
    position: 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right';
    fill: string;
    stroke: string;
    size: number;
    text: string | null;
  };
  background: {
    enabled: boolean;
    fill: string;
    stroke: string;
    stroke_width: number;
    corner_radius: number;
    padding: number;
    opacity: number;
  };
  notes: string | null;
}

export interface MarkerDesign {
  design_id: number;
  design_uuid: string;
  name: string;
  description: string | null;
  entity_type: string;
  design_type: MarkerDesignType;
  status: MarkerDesignStatus;
  definition: MarkerDefinition;
  asset_id: number | null;
  version: number;
  is_system_default: boolean;
  created_by: string | null;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
  in_use?: boolean;
  preview?: {
    svg: string;
    width: number;
    height: number;
    vector_only: boolean;
  };
  assignments?: {
    assignment_id: number;
    entity_type: string;
    entity_code: string | null;
    is_active: boolean;
    priority: number;
  }[];
}

export interface MarkerAsset {
  asset_id: number;
  asset_uuid: string;
  file_name: string;
  media_type: string;
  byte_size: number;
  width: number | null;
  height: number | null;
  content: string;
  sanitised: { changed: boolean; removed_elements: string[]; removed_attributes: string[] } | null;
  uploaded_by: string | null;
  created_at: string;
  reused?: boolean;
}

export interface DesignerOptions {
  entity_types: MapEntityType[];
  shapes: BuiltinShapeOption[];
  icons: { viewbox: number; categories: { key: string; icons: IconOption[] }[] };
  design_types: { key: MarkerDesignType; label: string }[];
  statuses: MarkerDesignStatus[];
  label_variables: string[];
  renderers: string[];
  upload: { max_bytes: number; extensions: string[] };
  counts: Record<string, number>;
}

export interface MarkerPreview {
  svg: string;
  width: number;
  height: number;
  anchor: { x: number; y: number };
  path_anchor: { x: number; y: number };
  vector_only: boolean;
  path: string | null;
  marker?: Record<string, unknown>;
}

export interface ResolvedMarkerConfig {
  entity_type: string;
  entity_code: string | null;
  design_id: number | null;
  design_name: string;
  source: 'entity' | 'entity_type' | 'system_default' | 'fallback';
  marker: Record<string, unknown>;
  preview_svg: string;
  anchor: { x: number; y: number };
  size: { width: number; height: number };
}

export interface MarkerLegendEntry extends ResolvedMarkerConfig {
  label: string;
  group: string;
  promoted: boolean;
}

export interface MarkerVersion {
  version_id: number;
  version: number;
  name: string;
  design_type: MarkerDesignType;
  status: MarkerDesignStatus;
  note: string | null;
  definition: MarkerDefinition;
  preview_svg: string;
  created_by: string | null;
  created_at: string;
}

export interface MarkerAssignmentRow {
  assignment_id: number;
  entity_type: string;
  entity_code: string | null;
  scope: 'entity' | 'entity_type';
  is_active: boolean;
  priority: number;
  design_id: number;
  design_name: string;
  design_status: MarkerDesignStatus;
}

// --- Business map -----------------------------------------------------------

export interface MapLevel {
  key: string;
  label: string;
  depth: number;
  child: string | null;
  parent: string | null;
}

export interface MapMetric {
  key: string;
  label: string;
  unit: 'currency' | 'quantity' | string;
  inverse: boolean;
  description: string;
}

export interface MapCoverageRow {
  entity_type: string;
  label: string;
  total: number;
  placed: number;
  derived: number;
  missing: number;
}

// ---------------------------------------------------------------------------
// Administrative area layer
// ---------------------------------------------------------------------------

/** The administrative levels the map can draw as polygons. */
export interface AdminLevel {
  key: string;
  label: string;
  code_field: string;
  name_field: string;
  parent: string | null;
  parent_field: string | null;
  depth: number;
  description: string;
}

/** How one administrative level is painted. Read from the database, never
 *  hardcoded — a colour in the frontend would be a second source of truth. */
export interface AreaStyle {
  entity_type: string;
  configured: boolean;
  fill_color: string;
  fill_opacity: number;
  stroke_color: string;
  stroke_opacity: number;
  stroke_width: number;
  hover_fill_color?: string;
  hover_fill_opacity?: number;
  selected_fill_color?: string;
  selected_fill_opacity?: number;
  z_index: number;
  /** Declared for future value-based colouring; nothing evaluates it. */
  rules?: Record<string, unknown> | null;
  is_system_default?: boolean;
}

export interface GeoJsonGeometry {
  type: 'Polygon' | 'MultiPolygon';
  coordinates: number[][][] | number[][][][];
}

export interface AreaProperties {
  country_id?: number;
  country_code?: string;
  country_name?: string;
  country_name_bn?: string | null;
  iso3?: string | null;
  upazila_id?: number;
  upazila_code?: string;
  upazila_name?: string;
  upazila_name_bn?: string | null;
  district_id?: number;
  district_code?: string;
  district_name?: string;
  division_id?: number;
  division_code?: string;
  division_name?: string;
  division_name_bn?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  centroid_latitude?: number;
  centroid_longitude?: number;
  point_count?: number;
  stock?: number;
  /** `measured` when the warehouse could account for it, `default` otherwise. */
  stock_source?: 'measured' | 'default';
  [key: string]: unknown;
}

export interface AreaFeature {
  type: 'Feature';
  id: string;
  properties: AreaProperties;
  geometry: GeoJsonGeometry | null;
  /** `[west, south, east, north]`, as GeoJSON orders a bbox. */
  bbox: [number, number, number, number];
}

export interface AreaCoverageRow {
  level: string;
  label: string;
  total: number;
  with_boundary: number;
  missing_boundary: number;
}

/*
 * `AreaLayer` — a level's features bundled with its style, for a renderer to
 * consume — is gone. Boundary geometry now reaches the map from
 * `public/geo/`, and each MapLibre layer reads its own source and its own paint
 * properties, so nothing needs the two carried together.
 */

/** A published administrative reference point: a capital or a label position. */
export interface AdminPointProperties {
  kind: 'capital' | 'point';
  /** 0 country, 1 division, 2 district, 3 upazila, 4 below it. */
  admin_level: number;
  name: string;
  name_bn?: string | null;
  latitude: number;
  longitude: number;
  country_code?: string | null;
  division_code?: string | null;
  district_code?: string | null;
  upazila_code?: string | null;
}

export interface AdminPointFeature {
  type: 'Feature';
  id: number;
  properties: AdminPointProperties;
  geometry: { type: 'Point'; coordinates: [number, number] };
}

export interface AdminPointKind {
  key: 'capital' | 'point';
  label: string;
  max_level: number;
}

export interface AdminPointResponse {
  type: 'FeatureCollection';
  kind: string;
  max_level: number;
  features: AdminPointFeature[];
  returned: number;
  truncated: boolean;
  kinds: AdminPointKind[];
}

export interface AreaResponse {
  type: 'FeatureCollection';
  level: string;
  layer_key: string;
  features: AreaFeature[];
  style: AreaStyle;
  total: number;
  returned: number;
  truncated: boolean;
  boundaries_loaded: boolean;
  unresolved_territories: string[];
  bounds: MapBounds | null;
  period: DateRange;
  metric: string;
  default_stock: number;
}

export interface AreaDetail {
  level: string;
  code: string;
  properties: AreaProperties;
  bbox: [number, number, number, number];
  style: AreaStyle;
}

export interface AdministrativeAreaConfig {
  layer_key: string;
  levels: AdminLevel[];
  default_level: string;
  styles: Record<string, AreaStyle>;
  coverage: AreaCoverageRow[];
  default_stock: number;
  has_boundaries: boolean;
}

/**
 * The basemap the map draws over.
 *
 * Named by the server rather than the bundle so a deployment can repoint it at
 * its own tile server without a frontend rebuild. No key or token appears here:
 * MapLibre GL JS and OpenFreeMap neither authenticate nor bill per view, which
 * is why this response is identical for every caller.
 */
export interface BasemapConfig {
  engine: 'maplibre-gl';
  provider: string;
  style_url: string;
  style_url_dark: string;
  attribution: string;
}

export interface MapConfig {
  basemap: BasemapConfig;
  default_view: { latitude: number; longitude: number; zoom: number };
  levels: MapLevel[];
  metrics: MapMetric[];
  cluster_threshold: number;
  coverage: MapCoverageRow[];
  has_locations: boolean;
  currency: string;
  administrative_areas?: AdministrativeAreaConfig;
}

export interface MapCluster {
  latitude: number;
  longitude: number;
  count: number;
  value: number;
  sample: string[];
  codes: string[];
}

export interface MapBounds {
  north: number;
  south: number;
  east: number;
  west: number;
  centre: { latitude: number; longitude: number };
}

/*
 * `MapPoint` and `MapDataResponse` are gone with the renderers that consumed
 * them. `/api/map/data` still exists and is still tested server-side, but the
 * browser reads `/api/map/entities` — every level in one call — and turns it
 * into GeoJSON in `components/map/businessGeoJson.ts`, so a point shape in the
 * frontend would describe nothing the frontend draws.
 */

/** Every entity type the map can draw, in hierarchy order. */
export type MapLayer =
  | 'company'
  | 'bu'
  | 'sales_line'
  | 'zone'
  | 'region'
  | 'area'
  | 'unit'
  | 'territory'
  | 'sub_territory'
  | 'customer'
  | 'sales_force';

export interface MapEntityPoint {
  type: MapLayer;
  id: string;
  code: string;
  name: string;
  parent_type: MapLayer | null;
  parent_id: string | null;
  parent_code: string | null;
  latitude: number | null;
  longitude: number | null;
  value: number | null;
  location_source: string | null;
}

export interface MapEntitiesResponse {
  period: DateRange;
  filters: Record<string, string>;
  applied_filters: Record<string, string>;
  scope_description: string;
  metric: string;
  metric_label: string;
  entities: MapEntityPoint[];
  markers: Record<string, ResolvedMarkerConfig>;
  layers: MapLayer[];
  /** Entities in scope, whether or not their layer is switched on. */
  counts: Record<string, number>;
  placed_counts: Record<string, number>;
  totals: { entities: number; placed: number; unplaced: number };
  clusters: Record<string, MapCluster[]>;
  unplaced: { type: string; code: string; name: string; reason: string }[];
  bounds: MapBounds | null;
  diagnostics?: {
    selected_filters: Record<string, string>;
    selected_level: string | null;
    selected_code: string | null;
    resolved_ancestors: Record<string, string[]>;
    resolved_descendants: Record<string, string[]>;
    resolved_business_codes: Record<string, string[]>;
    entity_counts: Record<string, number>;
    placed_counts: Record<string, number>;
    scope_empty: boolean;
  };
}

export interface MapEntityLocation {
  location_id: number;
  entity_type: string;
  entity_code: string;
  latitude: number;
  longitude: number;
  source: string;
  precision: string;
  label: string | null;
  derived_from: number | null;
  updated_by: string | null;
  updated_at: string;
}

export interface UploadSummary {
  total_uploads: number;
  by_status: Record<string, number>;
  pending_imports: number;
  failed_imports: number;
  partial_imports: number;
  completed_imports: number;
  uploads_last_24h: number;
  failed_records: number;
  recent: UploadBatch[];
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

/** The concrete window a report covers, resolved by the backend. */
export interface DateRange {
  type: string;
  date_from: string;
  date_to: string;
  label: string;
  financial_year?: string | null;
  compare_from?: string | null;
  compare_to?: string | null;
  compare_label?: string | null;
}

export interface PeriodOption {
  value: string;
  label: string;
  date_from: string;
  date_to: string;
}

export interface PeriodOptionsResponse {
  options: PeriodOption[];
  financial_year_start_month: number;
  current_financial_year: string;
}

/** Every organisational and entity filter the reports accept. */
export interface GlobalFilters {
  company_code?: string;
  bu_code?: string;
  sales_line_code?: string;
  zone_code?: string;
  region_code?: string;
  area_code?: string;
  unit_code?: string;
  territory_code?: string;
  sub_territory_code?: string;
  customer_code?: string;
  sales_force_code?: string;
  /** One production batch. Free text — batches are transaction data, not master data. */
  batch_code?: string;
  /**
   * One material, as the Material Master spells it. Narrows sales, target and
   * stock alike since revision 0022: all three name a Material Code, so one
   * item filter serves every report.
   */
  material_code?: string;
  /** The Material Master's top classification level. Global, like the two beside it. */
  material_group_code?: string;
  /**
   * One material brand, by name — `material_brand_code` is `'0'` on every
   * material, so it identifies nothing. This is the brand every report groups
   * by; there is no second, sales-side brand any more.
   */
  material_brand?: string;
  /**
   * Where a stock position is held. Stock only: a sale states no plant and no
   * storage location, and the backend skips a filter a view cannot honour
   * rather than returning nothing.
   */
  plant_code?: string;
  /**
   * One storage location as `plant|location`. Never the bare location code,
   * which is unique only within its plant — `FG01` names 40 different places.
   */
  storage_location_key?: string;
  /** One shelf-life bucket: EXPIRED, EXPIRING_SOON, VALID or NO_EXPIRY. */
  expiry_status?: string;
  // No `volume_unit`. A transaction line records one Total Volume and no unit
  // of measure, so there is no subset of a report to select.
}

export type FilterLevel = keyof GlobalFilters;

export interface HierarchyLevel {
  level: string;
  label: string;
  parent: string | null;
  depth: number;
}

export interface LevelsResponse {
  levels: HierarchyLevel[];
  independent: { level: string; label: string }[];
  /**
   * The material-stock cascade, as the backend declares it: two chains, one
   * for where a position is held and one for how it is classified.
   */
  stock: { level: string; label: string; parent: string | null }[];
}

/**
 * The parent levels implied by a selection.
 *
 * Two views of one answer, because two kinds of page ask it:
 *
 * - `ancestors` maps a level to **every** parent the selection implies. Three
 *   materials stocked across two plants imply both plants, and a bar that shows
 *   one of them would be lying about what the report is filtered on.
 * - `ancestor` is the same answer restricted to the levels that resolved to
 *   exactly one value, for the pages that hold one value per level. A level
 *   with several parents is absent rather than narrowed to an arbitrary one.
 *
 * Both omit the level asked about. Where the request names a `source`, parents
 * that only that page's transaction data can supply are included too: a material
 * has no plant in any master, so where it is held comes from the stock position.
 */
export interface AncestorsResponse {
  level: string;
  codes: string[];
  source: string | null;
  ancestors: Record<string, string[]>;
  ancestor: Record<string, string>;
}

export interface FilterOption {
  code: string;
  label: string;
  parent_code: string | null;
}

export interface OptionsResponse {
  level: string;
  parent_code: string | null;
  total: number;
  truncated: boolean;
  options: FilterOption[];
}

export interface SearchResult {
  entity_type: string;
  type_label: string;
  code: string;
  label: string;
  route: string;
  filter_level: string;
}

/** A chart the backend considered worth drawing. */
export interface ChartSpec {
  type: 'line' | 'bar' | 'pie' | 'area';
  x_axis: string;
  y_axis: string;
  data: Record<string, unknown>[];
}

/** The uniform shape every Phase 3 tool returns. */
export interface ToolResult {
  tool: string;
  metric: string | null;
  currency: string;
  date_from: string | null;
  date_to: string | null;
  filters: Record<string, unknown>;
  value: number | null;
  values: Record<string, any>;
  rows: Record<string, any>[];
  row_count: number;
  chart: ChartSpec | null;
  facts: string[];
  interpretations: string[];
  notes: string[];
  sources: string[];
  truncated: boolean;
}

/** `stock` is a material stock figure: a plain number under a `(KG/LTR)` label. */
export type KpiFormat =
  | 'currency'
  | 'percent'
  | 'count'
  | 'quantity'
  | 'volume'
  | 'stock';

export interface Kpi {
  key: string;
  label: string;
  value: number | null;
  previous_value: number | null;
  growth_percent: number | null;
  format: KpiFormat;
  /** Set on volume KPIs. One card per unit — they are never summed. */
  unit?: string | null;
}

export interface DashboardResponse {
  period: DateRange;
  filters: Record<string, unknown>;
  kpis: Kpi[];
  summary: ToolResult;
  sales_trend: ToolResult;
  region_performance: ToolResult;
  target_achievement: ToolResult;
  /**
   * Brand-wise ranking with targets beside actuals — the dashboard's general
   * performance view. Ranked by net sales, as every brand table here is.
   */
  top_brands: ToolResult;
  // No sales_volume and no stock_volume. The Sales Volume card was removed from
  // the executive view, and with it the query behind it — the figure is still
  // reported by the Sales page, by the Sales Vol column of `top_brands` and by
  // the agent. Material stock has no volume at all: it is four counted
  // quantities with no pack size behind them.
}

export interface PageResponse {
  period: DateRange;
  filters: Record<string, unknown>;
  [section: string]: unknown;
}

export interface SalesPage extends PageResponse {
  summary: ToolResult;
  growth: ToolResult;
  daily_trend: ToolResult;
  monthly_trend: ToolResult | null;
  target_vs_actual: ToolResult;
  region_performance: ToolResult;
  brand_performance: ToolResult;
  customer_performance: ToolResult;
  salesforce_performance: ToolResult;
  territory_performance: ToolResult;
}

/**
 * Material stock: a current position, not a period total.
 *
 * Every section is a `ToolResult` from the same tools the AI assistant calls,
 * so the page and a chat answer cannot disagree about a number. There is no
 * coverage, no low-stock and no out-of-stock section: all three divided stock
 * by an average daily sales *rate*, and a rate needs two readings and the time
 * between them. A stock position carries no posting date, so there is one
 * reading and no elapsed time. Stock and sales do share a Material Code since
 * revision 0022 — what cannot be derived is the rate, not the join.
 */
export interface StockPage extends PageResponse {
  summary: ToolResult;
  by_plant: ToolResult;
  by_storage_location: ToolResult;
  /** Ranked by total stock and capped server-side: materials run to thousands. */
  by_material: ToolResult;
  by_material_group: ToolResult;
  /** By material brand — the same brand a sales report ranks by, one master. */
  by_material_brand: ToolResult;
  expiry: ToolResult;
  expiring: ToolResult;
}

export interface TargetPage extends PageResponse {
  summary: ToolResult;
  region_achievement: ToolResult;
  territory_achievement: ToolResult;
  /**
   * Achievement by material brand: the same table the dashboard ranks its top
   * brands with, so a brand's target and its net sales read identically in both
   * places. Its columns are the brand-target ones (volume beside amount, two
   * achievements, two shortfalls), not the region/territory achievement set.
   */
  brand_achievement: ToolResult;
  gap: ToolResult;
}

export interface PerformancePage extends PageResponse {
  level: string;
  next_level: string | null;
  drill_chain: string[];
  performance: ToolResult;
  achievement: ToolResult | null;
}

/**
 * The analysis levels Material Analysis offers — the Material Master's three,
 * finest first. `material` is the default because it is the grain the sales
 * data is stated at.
 */
export type MaterialAnalysisLevel =
  | 'material'
  | 'material_brand'
  | 'material_group';

/** Present only when the filters name exactly one material brand. */
export interface BrandDetail {
  brand: string;
  summary: ToolResult;
  groups: ToolResult;
  materials: ToolResult;
  monthly_trend: ToolResult;
  territories: ToolResult;
  customers: ToolResult;
  volume: ToolResult;
}

export interface MaterialsPage extends PageResponse {
  level: MaterialAnalysisLevel;
  levels: MaterialAnalysisLevel[];
  materials: ToolResult;
  top: Record<string, any>[];
  bottom: Record<string, any>[];
  brand_detail: BrandDetail | null;
}

export interface CustomersPage extends PageResponse {
  customers: { rows: Record<string, any>[]; row_count: number; truncated: boolean };
  top_customers: Record<string, any>[];
  inactive_customers: Record<string, any>[];
  note: string;
}

export interface TransactionsResponse {
  data_type: string;
  period: DateRange;
  columns: string[];
  rows: Record<string, any>[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export interface ChatMessageResponse {
  conversation_id: string;
  /** The stored assistant turn, which is what feedback is attached to. */
  message_id: number | null;
  intent: string;
  answer: string;
  data: Record<string, any>;
  filters: Record<string, unknown>;
  date_range: Record<string, unknown>;
  sources: string[];
  assumptions: string[];
  chart: ChartSpec | null;
  needs_clarification: boolean;
  error_code: string | null;
  language: string;
  tools_used: string[];
  elapsed_ms: number;
}

export interface Conversation {
  conversation_id: string;
  title: string | null;
  started_at: string;
  last_message_at: string | null;
  message_count: number;
}

export interface ChatHistoryMessage {
  message_id: number;
  role: 'user' | 'assistant';
  message: string;
  intent: string | null;
  language: string | null;
  created_at: string;
  elapsed_ms: number | null;
  /** This reader's own verdict, so a reloaded thread shows the thumb they pressed. */
  feedback: FeedbackRating | null;
}

export type FeedbackRating = 'UP' | 'DOWN';

// --- Agent learning -------------------------------------------------------

export type SignalStatus = 'NEW' | 'TRIAGED' | 'PROPOSED' | 'DISMISSED';
export type LearningStatus = 'PROPOSED' | 'ACTIVE' | 'REJECTED' | 'RETIRED';
export type AliasKind = 'ENTITY' | 'METRIC' | 'GROUP_BY' | 'MODIFIER';

/** One phrase the assistant handled badly, and how often. */
export interface LearningSignal {
  signal_id: number;
  signal_type: string;
  phrase: string;
  raw_sample: string | null;
  language: string | null;
  occurrences: number;
  status: SignalStatus;
  first_seen_at: string;
  last_seen_at: string;
}

/** A phrase somebody taught the assistant, and what it means. */
export interface TermAlias {
  alias_id: number;
  phrase: string;
  language: string | null;
  alias_kind: AliasKind;
  entity_type: string | null;
  entity_code: string | null;
  target_keyword: string | null;
  /** Human-readable rendering of whichever target the kind uses. */
  target: string;
  status: LearningStatus;
  source: string;
  signal_id: number | null;
  notes: string | null;
  created_by: string | null;
  approved_by: string | null;
  approved_at: string | null;
  retired_at: string | null;
}

/** A question and the tool call that answered it correctly. */
export interface LearningExample {
  example_id: number;
  question: string;
  normalized_question: string;
  language: string | null;
  intent: string | null;
  tool_name: string;
  arguments: Record<string, unknown> | null;
  status: LearningStatus;
  source: string;
  use_count: number;
  notes: string | null;
  created_by: string | null;
  approved_by: string | null;
  approved_at: string | null;
  retired_at: string | null;
}

/**
 * What a reviewer may choose from.
 *
 * Served by the backend rather than restated here: a keyword added to the
 * agent's own tables becomes selectable with no frontend change.
 */
export interface LearningOptions {
  alias_kinds: AliasKind[];
  alias_targets: Record<AliasKind, string[]>;
  signal_types: string[];
  signal_statuses: SignalStatus[];
  statuses: LearningStatus[];
  sources: string[];
  /** Read off the tool registry, so it follows the server with no edit here. */
  tools: { name: string; description: string }[];
}

export interface FeedbackResponse {
  message_id: number;
  rating: FeedbackRating;
  /** True when this replaced an earlier verdict from the same reader. */
  updated: boolean;
  /** True when the note carried injection-style instructions and was stripped. */
  sanitized: boolean;
}

export type Severity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';

export interface Alert {
  alert_type: string;
  severity: Severity;
  entity: string;
  entity_type: string;
  metric: string;
  current_value: number | null;
  threshold: number | null;
  recommended_attention: string;
  category: string;
  link: string;
}

export interface AlertsResponse {
  period: DateRange;
  alerts: Alert[];
  total: number;
  counts_by_severity: Partial<Record<Severity, number>>;
  thresholds: Record<string, number>;
}

export interface Notification {
  notification_id: number;
  category: string;
  severity: Severity;
  title: string;
  body: string | null;
  link: string | null;
  is_read: boolean;
  created_at: string;
}

export interface NotificationsResponse {
  unread_count: number;
  notifications: Notification[];
}

export interface EtlBatch {
  batch_id: number;
  batch_uuid: string;
  data_type: string;
  source_type: string;
  source_system: string;
  source_file: string | null;
  load_mode: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  total_rows: number;
  successful_rows: number;
  failed_rows: number;
  duplicate_rows: number;
  inserted_rows: number;
  updated_rows: number;
}

export interface BatchQuality {
  batch_id: number;
  data_type: string;
  status: string;
  totals: {
    total_imported: number;
    valid: number;
    rejected: number;
    duplicate: number;
    inserted: number;
    updated: number;
    rejection_rate_percent: number;
  };
  by_category: Record<string, number>;
  by_error_code: {
    error_code: string;
    count: number;
    category: string | null;
    description: string | null;
  }[];
  rejected_records?: {
    total: number;
    records: {
      id: number;
      source_row_number: number | null;
      error_code: string;
      error_category: string;
      error_message: string;
      field_name: string | null;
      field_value: string | null;
      raw_data: Record<string, unknown> | null;
    }[];
  };
}

export interface AuditLogEntry {
  audit_id: number;
  user_id: number | null;
  username: string | null;
  action: string;
  resource: string | null;
  detail: Record<string, unknown> | null;
  ip_address: string | null;
  success: boolean;
  created_at: string;
}

export interface ExportRequest {
  format: 'xlsx' | 'csv' | 'pdf';
  title: string;
  report_name?: string;
  date_range?: string;
  filters?: Record<string, unknown>;
  rows: Record<string, unknown>[];
  kpis?: string[][];
}

export interface WhatsAppStatus {
  provider: string;
  configured: boolean;
  verify_token_set: boolean;
  signature_check_enabled: boolean;
  require_verified_profile: boolean;
  linked_users: number;
}

/**
 * One aggregated business point, as `/api/map/data` returns it.
 *
 * `value` is whatever metric was asked for. `measures` carries the rest of the
 * aggregate row, which for `metric=achievement` includes `net_sales`,
 * `target_amount` and `achievement_percent` — all summed and divided on the
 * server, so a bubble's size and its colour come from the same query the
 * reports read and no business figure is derived in the browser.
 */
export interface MapAggregatePoint {
  code: string;
  label: string;
  latitude: number;
  longitude: number;
  value: number;
  measures: Record<string, number | string | null>;
  location_source: string;
  location_precision: string;
}

export interface MapPointsResponse {
  level: string;
  metric: string;
  metric_label: string;
  clustered: boolean;
  points: MapAggregatePoint[];
  /** Records the metric covers but that have nowhere to be drawn, and why. */
  unplaced: { code: string; label: string; value: number; reason: string }[];
  totals: {
    plotted: number;
    unplaced: number;
    total_value: number;
    min_value: number;
    max_value: number;
  };
  bounds: { west: number; south: number; east: number; north: number } | null;
}
