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
  | 'target_management'
  | 'performance'
  | 'materials'
  | 'customers'
  | 'credit_control'
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

/**
 * What a user may do *inside* a section they hold.
 *
 * `APPROVE` and `REVISE` belong to Target Management and are two actions on
 * purpose: signing off on a figure and asking for it to change are different
 * privileges, held by different people at different levels of the hierarchy.
 */
export type SectionAction =
  | 'VIEW'
  | 'CREATE'
  | 'EDIT'
  | 'DELETE'
  | 'EXPORT'
  | 'UPLOAD'
  | 'APPROVE'
  | 'REVISE';

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

/**
 * One chain a data scope can be granted in, as `/api/admin/roles` describes it.
 *
 * Sent by the server rather than derived here, so the admin form carries no
 * list of which level belongs to which chain — the thing that would go stale
 * the day a third chain exists. A level in two chains is offered by the first,
 * so `company_code` appears under the sales hierarchy and not under plants.
 */
export interface ScopeDimension {
  key: string;
  /** The server's own English name for the chain; a fallback for the i18n key. */
  label: string;
  levels: { code_field: string; label: string }[];
}

export interface ManagedEntity {
  /**
   * The heading this record is listed under on the two data screens.
   *
   * Presentation only, and deliberately not `category`: that is MASTER or
   * TRANSACTIONAL, decides the route and the backend's processing path, and is
   * stored on every upload batch. A group may mix categories.
   */
  group?: string;
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

/**
 * One row of a management table.
 *
 * `_key` addresses the record in a URL — the whole business key, joined, for
 * the dimensions keyed on more than one column. `_removable` is the server's
 * answer for *this row* rather than for the entity: a Map Locations row can be
 * a coordinate somebody placed or a centroid the system recomputes, and only
 * the first is anybody's to remove. Absent means removable.
 */
export type ManagedRow = Record<string, unknown> & {
  _key: string;
  _removable?: boolean;
};

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
  /**
   * The heading this type is listed under on the two data screens.
   *
   * Presentation only. `category` below is MASTER or TRANSACTIONAL, decides
   * the processing path and is stored on every upload batch; the two are
   * separate on purpose.
   */
  group?: string;
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
  | 'STAGING'
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
  /**
   * Exact credit term in days, as the invoice states it.
   *
   * Credit Control only. A sale carries no terms — an invoice does — so the
   * backend skips this on any other view rather than returning nothing.
   */
  credit_days?: string;
  /** How the invoice is settled: CASH or CREDIT. Credit Control only. */
  payment_mode?: string;
  /**
   * NOT_YET_DUE, OVER_DUE or CLEARED.
   *
   * Derived rather than stored, and derived **for the reporting date**: the same
   * invoice is Not Yet Due in June and Over Due in August. So this filter and
   * the As On date are read together, and neither means much without the other.
   */
  credit_status?: string;
  /** One aging bucket, derived for the reporting date exactly as the status is. */
  aging_bucket?: string;
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
  /**
   * The lines to draw, when a chart has more than one.
   *
   * Absent on every single-series chart, which is most of them — `y_axis` names
   * the measure there and nothing changed. `get_sales_trend` populates it for a
   * year-against-year trend, keyed by a column present on each row of `data`
   * and *absent* at a position that series has no figure for.
   */
  series?: { key: string; label: string }[];
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
  /**
   * The cards this build serves, named by the server rather than listed here.
   *
   * Each is its own request. They were all in this response until the page took
   * nine seconds to paint — eight independent aggregates run one after another
   * — which is the problem the business map already solved by fetching one
   * layer per request in parallel.
   */
  sections: string[];
}

/** One card of the dashboard. */
export interface DashboardSectionResponse {
  period: DateRange;
  filters: Record<string, unknown>;
  name: string;
  section: ToolResult;
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

/**
 * Credit Control's page bundle: everything drawn above the table, in one read.
 *
 * One request rather than six, for the reason the dashboard is one: six queries
 * against the same filtered rows can arrive at six slightly different answers if
 * a load lands between them, and a KPI strip disagreeing with its own chart is
 * worse than a slower page.
 */
export interface CreditControlSummary {
  filters: Record<string, unknown>;
  /** The date every derived figure below was resolved against. */
  as_on_date: string;
  due_soon_days: number;
  metrics: CreditMetrics;
  /** All eight buckets, in order, zeros included — an empty bucket is a fact. */
  aging: CreditAgingRow[];
  /** The same money split by organisational level as well: the aging matrix. */
  aging_by_level: CreditAgingMatrix;
  /** What each group is owed and how much of it is late, ranked. */
  exposure_by_level: CreditExposureBreakdown;
  /** When money that is *not yet late* falls due — the aging chart's other half. */
  due_profile: CreditDueProfile;
  status: CreditStatusRow[];
  top_overdue_customers: CreditTopOverdueRow[];
  outstanding_trend: CreditTrend;
  notes: CreditNote[];
}

export interface CreditMetrics {
  invoice_count: number;
  open_invoice_count: number;
  overdue_invoice_count: number;
  due_soon_invoice_count: number;
  total_invoice_amount: number | null;
  net_invoice_amount: number | null;
  /**
   * Signed, unlike the three deductions below it.
   *
   * A return is posted negative and *subtracted*, so it pushes the net figure
   * above the gross one — the sign is what explains a card reading higher than
   * the invoice total beside it, and a magnitude would hide exactly that.
   */
  return_amount: number | null;
  payment_amount: number | null;
  discount_amount: number | null;
  adjustment_amount: number | null;
  outstanding_amount: number | null;
  /**
   * The three-way partition of everything still owed, by when it falls due.
   *
   * Mutually exclusive and exhaustive over the open book, so the three add to
   * `outstanding_amount` exactly. `due_later_amount` was missing until Stage 3
   * and is the largest of the three on the real data — money that is neither
   * late nor imminent, which the source's own columns do not publish at all.
   */
  overdue_amount: number | null;
  due_soon_amount: number | null;
  due_later_amount: number | null;
  due_later_invoice_count: number;
  /**
   * Both `null` when their denominator is zero, never `0`. A portfolio with
   * nothing outstanding has no overdue *proportion*, and rendering `0%` would
   * read as good news about a book that does not exist.
   */
  payment_rate_percent: number | null;
  overdue_share_percent: number | null;
}

export interface CreditAgingRow {
  bucket: string;
  invoice_count: number;
  outstanding_amount: number;
}

export interface CreditStatusRow {
  status: string;
  invoice_count: number;
  outstanding_amount: number;
  /**
   * What was billed, net of returns.
   *
   * Here because `outstanding_amount` cannot describe CLEARED: a cleared
   * invoice has a balance of zero or below by definition, so that status always
   * read as a count with no money beside it and no way to see how much had
   * actually been settled.
   */
  invoice_amount: number;
}

/**
 * The aging matrix: one row per organisational group, one column per bucket.
 *
 * A matrix rather than a list of triples, because a matrix is what the page
 * draws — flattening it here would put the bucket order, which is a business
 * rule, on the wrong side of the API. `amounts` and `counts` are positionally
 * aligned with `buckets`, and every row carries every bucket including the
 * empty ones: a row of four cells beside a row of eight does not line up.
 */
export interface CreditAgingMatrix {
  level: string;
  buckets: string[];
  rows: CreditAgingMatrixRow[];
}

export interface CreditAgingMatrixRow {
  code: string | null;
  name: string;
  outstanding_amount: number;
  invoice_count: number;
  amounts: number[];
  counts: number[];
}

export interface CreditExposureBreakdown {
  level: string;
  limit: number;
  rows: CreditExposureRow[];
}

export interface CreditExposureRow {
  code: string | null;
  name: string;
  outstanding_amount: number | null;
  overdue_amount: number | null;
  /** `null` where the group has nothing outstanding — never `0`. */
  overdue_share_percent: number | null;
  open_invoice_count: number;
  invoice_count: number;
  customer_count: number;
}

/**
 * The forward horizon. `overdue_amount` rides along **outside** the buckets:
 * overdue money has seven buckets of its own on the aging chart, and the same
 * taka on two charts is taka a reader will add.
 */
export interface CreditDueProfile {
  as_on_date: string;
  overdue_amount: number | null;
  buckets: CreditDueBucket[];
}

export interface CreditDueBucket {
  bucket: string;
  invoice_count: number;
  due_amount: number;
}

export interface CreditTopOverdueRow {
  customer_code: string;
  customer_name: string | null;
  /** Where to go and see them — the action this ranking leads to. */
  territory_code: string | null;
  territory_name: string;
  region_code: string | null;
  region_name: string;
  overdue_amount: number | null;
  invoice_count: number;
  max_days_overdue: number | null;
}

/**
 * The outstanding trend, which this platform cannot currently measure.
 *
 * `state` is `NOT_AVAILABLE` and `points` is empty: the source states one
 * aggregate payment per invoice and a single last payment date, so what was owed
 * at a past month end is unrecorded. `reason` says so, and the page prints it
 * rather than drawing an empty chart — a chart of assumed history looks exactly
 * like a measured one.
 */
export interface CreditTrend {
  state: 'NOT_AVAILABLE' | 'VALID_ZERO' | 'NO_DATA';
  points: { period: string; outstanding_amount: number }[];
  reason: string;
}

export interface CreditNote {
  code: string;
  count: number;
  message: string;
}

/** One invoice row. The last three are derived for the request's As On date. */
export interface CreditInvoiceRow {
  credit_invoice_id: number;
  company_code: string;
  invoice_no: string;
  plant_code: string | null;
  plant_name: string | null;
  customer_code: string;
  customer_name: string | null;
  sub_territory_code: string | null;
  invoice_date: string;
  credit_days: number;
  due_date: string;
  invoice_value: number;
  return_amount: number;
  net_invoice_amount: number;
  payment_amount: number;
  discount_amount: number;
  adjustment_amount: number;
  balance_amount: number;
  payment_mode: string | null;
  last_payment_date: string | null;
  clearing_date: string | null;
  clearing_document: string | null;
  /** Pipe-separated, or `null`. Shown, never used to hide the row. */
  data_quality_flag: string | null;
  days_overdue: number | null;
  credit_status: string;
  /** `null` on a cleared invoice: aging measures money still owed. */
  aging_bucket: string | null;
}

interface CreditPage {
  filters: Record<string, unknown>;
  as_on_date: string;
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface CreditInvoicePage extends CreditPage {
  rows: CreditInvoiceRow[];
}

export interface CreditCustomerRow {
  company_code: string;
  customer_code: string;
  customer_name: string | null;
  sub_territory_code: string | null;
  invoice_count: number;
  total_invoice_amount: number | null;
  net_invoice_amount: number | null;
  payment_amount: number | null;
  discount_amount: number | null;
  adjustment_amount: number | null;
  outstanding_amount: number | null;
  overdue_amount: number | null;
  overdue_invoice_count: number;
  oldest_due_date: string | null;
  last_payment_date: string | null;
  /**
   * This customer's share of the whole filtered portfolio, not of the page.
   * `null` when nothing is outstanding. The Customer Master carries no credit
   * limit, so a limit-versus-used exposure would have to be invented; a share of
   * the portfolio is a real ratio of two figures this system holds.
   */
  credit_exposure_percent: number | null;
}

export interface CreditCustomerPage extends CreditPage {
  rows: CreditCustomerRow[];
  portfolio_outstanding: number;
  page_outstanding: number;
}

export interface CreditInvoiceDetail {
  as_on_date: string;
  invoice: CreditInvoiceRow;
  /**
   * At most one payment event, and it may be undated. The source aggregates
   * payments into one figure, so a schedule cannot be reconstructed and none is
   * invented; where the file states no last payment date the event is reported
   * without one rather than dropped, because the money is real.
   */
  payment_events: {
    date: string | null;
    amount: number | null;
    kind: 'PAYMENT' | 'CLEARING';
    note: string;
  }[];
  data_quality_flags: string[];
}

export interface TargetPage extends PageResponse {
  summary: ToolResult;
  /**
   * The target drawn over time beside the last three years of actuals.
   *
   * Null for a window of a month or less, where two years of one month is two
   * points and a legend — the KPI cards above already say that better.
   */
  monthly_trend: ToolResult | null;
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

// ---------------------------------------------------------------------------
// Target Management
//
// Separate from `TargetPage` above, which reports achievement against targets
// that already exist. These describe the plan a target is *built* under.
// ---------------------------------------------------------------------------

/** The nine states a plan version moves through. Mirrors `TargetStatus`. */
export type TargetPlanStatus =
  | 'DRAFT'
  | 'ALLOCATION_IN_PROGRESS'
  | 'ALLOCATED'
  | 'UNDER_REVIEW'
  | 'PARTIALLY_APPROVED'
  | 'APPROVED'
  | 'REJECTED'
  | 'LOCKED'
  | 'REVISED';

/** `FY` is the whole financial year; the quarters are quarters *of* it. */
export type TargetPeriod = 'FY' | 'Q1' | 'Q2' | 'Q3' | 'Q4';

export interface TargetPlan {
  plan_id: number;
  plan_code: string;
  financial_year: string;
  target_period: TargetPeriod;
  /** `Q1 (Jul – Sep)`, derived from the configured financial-year start month. */
  period_label: string;
  company_code: string;
  bu_code: string;
  sales_line_code: string;
  basis_financial_years: string | null;
  status: TargetPlanStatus;
  created_by: string | null;
  created_at: string | null;
  current_version_id: number | null;
  current_version_no: number | null;
  current_version_status: TargetPlanStatus | null;
}

export interface TargetVersion {
  version_id: number;
  plan_id: number;
  version_no: number;
  /** `V3` — what every screen calls it. */
  label: string;
  status: TargetPlanStatus;
  is_current: boolean;
  reason: string | null;
  created_by: string | null;
  created_at: string | null;
  submitted_at: string | null;
  approved_at: string | null;
  locked_at: string | null;
  locked_batch_id: string | null;
  country_line_count: number | null;
}

/**
 * One material's country target.
 *
 * `quantity` and `value` are derived by the backend from the material master's
 * conversion factor and transfer price, and are `null` when it states neither —
 * which is not zero, and is why `missing` names the inputs that were absent
 * rather than leaving the browser to infer it from two nulls.
 */
export interface TargetCountryLine {
  material_code: string;
  material_description: string | null;
  material_brand: string | null;
  material_group_name: string | null;
  company_code: string | null;
  conversion_factor: number | null;
  transfer_price: number | null;
  target_volume: number;
  quantity: number | null;
  value: number | null;
  /** `conversion_factor` and/or `transfer_price` — whichever the master lacks. */
  missing: ('conversion_factor' | 'transfer_price')[];
}

/**
 * The country total.
 *
 * `quantity` and `value` are `null` unless *every* line derived. A total over a
 * mixture of derivable and non-derivable lines is short by an unknown amount,
 * so it is suppressed rather than shown — `missing_conversion_factor` and
 * `missing_transfer_price` name what has to be loaded to unsuppress it.
 */
export interface TargetCountryTotals {
  line_count: number;
  target_volume: number;
  quantity: number | null;
  value: number | null;
  derivable_count: number;
  missing_conversion_factor: string[];
  missing_transfer_price: string[];
}

export interface TargetCountryTargetResponse {
  plan: TargetPlan;
  version: TargetVersion;
  lines: TargetCountryLine[];
  totals: TargetCountryTotals;
  /** Why a total is suppressed, in the words the backend chose. */
  notes: string[];
  editable: boolean;
}

/** Which rule the allocation engine should follow for one material. */
export type TargetBasis =
  | 'TWO_YEAR_AVERAGE'
  | 'GROWTH_WEIGHTED'
  | 'NEW_MATERIAL'
  | 'NO_HISTORY';

/**
 * One material's volume in one financial year.
 *
 * `volume` is `null` when the material had no sales at all that year — which is
 * not `0`, and is why growth against it reads `n/a` rather than a number.
 * `complete` is false when some contributing sales row stated no volume, so the
 * total shown is the sum of the rows that did.
 */
export interface TargetHistoryYear {
  financial_year: string;
  volume: number | null;
  rows: number;
  rows_without_volume: number;
  complete: boolean;
}

export interface TargetHistoryRow {
  material_code: string;
  material_description: string | null;
  material_brand: string | null;
  material_group_name: string | null;
  years: TargetHistoryYear[];
  /** The same volumes flattened, oldest first — one column per basis year. */
  volumes: (number | null)[];
  growth_percent: number | null;
  average_volume: number | null;
  contribution_percent: number | null;
  current_target_volume: number | null;
  target_growth_percent: number | null;
  basis: TargetBasis;
  complete: boolean;
}

export interface TargetHistoryResponse {
  plan: TargetPlan;
  version: TargetVersion;
  /** Oldest first. Length drives how many volume columns the table draws. */
  basis_years: string[];
  rows: TargetHistoryRow[];
  totals: {
    material_count: number;
    years: {
      financial_year: string;
      volume: number | null;
      materials_with_volume: number;
    }[];
    target_volume: number;
    with_history: number;
    incomplete_materials: string[];
    without_history: string[];
  };
  notes: string[];
  growth_guidance_percent: number;
}

/** Where one allocation run has got to. Distinct from the *version's* status. */
export type TargetAllocationJobStatus =
  | 'QUEUED'
  | 'PROCESSING'
  | 'COMPLETED'
  /**
   * Allocated and reconciled, with something a planner should read — a seasonal
   * fallback, a node split evenly for want of history, a level shallower than
   * customer. Its own status rather than a flag on `COMPLETED`, because a run
   * nobody needs to look at and a run somebody does are different things to a
   * person scanning a list of twenty.
   */
  | 'COMPLETED_WITH_WARNINGS'
  | 'FAILED'
  | 'CANCELLED'
  /**
   * The engine ran, found no sales history and correctly refused to invent an
   * allocation. Not a failure — nothing went wrong, the data it needs has not
   * been loaded — and drawn as its own state so nobody hunts a bug that is not
   * there.
   */
  | 'NO_HISTORY';

export interface TargetAllocationStage {
  key: string;
  label: string;
}

export interface TargetReconciliation {
  balanced: boolean;
  /** Decimal strings, not numbers: exactness is the whole point. */
  country_target_volume: string;
  allocated_volume: string;
  difference: string;
  /** `null` at a zero target — a percentage of nothing is undefined. */
  allocation_percent: number | null;
  mismatches: {
    kind: string;
    level: string | null;
    node_code: string | null;
    material_code: string | null;
    target_month: string | null;
    expected: string;
    actual: string;
    difference: string;
  }[];
  mismatch_count: number;
  levels_checked: string[];
  node_count: number;
}

export interface TargetFactorAvailability {
  key: string;
  label: string;
  enabled: boolean;
  weight: number;
  available: boolean;
  reason: string | null;
}

export interface TargetAllocationJob {
  job_id: string;
  plan_id: number;
  version_id: number;
  status: TargetAllocationJobStatus;
  current_stage: string | null;
  current_stage_label: string | null;
  stages: TargetAllocationStage[];
  progress_percent: number;
  rows_processed: number;
  total_rows: number | null;
  error_count: number;
  error_message: string | null;
  settings: {
    weights: Record<string, number>;
    enabled: Record<string, boolean>;
    management_adjustment: Record<string, number>;
  } | null;
  result: {
    reconciliation: TargetReconciliation | null;
    months?: string[];
    materials?: string[];
    material_count?: number;
    node_count?: number;
    customer_count?: number;
    row_count?: number;
    factors?: TargetFactorAvailability[];
    warnings?: string[];
    equal_split_nodes?: string[];
    /** The deepest level this run reached — never assumed to be customer. */
    allocation_level?: string;
    /** What each management adjustment did: system, adjustment, final. */
    adjustments?: TargetAppliedAdjustment[];
    sales_rows_found?: number;
    /** Present only on a NO_HISTORY result. */
    basis_years?: string[];
  } | null;
  requested_by: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
  /**
   * What one finished run came to. `rows_saved` equals `rows_generated` by
   * construction — persist writes every row or raises — and
   * `duplicates_rejected` is always 0 because the full-grain unique constraint
   * makes one impossible, which is a different claim from "we did not look".
   */
  summary?: {
    rows_generated: number;
    rows_saved: number;
    duplicates_rejected: number;
    validation_failures: number;
    warnings: number;
    projected_rows: number | null;
    allocation_level: string | null;
    sales_rows_found: number | null;
    processing_seconds: number | null;
  };
}

export interface TargetAllocationState {
  plan: TargetPlan;
  version: TargetVersion;
  reconciliation: TargetReconciliation;
  job: TargetAllocationJob | null;
  has_allocation: boolean;
  editable: boolean;
}

export interface TargetFactorCatalogue {
  factors: {
    key: string;
    label: string;
    kind: 'SHARE' | 'MODE';
    description: string;
    default_weight: number;
    default_enabled: boolean;
    requires: string;
    /** False where the platform holds no source for the factor at all. */
    supported: boolean;
    unsupported_reason: string | null;
  }[];
  defaults: {
    weights: Record<string, number>;
    enabled: Record<string, boolean>;
    management_adjustment: Record<string, number>;
  };
  stages: TargetAllocationStage[];
  new_node_seed_share: number;
  growth_guidance_percent: number;
}

/**
 * The five states a piece of data can be in.
 *
 * They are five because they call for five different actions, and a business
 * user reading a bare `0` or a bare dash cannot tell them apart: sold nothing,
 * nothing loaded, loaded but missing the column this needs, present and
 * self-contradictory, or a master that does not exist in the platform at all.
 */
export type TargetDataState =
  | 'AVAILABLE'
  | 'VALID_ZERO'
  | 'NO_DATA'
  | 'INSUFFICIENT_DATA'
  | 'INVALID_DATA'
  | 'NOT_AVAILABLE'
  | 'NOT_APPLICABLE';

export interface TargetReadinessCheck {
  key: string;
  label: string;
  state: TargetDataState;
  tone: 'ok' | 'blocked' | 'error' | 'muted';
  ok: boolean;
  /** Whether this check stops the run — not the same as its data state. */
  blocking: boolean;
  /** True where the data is absent but the run may proceed anyway. */
  advisory: boolean;
  detail: string;
  action: string | null;
  facts: Record<string, unknown>;
}

export interface TargetCustomerMappingHealth {
  total_customers: number;
  mapped_to_sub_territory: number;
  unmapped: number;
  mapped_to_unknown_sub_territory: number;
  usable: number;
}

export interface TargetProjection {
  projected_rows: number;
  maximum_rows: number;
  /** Rows left under the ceiling. Negative when the plan is over it. */
  available_rows: number;
  capacity_used_percent: number | null;
  /** What the engine would hold, at ~400 bytes per row (measured). */
  estimated_memory_mb: number;
  exceeds: boolean;
  financial_year: string;
  target_period: string;
  months: number;
  materials: number;
  nodes: number;
  customers: number;
  allocation_level: string;
  narrowing_options: string[];
}

export interface TargetReadiness {
  plan: TargetPlan;
  version: TargetVersion;
  checks: TargetReadinessCheck[];
  ready: boolean;
  blocking: string[];
  /** The deepest level an allocation could reach on this data. */
  allocation_level: string;
  allocation_level_is_customer: boolean;
  customer_mapping: TargetCustomerMappingHealth;
  hierarchy: { counts: Record<string, number>; broken_parent_links: Record<string, number> };
  projection: TargetProjection;
  basis_years: string[];
  factors: TargetFactorAvailability[];
}

export interface TargetAllocationRun {
  job_id: string;
  plan_id: number;
  version_id: number;
  status: TargetAllocationJobStatus;
  started_by: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
  projected_rows: number | null;
  generated_rows: number;
  allocation_level: string | null;
  sales_rows_found: number | null;
  /** Three-valued: `null` means the run never got far enough to look. */
  sales_data_available: boolean | null;
  error_count: number;
  warning_count: number;
  error_message: string | null;
}

/**
 * A standing management adjustment: a node, an absolute volume and a reason.
 *
 * Absolute and signed, never a percentage — a uniform percentage re-normalised
 * is a mathematical no-op. It is an *input* to the engine, so it takes effect
 * on the next run rather than editing figures already generated.
 */
export interface TargetAdjustment {
  adjustment_id: number;
  level: string;
  node_code: string;
  material_code: string | null;
  adjustment_volume: string;
  reason: string;
  adjusted_by: string | null;
  adjusted_at: string | null;
}

/** What one adjustment did on the last run, for the screen's columns. */
export interface TargetAppliedAdjustment {
  level: string;
  node_code: string;
  material_code: string | null;
  system_volume: string;
  adjustment_volume: string;
  final_volume: string;
  /** `null` against a system volume of zero — undefined, not infinite. */
  adjustment_percent: number | null;
  reason: string;
  adjusted_by: string | null;
  adjusted_at: string | null;
}

/**
 * One node of the hierarchical review.
 *
 * `depth` and reading order carry the shape — the backend flattens the tree so
 * the browser can draw it as a table and keep the column arrangement every
 * other report table has.
 */
export interface TargetReviewRow {
  level: string;
  node_code: string;
  name: string | null;
  parent_level: string | null;
  parent_code: string | null;
  depth: number;
  has_children: boolean;
  target_volume: number;
  /** What the engine generated, before any management adjustment moved it. */
  system_volume: number;
  adjusted: boolean;
  /** `null` where a material on this node states no conversion factor. */
  target_quantity: number | null;
  target_value: number | null;
  missing_derivation: string[];
  previous_year_volume: number | null;
  /** `null` against a year with no sales — undefined, not −100%. */
  growth_percent: number | null;
  actual_volume: number | null;
  /** `null` before the period has any actuals. Never rendered as 0%. */
  achievement_percent: number | null;
  /** Child total minus this node's own figure. Zero at every level. */
  recon_variance: number;
  pending_revisions: number;
  status: string;
}

export interface TargetReviewResponse {
  plan: TargetPlan;
  version: TargetVersion;
  rows: TargetReviewRow[];
  reconciliation: {
    balanced: boolean;
    mismatched_nodes: number;
    node_count: number;
    target_volume: number;
  };
  /** How this reader's scope narrowed the tree, in words. */
  scope: string;
  notes: string[];
  materials: string[];
  material_code: string | null;
  levels: string[];
}

/**
 * One role's place in the approval chain.
 *
 * `approval_sequence` is `null` for a role that sits **outside** the chain —
 * an administrator configures the workflow and signs off on nothing. That is a
 * different thing from being first in it, so it is never rendered as 0.
 *
 * `adjustment_limit_percent` is `null` for unlimited and `0` for "may not
 * change a figure at all". Both are real settings and look alike as an empty
 * cell, which is why `limit_label` comes from the backend spelled out.
 */
export interface TargetMatrixRow {
  role: string;
  hierarchy_level: string;
  approval_sequence: number | null;
  can_edit: boolean;
  can_approve: boolean;
  can_reject: boolean;
  can_revise: boolean;
  adjustment_limit_percent: number | null;
  is_active: boolean;
  limit_label: string;
}

export interface TargetMatrixResponse {
  rows: TargetMatrixRow[];
  /** The sequenced, active roles only — the chain as it actually runs. */
  chain: TargetMatrixRow[];
  levels: string[];
  defaults: TargetMatrixRow[];
  my_role: string;
}

/**
 * A request to change one node's figure.
 *
 * Three volumes, and `approved_volume` stays `null` until somebody decides —
 * the screen shows a dash there rather than repeating the requested figure,
 * because "asked for 90,000" and "granted 90,000" must be tellable apart.
 */
export interface TargetRevision {
  revision_id: number;
  version_id: number | null;
  level: string | null;
  node_code: string | null;
  node_name: string | null;
  material_code: string | null;
  system_volume: number;
  requested_volume: number;
  approved_volume: number | null;
  /** `null` when the system volume was zero — a percentage of nothing. */
  change_percent: number | null;
  status: 'PENDING' | 'ESCALATED' | 'APPROVED' | 'REJECTED';
  /** Set only on an ESCALATED request: the role it went to instead. */
  escalated_to_role: string | null;
  reason: string;
  requested_by: string | null;
  decided_by: string | null;
  decided_at: string | null;
  requested_at: string | null;
}

export interface TargetRevisionsResponse {
  plan: TargetPlan;
  version: TargetVersion;
  rows: TargetRevision[];
  open_count: number;
  revisable: boolean;
}

/** One step of the chain, and where this version stands against it. */
export interface TargetChainStep {
  role: string;
  hierarchy_level: string;
  approval_sequence: number;
  /** False for a step that questions a target without signing it off. */
  can_approve: boolean;
  approved: boolean;
  actor: string | null;
  acted_at: string | null;
  is_current: boolean;
}

export interface TargetApprovalAct {
  approval_id: number;
  level: string | null;
  node_code: string | null;
  action: string;
  actor: string | null;
  actor_role: string | null;
  approval_sequence: number | null;
  comment: string | null;
  acted_at: string | null;
}

export interface TargetApprovalState {
  plan?: TargetPlan;
  version?: TargetVersion;
  steps: TargetChainStep[];
  history: TargetApprovalAct[];
  /** What final approval is still waiting on. Empty means nothing. */
  blockers: string[];
  my_role: string;
  my_matrix: TargetMatrixRow | null;
  my_turn: boolean;
  already_acted: string | null;
  current_role: string | null;
}

export interface TargetApprovalOutcome {
  status: TargetPlanStatus;
  steps: TargetChainStep[];
  blockers: string[];
}

export interface TargetQueueVersion {
  plan: TargetPlan;
  version: TargetVersion;
  is_my_turn: boolean;
  waiting_on: string[];
  blockers: string[];
}

export interface TargetQueueRevision extends TargetRevision {
  plan_code: string;
  version_no: number;
  version_status: TargetPlanStatus;
}

export interface TargetApprovalQueue {
  versions: TargetQueueVersion[];
  revisions: TargetQueueRevision[];
  notes: string[];
  role: string;
  matrix: TargetMatrixRow | null;
}

/**
 * Whether a version can be locked, and what is stopping it.
 *
 * `blockers` is empty exactly when `lockable` is true. Both are sent because the
 * screen draws them differently: one decides whether a control exists, the other
 * is a list a reader acts on.
 */
export interface TargetLockState {
  plan?: TargetPlan;
  version?: TargetVersion;
  lockable: boolean;
  blockers: string[];
  /** The ETL batch the lock wrote under, or null before it happens. */
  locked_batch_id: string | null;
  locked_at: string | null;
  /** The role that holds the lock — the last step of the approval chain. */
  locks_role: string | null;
}

export interface TargetLockResult {
  batch_uuid: string;
  rows_written: number;
  rows_inserted: number;
  rows_updated: number;
  /** Grains a previous version wrote that this one does not restate. */
  rows_voided: number;
  status: TargetPlanStatus;
}

/**
 * One entry of the business trail.
 *
 * `old_value` and `new_value` are text because what changed is not always a
 * number: a status moves from Approved to Locked, a version from V2 to V3.
 */
export interface TargetAuditEntry {
  audit_id: number;
  plan_id: number | null;
  version_id: number | null;
  action: string;
  actor: string | null;
  actor_role: string | null;
  node_label: string | null;
  old_value: string | null;
  new_value: string | null;
  reason: string | null;
  occurred_at: string | null;
}

export interface TargetAuditResponse {
  rows: TargetAuditEntry[];
  /** Matching entries in total, not the page — a filter that found forty and
   *  one that found forty thousand must look different. */
  total: number;
  actions: string[];
}

/**
 * One node's movement between two versions.
 *
 * `base_volume` is null for a node the newer version added, `volume` is null
 * for one it dropped, and both `change` and `change_percent` are null in either
 * case — no target and a target of nothing are different statements, and only
 * one of them is a number you can subtract.
 */
export interface TargetComparisonRow {
  level: string;
  node_code: string;
  name: string | null;
  parent_level: string | null;
  parent_code: string | null;
  depth: number;
  base_volume: number | null;
  volume: number | null;
  change: number | null;
  /** Null against a zero or absent base — a ratio against nothing. */
  change_percent: number | null;
  status: 'UNCHANGED' | 'INCREASED' | 'DECREASED' | 'ADDED' | 'REMOVED';
}

export interface TargetComparisonLine {
  material_code: string;
  base_volume: number | null;
  volume: number | null;
  change: number | null;
  change_percent: number | null;
  status: string;
}

export interface TargetComparisonResponse {
  plan: TargetPlan;
  base_version: TargetVersion;
  version: TargetVersion;
  rows: TargetComparisonRow[];
  /** Computed from the tree's roots, never by summing the rows. */
  totals: {
    base_volume: number | null;
    volume: number | null;
    change: number | null;
    change_percent: number | null;
    nodes_changed: number;
    nodes: number;
  };
  /** The typed country volumes on both sides — the figure a person entered. */
  country: TargetComparisonLine[];
  materials: string[];
  material_code: string | null;
  scope: string;
  notes: string[];
  statuses: string[];
}

export interface TargetComparisonOption extends TargetVersion {
  /** Listed and marked rather than hidden, so a missing version is explained. */
  has_allocation: boolean;
}

export interface TargetDashboardPlan {
  plan: TargetPlan;
  version: TargetVersion;
  /** Null when nobody has typed a country target — never 0. */
  country_volume: number | null;
  allocated_volume: number | null;
  /** Null when there is no country target to measure against. */
  allocated_percent: number | null;
  open_revisions: number;
  /** Keys into `attention_reasons`; empty when nothing is waiting. */
  attention: string[];
  locked_at: string | null;
}

export interface TargetDashboardResponse {
  stages: {
    drafting: number;
    allocated: number;
    in_approval: number;
    locked: number;
  };
  plan_count: number;
  financial_year: string | null;
  financial_years: string[];
  plans: TargetDashboardPlan[];
  attention: TargetDashboardPlan[];
  attention_reasons: Record<string, string>;
  my_queue: {
    versions: number;
    revisions: number;
    notes: string[];
    role: string;
  };
  recent: TargetAuditEntry[];
  levels: string[];
}

/**
 * One line of an uploaded country-target file, and what became of it.
 *
 * `raw_volume` is what the file actually said, kept so a rejected cell can be
 * shown back verbatim: telling somebody `12,5OO` was refused is useful where
 * telling them "row 4 was refused" is not.
 */
export interface TargetUploadRow {
  row_number: number;
  material_code: string | null;
  raw_volume: string | null;
  volume: number | null;
  /** The version's current figure, or null if this material is new to it. */
  current_volume: number | null;
  /**
   * `SKIPPED` is a material the file lists with no Target Volume. The
   * template pre-fills every material of the company, so most rows arrive
   * blank — a blank is “nothing stated here”, never a target of zero.
   */
  status: 'NEW' | 'CHANGED' | 'UNCHANGED' | 'SKIPPED' | 'REJECTED';
  error: string | null;
}

export interface TargetUploadPreview {
  plan: TargetPlan;
  version: TargetVersion;
  /** Passed back to apply. A name, never a path. */
  upload_token: string;
  file_name: string;
  editable: boolean;
  rows: TargetUploadRow[];
  counts: {
    read: number;
    rejected: number;
    new: number;
    changed: number;
    unchanged: number;
    /** Listed with no figure — left alone, never written as zero. */
    skipped: number;
    /** Lines already on the version that this file does not name. */
    untouched: number;
  };
  /** False whenever anything is rejected — the upload is all-or-nothing. */
  applicable: boolean;
  headers: { material: string | null; volume: string | null };
  notes: string[];
}

export interface TargetUploadResult {
  created: number;
  updated: number;
  rows_read: number;
  skipped: number;
  untouched: number;
  file_name: string;
}

/** Whether a plan may be deleted, and every reason it may not. */
export interface TargetPlanDeletable {
  deletable: boolean;
  blockers: string[];
}

export interface TargetPlanDeleted {
  plan_code: string;
  versions_removed: number;
  country_lines_removed: number;
}

/** Whether a plan may be deleted, and every reason it may not. */
export interface TargetPlanDeletable {
  deletable: boolean;
  blockers: string[];
}

export interface TargetAvailableMaterial {
  material_code: string;
  material_description: string | null;
  material_brand: string | null;
  material_group_name: string | null;
  conversion_factor: number | null;
  transfer_price: number | null;
}

export interface TargetScopeOption {
  code: string;
  name: string;
}

export interface TargetManagementOptions {
  companies: TargetScopeOption[];
  business_units: (TargetScopeOption & { company_code: string })[];
  sales_lines: (TargetScopeOption & { bu_code: string })[];
  periods: { code: TargetPeriod; label: string }[];
  statuses: TargetPlanStatus[];
  /**
   * What this caller may do here. Presentation only — every endpoint
   * re-resolves the same permission, and a hand-typed request is refused
   * whatever the browser was told.
   */
  actions: Partial<Record<SectionAction, boolean>>;
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


// ---------------------------------------------------------------------------
// Business Map
// ---------------------------------------------------------------------------

/**
 * A basemap the deployment offers, as `GET /api/map/config` publishes it.
 *
 * `kind` says whether `style_url` is a MapLibre style document or a raster
 * `{z}/{x}/{y}` template; the browser wraps a template in the one-source style
 * MapLibre needs rather than being handed a URL it cannot parse. `attribution`
 * is *added* to what the style already carries, never substituted for it.
 */
export interface MapBasemap {
  key: string;
  label: string;
  style_url: string;
  style_url_dark: string | null;
  kind: 'style' | 'raster';
  attribution: string | null;
  /** The glyph source a raster basemap draws labels with; a style has its own. */
  glyphs_url: string | null;
}

export type MapViewMode = 'point' | 'boundary' | 'both';

/** One drawable business level, derived server-side from the org chain. */
export interface MapLevelInfo {
  key: string;
  label: string;
  code_field: string;
  name_field: string;
  table: string;
  parent: string | null;
  group: string;
  depth: number | null;
  promoted: boolean;
  /** False for every level today: no source states a business outline. */
  boundary_available: boolean;
  /** The view modes this level can honour — `['point']` until it has an outline. */
  view_modes: MapViewMode[];
}

export type MapMetricKind = 'currency' | 'quantity' | 'volume' | 'percent' | 'count';

/** One figure a layer may be sized, coloured or ranked by. */
export interface MapMetricInfo {
  key: string;
  label: string;
  /** The feature property that carries the figure. */
  field: string;
  kind: MapMetricKind;
  higher_is_better: boolean;
  signed: boolean;
  unavailable_at: string[];
  description: string;
}

export interface MapStyleBand {
  key: 'good' | 'medium' | 'low' | 'critical';
  min: number | null;
  max: number | null;
  label: string;
  color: string;
}

/**
 * How a figure becomes a colour and a size. Declared once server-side and
 * overridable per layer; the browser builds MapLibre expressions from it and
 * invents no threshold or colour of its own.
 */
export interface MapStyle {
  thresholds: number[];
  band_colors: Record<string, string>;
  bands: MapStyleBand[];
  no_data_color: string;
  sequential: string[];
  diverging: { negative: string; neutral: string; positive: string };
  radius: [number, number];
  cluster: { color: string; text_color: string };
  /** Which of the declared shapes this layer's points are drawn as. */
  shape: string;
  /** The flat colour a point takes where no metric decides one. */
  point_color: string;
  /** How a derived centroid is drawn: the same shape, hollow. */
  derived_opacity: number;
  derived_stroke_width: number;
  /**
   * The colours a point takes when grouped by its parent, and where they run
   * out. Published so the legend can draw a swatch the map is not currently
   * showing, and so nothing on this side picks a colour of its own.
   */
  categorical: string[];
  max_categorical_groups: number;
  focus_color: string;
  group_neutral_color: string;
  /** How an administrative outline is drawn beneath the points. */
  boundary: MapBoundaryStyle;
}

/**
 * The backdrop's paint, declared server-side like every other colour.
 *
 * Deliberately quiet: no metric is aggregated at district grain, so nothing
 * here encodes a figure and a strong fill would be read as meaning that is not
 * there. Selection is emphasis, not information.
 */
export interface MapBoundaryStyle {
  fill_color: string;
  fill_opacity: number;
  line_color: string;
  line_width: number;
  line_opacity: number;
  hover_fill_opacity: number;
  selected_line_color: string;
  selected_line_width: number;
  selected_fill_opacity: number;
  mask_color: string;
  mask_opacity: number;
  label_min_zoom: number;
}

/**
 * One administrative level's outlines, as a file the browser may fetch.
 *
 * **Not a business boundary.** These are divisions, districts and upazilas —
 * published geography drawn as reference beneath the points. No source states
 * where a zone, region, area or territory ends, so none is drawn, and every
 * `MapLevelInfo.boundary_available` stays false.
 */
export interface MapBoundarySet {
  key: string;
  label: string;
  /** Where to fetch it; the browser holds no table of filenames. */
  url: string;
  file: string;
  admin_level: number;
  /** The master these outlines correspond to, e.g. `dim_district`. */
  table: string;
  features: number;
  /** Published so a control can warn before pulling 1.7 MB. */
  bytes: number;
}

export interface MapBoundaryCatalogue {
  sets: MapBoundarySet[];
  /**
   * What each surface opens with, keyed by `MapPurpose`.
   *
   * `analysis` is `null` and `demarcation` names a set, because the two maps
   * answer different questions of the same outlines: on a map of figures a
   * backdrop is ink over the subject, and on a map for judging where a line
   * falls it *is* the subject. One value could not say both, which is why this
   * replaced a single `default` rather than joining it.
   *
   * The browser is told rather than deciding: which surface opens with what is
   * a product decision, and a second copy of it here would be one to keep in
   * step.
   */
  defaults: Record<MapPurpose, string | null>;
  mask_url: string;
  note: string;
}

/**
 * One shape a layer may be drawn with, geometry included.
 *
 * The path arrives from the server rather than living here, which is the rule
 * `0033`'s removal recorded read as strictly as it can be: the renderer is
 * never the only thing that knows a marker's design, and a shape the server
 * allows can never be one the browser has no geometry for.
 */
export interface MapShape {
  key: string;
  label: string;
  /** SVG path data, drawn on a square of `viewbox` units. */
  path: string;
  viewbox: number;
}

export interface MapCoverage {
  entity_type: string;
  label: string;
  total: number;
  placed: number;
  derived: number;
  missing: number;
}

export interface MapConfig {
  basemaps: MapBasemap[];
  default_basemap: string;
  view: { latitude: number; longitude: number; zoom: number };
  levels: MapLevelInfo[];
  promoted_levels: string[];
  view_modes: { key: MapViewMode; label: string; requires_boundary: boolean }[];
  metrics: MapMetricInfo[];
  /** Every shape a layer may be drawn with, with its geometry. */
  shapes: MapShape[];
  /** The maps a design may compose: analysis (figures) or demarcation. */
  purposes: MapPurpose[];
  /** Administrative outlines a reader may draw beneath the points. */
  boundaries: MapBoundaryCatalogue;
  /**
   * What the Area Demarcation tab may be narrowed by, derived server-side from
   * the level registry. The browser keeps `LOCATION_FILTERS` for its controls
   * and pins it equal to this, so neither can outlive what it names.
   */
  location_filters: string[];
  /**
   * The levels a demarcation point may be coloured by — the organisational
   * chain, derived server-side. A customer contains nothing, so colouring by
   * one would give every point its own colour and mean nothing.
   */
  color_by_levels: string[];
  defaults: {
    metric: string;
    color_metric: string;
    size_metric: string;
    tooltip_fields: string[];
  };
  style: MapStyle;
  coverage: MapCoverage[];
}

/**
 * Which map a design composes.
 *
 * The two tabs are different maps, not two views of one: `analysis` draws
 * figures over a period inside the reader's scope, `demarcation` draws
 * coordinates and nothing else. A design belongs to exactly one, and each has
 * its own default so both tabs always have something to open on.
 */
export type MapPurpose = 'analysis' | 'demarcation';

export type MapColorMode = 'bands' | 'diverging' | 'sequential';

/**
 * One layer of a design, with every inherited value resolved beside the
 * stored one: `metric` is what the layer says (null = inherit) and
 * `effective_metric` is what it draws.
 */
export interface MapLayerConfig {
  layer_id: number;
  layer_name: string;
  point_level: string;
  level_label: string;
  parent_level: string | null;
  view_mode: MapViewMode;
  view_modes: MapViewMode[];
  boundary_available: boolean;
  metric: string | null;
  effective_metric: string;
  color_metric: string | null;
  effective_color_metric: string;
  color_mode: MapColorMode;
  size_metric: string | null;
  effective_size_metric: string;
  is_visible: boolean;
  display_order: number;
  min_zoom: number;
  cluster_at: number | null;
  label_field: string;
  show_label: boolean;
  label_min_zoom: number;
  tooltip_fields: string[];
  tooltip_inherited: boolean;
  style_config: Record<string, unknown> | null;
  style: MapStyle;
}

export interface MapDesign {
  design_id: number;
  name: string;
  description: string | null;
  /** Fixed at creation and never edited — duplicating is how a design moves. */
  purpose: MapPurpose;
  basemap: string;
  basemap_resolved: MapBasemap;
  /** Set when the design names a basemap the deployment no longer configures. */
  basemap_note: string | null;
  default_metric: string;
  is_default: boolean;
  is_active: boolean;
  is_system_default: boolean;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  layer_count: number;
  layers: MapLayerConfig[];
}

export interface MapDesignsResponse {
  purpose: MapPurpose;
  designs: MapDesign[];
  default_design_id: number | null;
}

/**
 * One level's placed coordinates, with no figure attached to any of them.
 *
 * Deliberately not `MapLayerData`: that carries extents, class breaks, rows
 * and a ranking, every one of which needs a metric. What a demarcation layer
 * has instead is provenance — how many of its points are centroids rather
 * than positions somebody stated.
 */
export interface MapLocationProperties {
  code: string;
  name: string;
  level: string;
  parent_level: string | null;
  parent_code: string | null;
  source: string;
  precision: string;
  derived_from: number | null;
  /**
   * The ancestor this point is coloured by, at the level the reader chose.
   *
   * Absent when nothing is being coloured; `null` when the point has no
   * ancestor at that level — a zone under "colour by region", or an entity
   * whose parent code names nothing in the chain. The two are different and
   * the renderer treats them differently: absent takes the layer's own icon,
   * `null` is a value the `match` fails to hit and so takes the fallback.
   */
  group_code?: string | null;
}

export interface MapLocationFeature {
  type: 'Feature';
  id: string;
  geometry: { type: 'Point'; coordinates: [number, number] };
  properties: MapLocationProperties;
}

export interface MapLocationCollection {
  type: 'FeatureCollection';
  features: MapLocationFeature[];
}

/** One parent, its colour, and how many drawn points belong to it. */
export interface MapColorGroup {
  code: string;
  name: string;
  color: string;
  count: number;
}

/**
 * How the drawn points were coloured, and by what.
 *
 * The colours are **assigned by the server** in both modes — this is a list to
 * render, never a palette to apply an ordering rule to. That is what keeps the
 * legend's swatch and the dot on the map from ever disagreeing.
 *
 * `categorical` gives every group its own colour. `focus` is what a level with
 * more groups than the palette can keep apart gets instead: one group picked
 * out against a neutral ground, and neutral everywhere until the reader picks,
 * because a focus nobody asked for is a filter nobody applied. Nothing cycles a
 * palette — two neighbours sharing a colour on this map is a wrong answer about
 * where a boundary falls, not an untidy one.
 */
export interface MapColorBy {
  level: string;
  label: string;
  mode: 'categorical' | 'focus';
  groups: MapColorGroup[];
  /** The picked group in `focus` mode; `null` until the reader chooses. */
  focus: string | null;
  /** Drawn points with no ancestor at this level, drawn neutral. */
  ungrouped: number;
  note: string | null;
}

export interface MapLocationLayer {
  level: string;
  label: string;
  features: MapLocationCollection;
  /**
   * Records the master holds at this level, placed or not — inside the
   * caller's scope, like every count on this layer.
   */
  total: number;
  placed: number;
  /**
   * Coordinates at this level inside the caller's scope, before their own
   * filter narrowed them.
   *
   * The denominator that makes `placed` legible: "9 points" and "9 of 94" are
   * different findings, and only the second tells a narrow filter apart from a
   * level nobody has surveyed. **Scoped**, because a denominator is a figure
   * too — printing the national 94 beside a regional manager's nine points
   * would hand them a total they may not see under a label calling it theirs.
   */
  available: number;
  /**
   * Centroids at this level — **counted, and not drawn**.
   *
   * A `DERIVED` row is the average of the coordinates below it, so it marks a
   * spot nobody surveyed. No such feature is sent, so this is the only trace
   * of them the browser gets; `placed + derived + missing === total`.
   */
  derived: number;
  /** Records with no coordinate. Never records a filter excluded. */
  missing: number;
  bounds: MapBounds | null;
  notes: string[];
  layer: MapLayerConfig;
}

export interface MapLocationsResponse {
  design: MapDesign;
  levels: string[];
  /** The filters that narrowed it, echoed back so a chip cannot lie. */
  filters: Record<string, unknown>;
  /** How the reader's own data scope bounded it, in words; null when full. */
  scope_note: string | null;
  empty: boolean;
  layers: MapLocationLayer[];
  /** `null` until a reader chooses a level to colour by. */
  color_by: MapColorBy | null;
}

/** A layer as the editor sends it. Order in the list is display order. */
export interface MapLayerInput {
  point_level: string;
  layer_name?: string | null;
  view_mode?: MapViewMode;
  metric?: string | null;
  color_metric?: string | null;
  size_metric?: string | null;
  is_visible?: boolean;
  min_zoom?: number;
  cluster_at?: number | null;
  label_field?: string;
  show_label?: boolean;
  label_min_zoom?: number;
  tooltip_fields?: string[] | null;
  style_config?: Record<string, unknown> | null;
}

export interface MapDesignInput {
  name: string;
  description?: string | null;
  basemap?: string;
  default_metric?: string;
  /**
   * Which map the design composes. Fixed at creation and never editable —
   * `MapDesignUpdate` deliberately has no counterpart, because moving a design
   * between the two maps would change what its settings *mean*.
   *
   * Sent because the settings drawer is now on both tabs, and the server
   * defaults it to `analysis`: a design created from the Area Demarcation
   * drawer without it would be saved to the other map and vanish from the list
   * the composer was looking at.
   */
  purpose?: MapPurpose;
  layers: MapLayerInput[];
}

/** Only the fields sent are changed; `null` clears, absence leaves alone. */
export interface MapDesignUpdate {
  name?: string;
  description?: string | null;
  basemap?: string;
  default_metric?: string;
  layers?: MapLayerInput[];
}

/** The figures every map row carries. `null` is absent, never zero. */
export interface MapMeasures {
  net_sales: number | null;
  quantity: number | null;
  volume: number | null;
  customer_count: number | null;
  target_amount: number | null;
  target_quantity: number | null;
  target_volume: number | null;
  achievement_percent: number | null;
  shortfall: number | null;
  previous_net_sales: number | null;
  growth_percent: number | null;
}

export interface MapFeatureProperties extends MapMeasures {
  code: string;
  name: string;
  level: string;
  parent_level: string | null;
  parent_code: string | null;
  location_source: string;
  location_precision: string;
  derived_from: number | null;
}

export interface MapFeature {
  type: 'Feature';
  id: string;
  /** GeoJSON: longitude first. */
  geometry: { type: 'Point'; coordinates: [number, number] };
  properties: MapFeatureProperties;
}

export interface MapFeatureCollection {
  type: 'FeatureCollection';
  features: MapFeature[];
}

export interface MapRankingRow extends MapMeasures {
  code: string;
  name: string;
  placed: boolean;
  parent_code: string | null;
}

export interface MapRanking {
  metric: string;
  field: string;
  top: MapRankingRow[];
  bottom: MapRankingRow[];
  ranked_count: number;
  unranked_count: number;
}

export interface MapBounds {
  north: number;
  south: number;
  east: number;
  west: number;
  centre: { latitude: number; longitude: number };
}

/** One drawn layer: its points, what could not be drawn, and its legend inputs. */
export interface MapLayerData {
  level: string;
  label: string;
  group_by: string;
  entity_count: number;
  placed_count: number;
  features: MapFeatureCollection;
  /** Entities with data in the period and no coordinate — reported, never hidden. */
  unplaced: { code: string; label: string }[];
  unassigned: Record<string, unknown> | null;
  notes: string[];
  bounds: MapBounds | null;
  extents: Record<string, { min: number; max: number }>;
  breaks: Record<string, number[]>;
  layer: MapLayerConfig;
  ranking: MapRanking;
}

export interface MapDataResponse {
  period: DateRange;
  filters: Record<string, unknown>;
  design: MapDesign;
  metric: string;
  levels: string[];
  empty: boolean;
  layers: MapLayerData[];
}

export interface MapEntityAncestor {
  level: string;
  label: string;
  code: string;
  name: string;
  /** False when the parent code names a record the master lacks. */
  known: boolean;
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
  updated_at: string | null;
}

/** The selected-entity card: a name, the chain above it, and where it is. */
export interface MapEntity {
  level: string;
  label: string;
  code: string;
  name: string;
  ancestors: MapEntityAncestor[];
  location: MapEntityLocation | null;
}
