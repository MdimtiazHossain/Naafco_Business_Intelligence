/**
 * The service layer: one module per domain, all built on `apiClient`.
 *
 * Components import from here and never touch `fetch`, so authentication,
 * error mapping and query building stay in one place.
 */

import {
  request,
  requestBlob,
  requestForm,
  requestFormWithProgress,
  tokenStore,
} from './apiClient';
import type {
  AdminLevel,
  AdminSummary,
  AlertsResponse,
  AncestorsResponse,
  AreaCoverageRow,
  AreaResponse,
  AreaStyle,
  AuditLogEntry,
  BatchQuality,
  BulkResponse,
  ChatHistoryMessage,
  ChatMessageResponse,
  Conversation,
  AliasKind,
  FeedbackRating,
  FeedbackResponse,
  LearningExample,
  LearningOptions,
  LearningSignal,
  TermAlias,
  CreditControlSummary,
  CreditCustomerPage,
  CreditInvoiceDetail,
  CreditInvoicePage,
  CustomersPage,
  DashboardResponse,
  DataCatalogue,
  Dependants,
  DesignerOptions,
  EtlBatch,
  HistoryEntry,
  ExportRequest,
  GlobalFilters,
  LevelsResponse,
  LoginResponse,
  MapConfig,
  MapCoverageRow,
  MapEntitiesResponse,
  MapEntityTrendResponse,
  MapPointsResponse,
  MapEntityLocation,
  MarkerAsset,
  MarkerAssignmentRow,
  MarkerDesign,
  MarkerLegendEntry,
  MarkerPreview,
  MarkerVersion,
  MaterialsPage,
  NotificationsResponse,
  OptionsResponse,
  PerformancePage,
  PeriodOptionsResponse,
  RecordDetailResponse,
  RecordListResponse,
  ResolvedMarkerConfig,
  Role,
  RoleSummary,
  SalesPage,
  SearchResult,
  Section,
  SectionAccess,
  SectionPermission,
  StockPage,
  TargetPage,
  TargetAvailableMaterial,
  TargetCountryLine,
  TargetCountryTargetResponse,
  TargetAdjustment,
  TargetAllocationJob,
  TargetAllocationRun,
  TargetAllocationState,
  TargetCountryTotals,
  TargetFactorCatalogue,
  TargetReadiness,
  TargetApprovalOutcome,
  TargetAuditResponse,
  TargetComparisonOption,
  TargetComparisonResponse,
  TargetDashboardResponse,
  TargetUploadPreview,
  TargetUploadResult,
  TargetApprovalQueue,
  TargetApprovalState,
  TargetMatrixResponse,
  TargetLockResult,
  TargetLockState,
  TargetMatrixRow,
  TargetRevision,
  TargetRevisionsResponse,
  TargetReviewResponse,
  TargetHistoryResponse,
  TargetManagementOptions,
  TargetPeriod,
  TargetPlan,
  TargetPlanDeletable,
  TargetPlanDeleted,
  TargetPlanDeletable,
  TargetPlanDeleted,
  TargetPlanStatus,
  TargetVersion,
  TransactionsResponse,
  UploadBatch,
  UploadCatalogue,
  UploadIssue,
  UploadJobProgress,
  UploadJobsResponse,
  UploadOutcome,
  UploadQueued,
  UploadSummary,
  UploadType,
  User,
  UserStatus,
  WhatsAppStatus,
  WriteResponse,
} from '../types/api';

/** Filters plus a period, as every report endpoint expects them. */
export interface ReportQuery extends GlobalFilters {
  period?: string;
  date_from?: string;
  date_to?: string;
}

export const authService = {
  login: (username: string, password: string, remember: boolean) =>
    request<LoginResponse>('/api/auth/login', {
      method: 'POST',
      body: { username, password, remember },
      anonymous: true,
    }),
  me: () => request<User>('/api/auth/me'),
  logout: () => request<{ status: string }>('/api/auth/logout', { method: 'POST' }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ status: string }>('/api/auth/password', {
      method: 'POST',
      body: { current_password, new_password },
    }),
  updatePreferences: (preferences: { preferred_language?: string; theme?: string }) =>
    request<User>('/api/auth/preferences', { method: 'PATCH', body: preferences }),
  storeToken: tokenStore.set,
  clearToken: tokenStore.clear,
  hasToken: () => tokenStore.get() !== null,
};

export const dashboardService = {
  get: (query: ReportQuery) => request<DashboardResponse>('/api/dashboard', { params: query }),
  periodOptions: () => request<PeriodOptionsResponse>('/api/period-options'),
};

export const salesService = {
  page: (query: ReportQuery) => request<SalesPage>('/api/pages/sales', { params: query }),
};


export const stockService = {
  /**
   * `expiring_within_days` overrides the server's configured "expiring soon"
   * horizon for this request. The date range in `query` is sent because the
   * shared filter bar always supplies one, and is ignored by the backend: a
   * material stock position carries no posting date.
   */
  page: (query: ReportQuery & { expiring_within_days?: number }) =>
    request<StockPage>('/api/pages/stock', { params: query }),
};

export const targetService = {
  page: (query: ReportQuery & { below_percent?: number }) =>
    request<TargetPage>('/api/pages/target', { params: query }),
};

/**
 * Credit Control: what is owed, how late it is, and against which invoices.
 *
 * Four reads rather than one page bundle plus client-side slicing, because the
 * two tables are server-paged: a customer with four thousand invoices is not a
 * payload the browser should be asked to hold so it can show twenty-five rows.
 *
 * `as_on_date` travels with every one of them. Overdue is a function of a date,
 * and the derived columns — status, aging bucket, days overdue — are resolved
 * against it server-side, so a page that sent it to one endpoint and not another
 * would show a KPI strip and a table that disagreed.
 */
export interface CreditQuery extends ReportQuery {
  as_on_date?: string;
  due_soon_days?: number;
  search?: string;
  sort_by?: string;
  sort_dir?: 'asc' | 'desc';
  page?: number;
  page_size?: number;
}

export const creditService = {
  summary: (query: CreditQuery) =>
    request<CreditControlSummary>('/api/reports/credit-control', { params: query }),
  invoices: (query: CreditQuery) =>
    request<CreditInvoicePage>('/api/reports/credit-control/invoices', {
      params: query,
    }),
  customers: (query: CreditQuery) =>
    request<CreditCustomerPage>('/api/reports/credit-control/customers', {
      params: query,
    }),
  /**
   * Addressed by company *and* invoice number: the number alone is unique only
   * within its company, and two group companies each numbering from 1 is
   * ordinary. Both are encoded — an invoice number is a source-supplied string
   * and may legitimately contain a slash.
   */
  invoice: (companyCode: string, invoiceNo: string, query: CreditQuery) =>
    request<CreditInvoiceDetail>(
      `/api/reports/credit-control/invoices/${encodeURIComponent(companyCode)}`
      + `/${encodeURIComponent(invoiceNo)}`,
      { params: query },
    ),
};

/**
 * Target Management: plans and the versions under them.
 *
 * Its own service rather than more methods on `targetService`, because the two
 * answer different questions from different tables — that one reads achievement
 * out of `fact_target`, this one builds the plan a target is set under.
 */
export const targetManagementService = {
  options: () => request<TargetManagementOptions>('/api/target-management/options'),

  plans: (
    params: {
      financial_year?: string;
      plan_status?: string;
      company_code?: string;
      sales_line_code?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    request<{ plans: TargetPlan[]; total: number }>('/api/target-management/plans', {
      params,
    }),

  plan: (planId: number) =>
    request<{ plan: TargetPlan; versions: TargetVersion[] }>(
      `/api/target-management/plans/${planId}`,
    ),

  createPlan: (body: {
    financial_year: string;
    target_period: TargetPeriod;
    company_code: string;
    bu_code: string;
    sales_line_code: string;
    basis_financial_years?: string | null;
  }) =>
    request<{ plan: TargetPlan }>('/api/target-management/plans', {
      method: 'POST',
      body,
    }),

  versions: (planId: number) =>
    request<{ versions: TargetVersion[] }>(
      `/api/target-management/plans/${planId}/versions`,
    ),

  /** A reason is required: it is kept with the version and shown beside it. */
  createVersion: (planId: number, reason: string) =>
    request<{ version: TargetVersion }>(
      `/api/target-management/plans/${planId}/versions`,
      { method: 'POST', body: { reason } },
    ),

  setVersionStatus: (versionId: number, status: TargetPlanStatus, reason?: string) =>
    request<{ version: TargetVersion }>(
      `/api/target-management/versions/${versionId}/status`,
      { method: 'PATCH', body: { status, reason: reason ?? null } },
    ),

  countryTarget: (versionId: number) =>
    request<TargetCountryTargetResponse>(
      `/api/target-management/versions/${versionId}/country-target`,
    ),

  /** The factor catalogue, derived from the engine's own declaration. */
  allocationFactors: () =>
    request<TargetFactorCatalogue>('/api/target-management/allocation-factors'),

  /**
   * The version's allocation, its reconciliation and its last run.
   *
   * Reconciliation is recomputed by the backend on every read rather than
   * served from the job's saved verdict — the two differ the moment somebody
   * edits a country volume without re-allocating, which is exactly the state a
   * planner needs to see.
   */
  allocation: (versionId: number) =>
    request<TargetAllocationState>(
      `/api/target-management/versions/${versionId}/allocation`,
    ),

  /** Queue a run. Answers 202 with a job to poll — never a finished result. */
  startAllocation: (
    versionId: number,
    body: {
      weights?: Record<string, number>;
      enabled?: Record<string, boolean>;
      management_adjustment?: Record<string, number>;
    } = {},
  ) =>
    request<{ job: TargetAllocationJob }>(
      `/api/target-management/versions/${versionId}/allocation`,
      { method: 'POST', body },
    ),

  /**
   * The pre-flight gate.
   *
   * Read-only and cheap, so it is fetched before the button is pressed rather
   * than after — discovering that no customer carries a sub-territory halfway
   * through a background job is the worst time to discover it.
   */
  readiness: (versionId: number) =>
    request<TargetReadiness>(
      `/api/target-management/versions/${versionId}/readiness`,
    ),

  /** Every run against this version, failures included. */
  allocationRuns: (versionId: number, params: { limit?: number } = {}) =>
    request<{ runs: TargetAllocationRun[]; total: number }>(
      `/api/target-management/versions/${versionId}/allocation-runs`,
      { params },
    ),

  /** The hierarchical review tree, scoped to what this reader may see. */
  review: (versionId: number, materialCode?: string | null) =>
    request<TargetReviewResponse>(
      `/api/target-management/versions/${versionId}/review`,
      { params: materialCode ? { material_code: materialCode } : {} },
    ),

  adjustments: (versionId: number) =>
    request<{ adjustments: TargetAdjustment[] }>(
      `/api/target-management/versions/${versionId}/adjustments`,
    ),

  /**
   * Record a management adjustment for the next run to apply.
   *
   * `adjustment_volume` is absolute and signed — `+500`, `-1200` — never a
   * percentage. It is an input to the engine, so the response says it applies
   * on the next run rather than implying the numbers have already moved.
   */
  setAdjustment: (
    versionId: number,
    body: {
      level: string;
      node_code: string;
      material_code?: string | null;
      adjustment_volume: number;
      reason: string;
    },
  ) =>
    request<{ adjustment: TargetAdjustment; applies_on_next_run: boolean }>(
      `/api/target-management/versions/${versionId}/adjustments`,
      { method: 'PUT', body },
    ),

  removeAdjustment: (versionId: number, adjustmentId: number) =>
    request<{ removed: boolean }>(
      `/api/target-management/versions/${versionId}/adjustments/${adjustmentId}`,
      { method: 'DELETE' },
    ),

  allocationJob: (jobId: string) =>
    request<TargetAllocationJob>(
      `/api/target-management/allocation-jobs/${jobId}`,
    ),

  /** Two financial years of actual sales, and the basis they imply. */
  history: (versionId: number) =>
    request<TargetHistoryResponse>(
      `/api/target-management/versions/${versionId}/history`,
    ),

  /** The configured approval chain, plus the shipped default a reset restores. */
  approvalMatrix: () =>
    request<TargetMatrixResponse>('/api/target-management/approval-matrix'),

  /**
   * Reconfigure the chain.
   *
   * `fields_present` names which optional fields the caller actually set. It is
   * what distinguishes "leave the sequence alone" from "put this role outside
   * the chain" — both travel as `null` over JSON, they mean opposite things,
   * and without the list one silently becomes the other.
   */
  updateApprovalMatrix: (
    entries: Array<Partial<TargetMatrixRow> & { role: string; reason?: string;
      fields_present: string[] }>,
  ) =>
    request<{ chain: TargetMatrixRow[] }>(
      '/api/target-management/approval-matrix',
      { method: 'PUT', body: { entries } },
    ),

  revisions: (versionId: number, openOnly = false) =>
    request<TargetRevisionsResponse>(
      `/api/target-management/versions/${versionId}/revisions`,
      { params: openOnly ? { open_only: true } : {} },
    ),

  /**
   * Ask for one node's figure to be changed.
   *
   * `requested_volume` is sent as the **raw string the user typed**. The
   * backend decides what it means, so a value with a letter O in place of a
   * zero is refused by name rather than silently coerced into a smaller number
   * by JSON parsing.
   */
  createRevision: (
    versionId: number,
    body: {
      level: string;
      node_code: string;
      material_code?: string | null;
      requested_volume: string;
      reason: string;
    },
  ) =>
    request<{ revision: TargetRevision }>(
      `/api/target-management/versions/${versionId}/revisions`,
      { method: 'POST', body },
    ),

  decideRevision: (
    versionId: number,
    revisionId: number,
    body: { approve: boolean; approved_volume?: string | null; comment?: string },
  ) =>
    request<{ revision: TargetRevision; open_count: number }>(
      `/api/target-management/versions/${versionId}/revisions/${revisionId}/decision`,
      { method: 'POST', body },
    ),

  approvalState: (versionId: number) =>
    request<TargetApprovalState>(
      `/api/target-management/versions/${versionId}/approval`,
    ),

  submitForApproval: (versionId: number, comment?: string) =>
    request<TargetApprovalState>(
      `/api/target-management/versions/${versionId}/submit`,
      { method: 'POST', body: { comment: comment ?? null } },
    ),

  approveVersion: (versionId: number, comment?: string) =>
    request<TargetApprovalOutcome>(
      `/api/target-management/versions/${versionId}/approve`,
      { method: 'POST', body: { comment: comment ?? null } },
    ),

  /** Rejecting requires a reason; approving does not. */
  rejectVersion: (versionId: number, comment: string) =>
    request<TargetApprovalOutcome>(
      `/api/target-management/versions/${versionId}/reject`,
      { method: 'POST', body: { comment } },
    ),

  /**
   * Return an approved version to review before it is locked.
   *
   * Distinct from rejecting: the version goes back to the chain rather than
   * back to its author.
   */
  sendVersionBack: (versionId: number, comment: string) =>
    request<TargetApprovalOutcome>(
      `/api/target-management/versions/${versionId}/send-back`,
      { method: 'POST', body: { comment } },
    ),

  myApprovals: () =>
    request<TargetApprovalQueue>('/api/target-management/my-approvals'),

  lockState: (versionId: number) =>
    request<TargetLockState>(
      `/api/target-management/versions/${versionId}/lock`,
    ),

  /**
   * Write the agreed allocation into `fact_target`.
   *
   * The response says what was written — inserted, updated and voided — rather
   * than a bare success, because "the target is locked" and "1,872 rows now
   * stand" are different amounts of reassurance.
   */
  lockVersion: (versionId: number, comment?: string) =>
    request<TargetLockResult>(
      `/api/target-management/versions/${versionId}/lock`,
      { method: 'POST', body: { comment: comment ?? null } },
    ),

  auditTrail: (params: {
    plan_id?: number;
    version_id?: number;
    action?: string;
    actor?: string;
    limit?: number;
    offset?: number;
  } = {}) =>
    request<TargetAuditResponse>('/api/target-management/audit', { params }),

  compareOptions: (planId: number) =>
    request<{ plan: TargetPlan; versions: TargetComparisonOption[] }>(
      `/api/target-management/plans/${planId}/compare-options`,
    ),

  /**
   * What moved between two versions of one plan.
   *
   * Same plan only — two plans have different scopes, materials and
   * hierarchies, so a difference between them would be two unrelated targets
   * subtracted from each other. The backend refuses it by name.
   */
  compareVersions: (
    versionId: number,
    baseVersionId: number,
    materialCode?: string | null,
  ) =>
    request<TargetComparisonResponse>(
      `/api/target-management/versions/${versionId}/compare`,
      {
        params: {
          base_version_id: baseVersionId,
          ...(materialCode ? { material_code: materialCode } : {}),
        },
      },
    ),

  dashboard: (financialYear?: string | null) =>
    request<TargetDashboardResponse>('/api/target-management/dashboard', {
      params: financialYear ? { financial_year: financialYear } : {},
    }),

  /** A CSV of this plan's materials, pre-filled with the version's figures. */
  countryTargetTemplate: (versionId: number) =>
    saveAs(
      `/api/target-management/versions/${versionId}/country-target/template`,
    ),

  /**
   * Read an uploaded file and report what applying it would do. Writes nothing.
   *
   * The file is sent as multipart and staged server-side; the token that comes
   * back is what `applyCountryTarget` takes. The bytes are re-read and
   * re-validated there rather than trusted from here.
   */
  previewCountryTargetUpload: (
    versionId: number,
    file: File,
    sheetName?: string,
  ) => {
    const form = new FormData();
    form.append('file', file);
    if (sheetName) form.append('sheet_name', sheetName);
    return requestForm<TargetUploadPreview>(
      `/api/target-management/versions/${versionId}/country-target/preview`,
      form,
    );
  },

  applyCountryTargetUpload: (
    versionId: number,
    uploadToken: string,
    sheetName?: string,
  ) =>
    request<TargetUploadResult>(
      `/api/target-management/versions/${versionId}/country-target/apply`,
      { method: 'POST', body: { upload_token: uploadToken, sheet_name: sheetName ?? null } },
    ),

  planDeletable: (planId: number) =>
    request<TargetPlanDeletable>(
      `/api/target-management/plans/${planId}/deletable`,
    ),

  /**
   * Remove a draft plan that was never allocated, approved or locked.
   *
   * A typed country target does not block it — figures entered and never
   * allocated are a draft target, not a record. Everything the plan
   * actually became is refused by the backend, by name.
   */
  deletePlan: (planId: number) =>
    request<TargetPlanDeleted>(
      `/api/target-management/plans/${planId}`,
      { method: 'DELETE' },
    ),

  planDeletable: (planId: number) =>
    request<TargetPlanDeletable>(
      `/api/target-management/plans/${planId}/deletable`,
    ),

  /**
   * Remove a draft plan that was never allocated, approved or locked.
   *
   * A typed country target does not block it — figures entered and never
   * allocated are a draft target, not a record. Everything the plan
   * actually became is refused by the backend, by name.
   */
  deletePlan: (planId: number) =>
    request<TargetPlanDeleted>(
      `/api/target-management/plans/${planId}`,
      { method: 'DELETE' },
    ),

  availableMaterials: (versionId: number) =>
    request<{ materials: TargetAvailableMaterial[] }>(
      `/api/target-management/versions/${versionId}/available-materials`,
    ),

  /**
   * Volumes go up as **strings**, deliberately.
   *
   * `Number('12,5OO')` is `NaN` and `parseFloat` would read it as `12` — either
   * way the browser would have decided what an unreadable cell means. Sending
   * the raw text lets the backend refuse it by name, which is the same answer a
   * bulk upload of the same value gets.
   */
  setCountryTarget: (
    versionId: number,
    lines: { material_code: string; target_volume: string }[],
  ) =>
    request<{
      written: { created: number; updated: number };
      lines: TargetCountryLine[];
      totals: TargetCountryTotals;
      notes: string[];
      editable: boolean;
    }>(`/api/target-management/versions/${versionId}/country-target`, {
      method: 'PUT',
      body: { lines },
    }),
};

export const performanceService = {
  page: (query: ReportQuery & { level: string; limit?: number }) =>
    request<PerformancePage>('/api/pages/performance', { params: query }),
};

export const materialService = {
  page: (query: ReportQuery & { level?: string; limit?: number }) =>
    request<MaterialsPage>('/api/pages/materials', { params: query }),
};

export const customerService = {
  page: (query: ReportQuery & { limit?: number }) =>
    request<CustomersPage>('/api/pages/customers', { params: query }),
};

export const transactionService = {
  list: (
    dataType: string,
    query: ReportQuery & {
      page?: number;
      page_size?: number;
      search?: string;
      sort_by?: string;
      sort_dir?: 'asc' | 'desc';
    },
  ) => request<TransactionsResponse>(`/api/pages/transactions/${dataType}`, { params: query }),
};

export const masterDataService = {
  levels: () => request<LevelsResponse>('/api/master-data/levels'),
  options: (level: string, parentCode?: string, search?: string,
            parentLevel?: string) =>
    request<OptionsResponse>(`/api/master-data/options/${level}`, {
      params: { parent_code: parentCode, parent_level: parentLevel, search },
    }),
  /**
   * The parent levels one selection implies — the whole chain in one request.
   *
   * Selecting a customer implies nine levels above it. Resolving them here in
   * a single call is what lets the bar set them all at once and issue one
   * report query, instead of walking the hierarchy a level at a time.
   */
  /**
   * `code` may be one value or several; `source` names the page's transaction
   * data where the page has one, which is how a material reaches a plant.
   */
  ancestors: (level: string, code: string | string[], source?: string) =>
    request<AncestorsResponse>(`/api/master-data/ancestors/${level}`, {
      params: { code, source },
    }),
  search: (q: string, signal?: AbortSignal) =>
    request<{ query: string; count: number; results: SearchResult[] }>(
      '/api/master-data/search',
      { params: { q }, signal },
    ),
};

export const chatService = {
  /**
   * Ask a question, with what the reader has selected on screen.
   *
   * `context` is the same filter object every report page sends, under the same
   * names, so the assistant narrows by the bar exactly as a report does. The
   * server treats it as filters and nothing more: it can make an answer
   * narrower, never wider, and it passes the same permission check the
   * question's own entities do.
   */
  send: (message: string, conversationId?: string,
         context?: Record<string, string | undefined>) =>
    request<ChatMessageResponse>('/api/chat', {
      method: 'POST',
      body: {
        message,
        conversation_id: conversationId ?? null,
        context: context ?? null,
      },
    }),
  conversations: () =>
    request<{ conversations: Conversation[] }>('/api/chat/conversations'),
  history: (conversationId: string) =>
    request<{ conversation_id: string; messages: ChatHistoryMessage[] }>(
      `/api/chat/conversations/${conversationId}`,
    ),
  capabilities: () =>
    request<{
      user: { username: string; role: Role; scope_description: string };
      languages: string[];
      tools: { name: string; description: string }[];
      examples: string[];
    }>('/api/chat/capabilities'),
  /**
   * Rate one answer, optionally saying what was expected instead.
   *
   * `expected` is free text and the backend sanitises it; `sanitized` comes
   * back true when it had to, so the reader is told their words were edited
   * rather than being shown one thing and having another stored.
   */
  feedback: (messageId: number, rating: FeedbackRating, expected?: string) =>
    request<FeedbackResponse>('/api/chat/feedback', {
      method: 'POST',
      body: {
        message_id: messageId,
        rating,
        ...(expected ? { expected } : {}),
      },
    }),
};

/**
 * The agent-learning review queue and the vocabulary approved from it.
 *
 * Every write here is a deliberate act by a reviewer: `propose*` writes an
 * inert row and `approve*` is a separate call, because the two halves are meant
 * to be done by different people at different times.
 */
export const learningService = {
  options: () => request<LearningOptions>('/api/learning/options'),

  signals: (params: { signal_status?: string; signal_type?: string; limit?: number } = {}) =>
    request<{ signals: LearningSignal[]; total: number }>('/api/learning/signals', {
      params,
    }),
  setSignalStatus: (signalId: number, status: string) =>
    request<LearningSignal>(`/api/learning/signals/${signalId}`, {
      method: 'PATCH',
      body: { status },
    }),

  aliases: (params: { alias_status?: string; alias_kind?: string } = {}) =>
    request<{ aliases: TermAlias[]; total: number }>('/api/learning/aliases', {
      params,
    }),
  proposeAlias: (body: {
    phrase: string;
    alias_kind: AliasKind;
    entity_type?: string | null;
    entity_code?: string | null;
    target_keyword?: string | null;
    language?: string | null;
    signal_id?: number | null;
    notes?: string | null;
  }) => request<TermAlias>('/api/learning/aliases', { method: 'POST', body }),
  approveAlias: (aliasId: number, replace = false) =>
    request<TermAlias>(`/api/learning/aliases/${aliasId}/approve`, {
      method: 'POST',
      body: { replace },
    }),
  rejectAlias: (aliasId: number, reason?: string) =>
    request<TermAlias>(`/api/learning/aliases/${aliasId}/reject`, {
      method: 'POST',
      body: { reason: reason ?? null },
    }),
  retireAlias: (aliasId: number, reason?: string) =>
    request<TermAlias>(`/api/learning/aliases/${aliasId}/retire`, {
      method: 'POST',
      body: { reason: reason ?? null },
    }),

  proposeExample: (body: {
    question: string;
    tool_name: string;
    intent?: string | null;
    language?: string | null;
    notes?: string | null;
  }) => request<LearningExample>('/api/learning/examples', { method: 'POST', body }),

  examples: (params: { example_status?: string } = {}) =>
    request<{ examples: LearningExample[]; total: number }>('/api/learning/examples', {
      params,
    }),
  approveExample: (exampleId: number, replace = false) =>
    request<LearningExample>(`/api/learning/examples/${exampleId}/approve`, {
      method: 'POST',
      body: { replace },
    }),
  rejectExample: (exampleId: number, reason?: string) =>
    request<LearningExample>(`/api/learning/examples/${exampleId}/reject`, {
      method: 'POST',
      body: { reason: reason ?? null },
    }),
  retireExample: (exampleId: number, reason?: string) =>
    request<LearningExample>(`/api/learning/examples/${exampleId}/retire`, {
      method: 'POST',
      body: { reason: reason ?? null },
    }),
};

export const alertService = {
  list: (query: ReportQuery & { severity?: string; category?: string }) =>
    request<AlertsResponse>('/api/alerts', { params: query }),
  notifications: (unreadOnly = false) =>
    request<NotificationsResponse>('/api/notifications', {
      params: { unread_only: unreadOnly },
    }),
  markRead: (id: number) =>
    request<{ status: string }>(`/api/notifications/${id}/read`, { method: 'POST' }),
  markAllRead: () =>
    request<{ updated: number }>('/api/notifications/read-all', { method: 'POST' }),
};

export const dataQualityService = {
  batches: (params: { data_type?: string; limit?: number; offset?: number } = {}) =>
    request<{ total: number; batches: EtlBatch[] }>('/api/etl/batches', { params }),
  batchQuality: (batchId: number, includeRecords = false) =>
    request<BatchQuality>(`/api/data-quality/${batchId}`, {
      params: { include_records: includeRecords },
    }),
  overview: (dataType?: string) =>
    request<{
      batches: number;
      totals: Record<string, number>;
      by_category: Record<string, number>;
    }>('/api/data-quality', { params: { data_type: dataType } }),
};

export const adminService = {
  users: (
    params: {
      search?: string;
      role?: string;
      status?: string;
      sort_by?: string;
      sort_dir?: 'asc' | 'desc';
      limit?: number;
      offset?: number;
    } = {},
  ) => request<{ total: number; users: User[] }>('/api/admin/users', { params }),
  user: (userId: number) => request<User>(`/api/admin/users/${userId}`),
  roles: () =>
    request<{ roles: RoleSummary[]; scope_levels: string[]; statuses: UserStatus[] }>(
      '/api/admin/roles',
    ),
  sections: () =>
    request<{ sections: Section[]; access_values: SectionAccess[] }>(
      '/api/admin/sections',
    ),
  summary: () => request<AdminSummary>('/api/admin/summary'),
  createUser: (body: Record<string, unknown>) =>
    request<User>('/api/admin/users', { method: 'POST', body }),
  updateUser: (userId: number, body: Record<string, unknown>) =>
    request<User>(`/api/admin/users/${userId}`, { method: 'PATCH', body }),
  userPermissions: (userId: number) =>
    request<{ user: User; sections: SectionPermission[]; access_values: SectionAccess[] }>(
      `/api/admin/users/${userId}/permissions`,
    ),
  setUserPermissions: (userId: number, permissions: Record<string, SectionAccess>) =>
    request<{ user: User; sections: SectionPermission[] }>(
      `/api/admin/users/${userId}/permissions`,
      { method: 'PUT', body: { permissions } },
    ),
  rolePermissions: (role: string) =>
    request<{
      role: Role;
      is_admin: boolean;
      sections: (Section & {
        access: SectionAccess;
        explicit: SectionAccess | null;
        catalogue_default: SectionAccess;
        locked: boolean;
      })[];
    }>(`/api/admin/roles/${role}/permissions`),
  setRolePermissions: (role: string, permissions: Record<string, SectionAccess>) =>
    request(`/api/admin/roles/${role}/permissions`, {
      method: 'PUT',
      body: { permissions },
    }),
  auditLogs: (params: { action?: string; username?: string; limit?: number } = {}) =>
    request<{ total: number; actions: string[]; logs: AuditLogEntry[] }>(
      '/api/admin/audit-logs',
      { params },
    ),
  whatsappStatus: () => request<WhatsAppStatus>('/api/integrations/whatsapp/status'),
};

export const dataUploadService = {
  types: () => request<UploadCatalogue>('/api/data-upload/types'),
  type: (key: string) => request<UploadType>(`/api/data-upload/types/${key}`),
  summary: () => request<UploadSummary>('/api/data-upload/summary'),

  /** Ask the backend for a template and hand the file to the browser. */
  async downloadTemplate(key: string, format: 'xlsx' | 'csv' = 'xlsx'): Promise<void> {
    await saveAs(`/api/data-upload/types/${key}/template`, { params: { format } });
  },

  /**
   * Upload and validate. Nothing is written to the warehouse by this call.
   *
   * Answers **202** the moment the bytes are staged: the reading, validation and
   * master mapping run on a background worker. The caller gets an acknowledgement,
   * not a result — use `awaitOutcome` to turn it into one.
   */
  preview: (
    file: File,
    body: {
      upload_type: string;
      import_mode: string;
      date_format?: string;
      /**
       * Ignored by the server, which has no `job_id` parameter on this endpoint
       * and mints its own `upload_uuid` instead. Kept only because the field is
       * harmless and callers still pass it; the id to watch and to cancel with
       * is the `upload.job_id` on the 202 response, not this one.
       */
      job_id?: string;
    },
    options: { onUploadProgress?: (percent: number) => void } = {},
  ) => {
    const form = new FormData();
    form.append('file', file);
    form.append('upload_type', body.upload_type);
    form.append('import_mode', body.import_mode);
    if (body.date_format) form.append('date_format', body.date_format);
    if (body.job_id) form.append('job_id', body.job_id);
    return requestFormWithProgress<UploadQueued>('/api/data-upload/preview', form, {
      onProgress: options.onUploadProgress,
    });
  },

  /** Queue a validated upload for import. Also **202**; see `awaitOutcome`. */
  commit: (uploadId: number, jobId?: string) =>
    request<UploadQueued>(`/api/data-upload/${uploadId}/commit`, {
      method: 'POST',
      params: { confirm: true, ...(jobId ? { job_id: jobId } : {}) },
    }),

  /**
   * Wait for a queued job to settle, then return the batch's full result.
   *
   * A run is still in flight only while its status is one of the three the
   * server calls active — QUEUED, VALIDATING, IMPORTING. Anything else has
   * settled, which deliberately includes `VALIDATED`: that is where a validation
   * job *ends*, and it is neither active nor terminal because the batch is then
   * resting until someone confirms the import.
   *
   * The batch status is the authority rather than the job's own `done` flag,
   * because a finished job is evicted from the server's in-memory registry after
   * a short while — at which point `done` and `phase` stop being reported and
   * only the stored batch row can still say how the run ended.
   */
  async awaitOutcome(
    queued: UploadQueued,
    options: { signal?: AbortSignal; intervalMs?: number } = {},
  ): Promise<UploadOutcome> {
    const { signal, intervalMs = 700 } = options;
    const jobId = queued.upload.job_id ?? queued.upload.upload_uuid;
    const running = new Set(['QUEUED', 'VALIDATING', 'IMPORTING']);

    let status = queued.upload.status;
    while (running.has(status)) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
      let job: UploadJobProgress;
      try {
        job = await dataUploadService.jobProgress(jobId);
      } catch {
        // A dropped poll is not a failed upload; the next tick tries again.
        continue;
      }
      // No such job means the server has forgotten it entirely. Fall through to
      // the batch read, which is the only thing left that can answer.
      if (!job.known) break;
      status = job.status ?? status;
    }
    return dataUploadService.outcome(queued.upload.upload_id);
  },

  /**
   * One finished batch, in the shape the wizard renders.
   *
   * `GET /history/{id}` is the endpoint that has the counts, the preview rows,
   * the warnings and the errors — everything the old synchronous upload response
   * used to carry. The two names it spells differently are mapped here so the
   * component keeps its single `UploadOutcome` contract.
   */
  async outcome(uploadId: number): Promise<UploadOutcome> {
    const detail = await dataUploadService.batch(uploadId);
    const errors = detail.errors ?? [];
    return {
      upload: detail.upload,
      warnings: detail.warnings ?? [],
      errors,
      error_count: detail.error_total ?? errors.length,
      error_truncated: errors.length < (detail.error_total ?? errors.length),
      preview: detail.preview ?? undefined,
    };
  },

  /**
   * How far a run has got. Safe to call at any point and cheap by design — it
   * reads the server's in-memory job registry and never queries the warehouse.
   */
  jobProgress: (jobId: string) =>
    request<UploadJobProgress>(`/api/data-upload/jobs/${jobId}`),

  /**
   * Every import this user has running, plus the ones that just finished.
   *
   * The call that makes an upload survive navigation. It is keyed on nothing the
   * browser holds — the server scopes it to the caller and reads it from
   * `upload_batches` — so any page can ask "what is running?" and get the truth,
   * including a page that has only just mounted after a refresh.
   */
  activeJobs: () => request<UploadJobsResponse>('/api/data-upload/jobs'),

  /**
   * Ask a running import to stop.
   *
   * Cancellation is cooperative and owned by the server: this raises a flag and
   * the worker stops at its next checkpoint and rolls its transaction back. The
   * run is one transaction, so a cancelled import has written nothing.
   *
   * A **409** means the run had already finished. That is not a failure worth
   * showing as one — the stored outcome stands because it is what actually
   * happened, and the caller simply reads the real result instead.
   */
  cancelJob: (jobId: string) =>
    request<{ outcome: string } & UploadJobProgress>(
      `/api/data-upload/jobs/${jobId}/cancel`,
      { method: 'POST' },
    ),

  history: (
    params: {
      category?: string;
      upload_type?: string;
      status?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    request<{
      total: number;
      statuses: string[];
      categories: string[];
      batches: UploadBatch[];
    }>('/api/data-upload/history', { params }),

  batch: (uploadId: number) =>
    request<{
      upload: UploadBatch;
      upload_type: UploadType;
      error_total: number;
      errors: UploadIssue[];
      // Written by the validation job, so absent on a batch that never got as
      // far as reading the file.
      preview?: UploadOutcome['preview'];
      warnings?: string[];
    }>(`/api/data-upload/history/${uploadId}`),

  failedRecords: (params: { upload_type?: string; limit?: number } = {}) =>
    request<{
      total: number;
      records: (UploadIssue & {
        upload_id: number;
        upload_type: string;
        file_name: string;
        uploaded_by: string | null;
      })[];
    }>('/api/data-upload/failed-records', { params }),

  async downloadErrorReport(uploadId: number): Promise<void> {
    await saveAs(`/api/data-upload/history/${uploadId}/errors`);
  },

  rollback: (uploadId: number) =>
    request<{ upload: UploadBatch; fact_rows_removed: number }>(
      `/api/data-upload/history/${uploadId}/rollback`,
      { method: 'POST', params: { confirm: true } },
    ),
};

/**
 * The map's server calls.
 *
 * Deliberately short. Since the MapLibre rebuild the map's *geometry* comes from
 * static files under `public/geo/` — administrative boundaries change only when
 * somebody imports a new release, so an endpoint for them would answer the same
 * bytes on every page load. What remains here is what only the server can
 * answer: business data under the caller's permissions, and how it should look.
 */
export const mapService = {
  config: () => request<MapConfig>('/api/map/config'),

  /**
   * Every entity under a hierarchy filter, at every level, in one call.
   *
   * The one business-data call the map makes, and the same permission-filtered
   * query the rest of the dashboard reads — which is why a map and a report can
   * never disagree about a number.
   *
   * `layers` controls what is drawn; it never narrows the filter, so the
   * returned `counts` always describe the full scope.
   */
  entities: (
    query: ReportQuery & {
      zone_code?: string;
      region_code?: string;
      area_code?: string;
      unit_code?: string;
      territory_code?: string;
      sub_territory_code?: string;
      layers?: string;
      metric?: string;
      zoom?: number;
      diagnostics?: boolean;
    },
  ) => request<MapEntitiesResponse>('/api/map/entities', { params: query }),

  /**
   * Monthly net sales for one entity the map has drawn.
   *
   * What the detail panel's history bars read. Runs `get_sales_trend` through
   * the same tool path everything else on this page uses, so the months here
   * and the figure in the KPI strip come from one query layer under one set of
   * permissions.
   *
   * Actuals only — `fact_target` records a target month and a financial year
   * rather than a date, and nothing on this path groups it by month, so there
   * is no monthly target to draw behind them.
   */
  entityTrend: (
    query: ReportQuery & { level: string; code: string; months?: number },
  ) => request<MapEntityTrendResponse>('/api/map/entity-trend', { params: query }),

  /**
   * Aggregated business points for one level, from the warehouse.
   *
   * Distinct from `entities`, which lists records so they can be *drawn*: this
   * asks the warehouse to **aggregate** a level and hand back one point per
   * code with its measures. That is what a bubble map reads — a territory's
   * bubble is its territory's sales, summed by the same query the reports use,
   * not a count of the pins that happen to sit inside it.
   *
   * With `metric: 'achievement'` each point also carries `net_sales`,
   * `target_amount` and `achievement_percent`, aggregated server-side, so the
   * browser never divides one business figure by another.
   */
  points: (
    query: ReportQuery & {
      level?: string;
      metric?: string;
      cluster?: boolean;
      zoom?: number;
      limit?: number;
    },
  ) => request<MapPointsResponse>('/api/map/data', { params: query }),

  /**
   * The metric behind each administrative area.
   *
   * Called with `geometry: false` by the map: the polygons are already in the
   * browser from `public/geo/`, and the two are joined on the P-code both carry.
   * Asking for geometry as well is supported and is what a non-browser client
   * gets, but on the map's path it would re-send megabytes that never change.
   */
  areas: (
    query: ReportQuery & {
      level?: string;
      division_code?: string;
      district_code?: string;
      upazila_code?: string;
      territory_code?: string;
      bbox?: string;
      simplify?: number;
      geometry?: boolean;
    },
  ) => request<AreaResponse>('/api/map/areas', { params: query }),

  areaLevels: () =>
    request<{ levels: AdminLevel[]; default_level: string; layer_key: string }>(
      '/api/map/area-levels',
    ),

  areaStyles: () =>
    request<{ styles: Record<string, AreaStyle>; coverage: AreaCoverageRow[] }>(
      '/api/map/area-styles',
    ),

  saveAreaStyle: (level: string, style: Partial<AreaStyle>) =>
    request<AreaStyle>(`/api/map/area-styles/${level}`, {
      method: 'PUT',
      body: style,
    }),

  locations: (entityType?: string) =>
    request<{ locations: MapEntityLocation[]; coverage: MapCoverageRow[] }>(
      '/api/map/locations',
      { params: { entity_type: entityType } },
    ),

  saveLocations: (
    locations: {
      entity_type: string;
      entity_code: string;
      latitude: number;
      longitude: number;
      label?: string;
    }[],
    derive = true,
  ) =>
    request<{ saved: number; derived: Record<string, number>; problems: string[] }>(
      '/api/map/locations',
      { method: 'PUT', body: { locations, derive_parents: derive } },
    ),

  deriveLocations: () =>
    request<{ derived: Record<string, number>; coverage: MapCoverageRow[] }>(
      '/api/map/locations/derive',
      { method: 'POST' },
    ),

  deleteLocation: (entityType: string, entityCode: string) =>
    request<{ deleted: string }>(`/api/map/locations/${entityType}/${entityCode}`, {
      method: 'DELETE',
    }),
};

export const markerService = {
  options: () => request<DesignerOptions>('/api/map/designer-options'),

  list: (
    params: {
      entity_type?: string;
      status?: string;
      search?: string;
      limit?: number;
      offset?: number;
    } = {},
  ) => request<{ total: number; designs: MarkerDesign[] }>('/api/map/marker-designs', { params }),

  get: (designId: number) => request<MarkerDesign>(`/api/map/marker-designs/${designId}`),

  create: (body: Record<string, unknown>) =>
    request<MarkerDesign>('/api/map/marker-designs', { method: 'POST', body }),

  update: (designId: number, body: Record<string, unknown>) =>
    request<MarkerDesign>(`/api/map/marker-designs/${designId}`, { method: 'PUT', body }),

  remove: (designId: number, confirm = false) =>
    request<{ deleted: number; assignments_removed: number }>(
      `/api/map/marker-designs/${designId}`,
      { method: 'DELETE', params: { confirm } },
    ),

  duplicate: (designId: number, name?: string) =>
    request<MarkerDesign>(`/api/map/marker-designs/${designId}/duplicate`, {
      method: 'POST',
      body: { name },
    }),

  assign: (designId: number, entityCode?: string | null, priority = 0) =>
    request<{ assignment_id: number; entity_type: string; design: MarkerDesign }>(
      `/api/map/marker-designs/${designId}/assign`,
      { method: 'POST', body: { entity_code: entityCode ?? null, priority } },
    ),

  activate: (designId: number) =>
    request<MarkerDesign>(`/api/map/marker-designs/${designId}/activate`, {
      method: 'POST',
    }),

  deactivate: (designId: number) =>
    request<MarkerDesign>(`/api/map/marker-designs/${designId}/deactivate`, {
      method: 'POST',
    }),

  versions: (designId: number) =>
    request<{ design_id: number; current_version: number; versions: MarkerVersion[] }>(
      `/api/map/marker-designs/${designId}/versions`,
    ),

  /** Render a definition without saving — drives the live preview. */
  preview: (body: {
    definition: Record<string, unknown>;
    asset_id?: number | null;
    context?: Record<string, string>;
  }) => request<MarkerPreview>('/api/map/marker-designs/preview', { method: 'POST', body }),

  uploadAsset: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return requestForm<MarkerAsset>('/api/map/marker-assets', form);
  },

  assets: () => request<{ assets: MarkerAsset[] }>('/api/map/marker-assets'),

  assignments: () =>
    request<{ assignments: MarkerAssignmentRow[] }>('/api/map/assignments'),

  reset: (entityType: string) =>
    request<{ entity_type: string; assignments_removed: number }>(
      `/api/map/assignments/${entityType}/reset`,
      { method: 'POST', params: { confirm: true } },
    ),

  /** The map's own configuration call — one request for every entity type. */
  config: () =>
    request<{
      generation: number;
      renderer: string;
      markers: Record<string, ResolvedMarkerConfig>;
    }>('/api/map/marker-config'),

  legend: () =>
    request<{ generation: number; entries: MarkerLegendEntry[] }>('/api/map/legend'),

  async exportDesigns(entityType?: string): Promise<void> {
    await saveAs('/api/map/marker-designs-export', {
      params: entityType ? { entity_type: entityType } : {},
    });
  },

  importDesigns: (designs: unknown[], overwrite = false) =>
    request<{ created: number; updated: number; skipped: number; problems: string[] }>(
      '/api/map/marker-designs-import',
      { method: 'POST', body: { designs, overwrite } },
    ),
};

/** Fetch an attachment and trigger the browser's download. */
async function saveAs(path: string, options: { params?: object } = {}): Promise<void> {
  const { blob, filename } = await requestBlob(path, { method: 'GET', ...options });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export const exportService = {
  /** Ask the backend to render a report and hand the file to the browser. */
  async download(body: ExportRequest): Promise<void> {
    const { blob, filename } = await requestBlob('/api/reports/export', {
      method: 'POST',
      body,
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  },
};

export const systemService = {
  health: () =>
    request<{ status: string; version: string; database: string }>('/health', {
      anonymous: true,
    }),
};

// ---------------------------------------------------------------------------
// Data Management
// ---------------------------------------------------------------------------

/** Everything that shapes one page of a managed table. */
export interface RecordQuery extends Partial<ReportQuery> {
  search?: string;
  sort_by?: string;
  sort_dir?: 'asc' | 'desc';
  page?: number;
  page_size?: number;
  include_deleted?: boolean;
  include_voided?: boolean;
  /** Comma-separated codes, for "show selected" and selection exports. */
  codes?: string;
  /**
   * Entity-specific filters, sent as `filter.<field>`.
   *
   * Namespaced because the catalogue is built at runtime: the backend cannot
   * declare a query parameter per field of an entity it discovers at import,
   * and an un-namespaced `status=` would collide with the shared filter bar.
   */
  filters?: Record<string, string>;
}

function recordParams(query: RecordQuery = {}): Record<string, unknown> {
  const { filters, ...rest } = query;
  const params: Record<string, unknown> = { ...rest };
  Object.entries(filters ?? {}).forEach(([name, value]) => {
    if (value) params[`filter.${name}`] = value;
  });
  return params;
}

export const dataManagementService = {
  catalogue: () => request<DataCatalogue>('/api/data-management/catalogue'),
};

export const masterRecordService = {
  list: (entity: string, query: RecordQuery = {}) =>
    request<RecordListResponse>(`/api/master/${entity}`, {
      params: recordParams(query),
    }),
  get: (entity: string, code: string) =>
    request<RecordDetailResponse>(
      `/api/master/${entity}/${encodeURIComponent(code)}`,
    ),
  history: (entity: string, code: string, limit = 50, offset = 0) =>
    request<{ history: HistoryEntry[]; total: number }>(
      `/api/master/${entity}/${encodeURIComponent(code)}/history`,
      { params: { limit, offset } },
    ),
  dependants: (entity: string, code: string) =>
    request<Dependants>(
      `/api/master/${entity}/${encodeURIComponent(code)}/dependants`,
    ),
  create: (entity: string, values: Record<string, unknown>) =>
    request<WriteResponse>(`/api/master/${entity}`, {
      method: 'POST',
      body: { values },
    }),
  update: (
    entity: string,
    code: string,
    values: Record<string, unknown>,
    reason?: string,
  ) =>
    request<WriteResponse>(`/api/master/${entity}/${encodeURIComponent(code)}`, {
      method: 'PUT',
      body: { values, reason: reason || null },
    }),
  setStatus: (entity: string, code: string, status: string) =>
    request<WriteResponse>(
      `/api/master/${entity}/${encodeURIComponent(code)}/status`,
      { method: 'PUT', body: { status } },
    ),
  remove: (entity: string, code: string, reason?: string) =>
    request<WriteResponse>(`/api/master/${entity}/${encodeURIComponent(code)}`, {
      method: 'DELETE',
      params: { reason },
    }),
  restore: (entity: string, code: string) =>
    request<WriteResponse>(
      `/api/master/${entity}/${encodeURIComponent(code)}/restore`,
      { method: 'POST' },
    ),
  bulk: (entity: string, action: string, codes: string[], reason?: string) =>
    request<BulkResponse>(`/api/master/${entity}/bulk`, {
      method: 'POST',
      body: { action, codes, reason: reason || null },
    }),
  export: (entity: string, fmt: 'csv' | 'xlsx', query: RecordQuery = {},
           columns?: string[]) =>
    saveAs(`/api/master/${entity}/export/${fmt}`, {
      params: { ...recordParams(query), columns: columns?.join(',') },
    }),
};

export const transactionRecordService = {
  list: (dataType: string, query: RecordQuery = {}) =>
    request<RecordListResponse>(`/api/transactions/${dataType}`, {
      params: recordParams(query),
    }),
  get: (dataType: string, id: string | number) =>
    request<RecordDetailResponse>(`/api/transactions/${dataType}/${id}`),
  history: (dataType: string, id: string | number, limit = 50, offset = 0) =>
    request<{ history: HistoryEntry[]; total: number }>(
      `/api/transactions/${dataType}/${id}/history`,
      { params: { limit, offset } },
    ),
  update: (
    dataType: string,
    id: string | number,
    values: Record<string, unknown>,
    reason?: string,
  ) =>
    request<WriteResponse>(`/api/transactions/${dataType}/${id}`, {
      method: 'PUT',
      body: { values, reason: reason || null },
    }),
  /** Void, not delete. The reason is required by the API. */
  void: (dataType: string, id: string | number, reason: string) =>
    request<WriteResponse>(`/api/transactions/${dataType}/${id}`, {
      method: 'DELETE',
      params: { reason },
    }),
  restore: (dataType: string, id: string | number) =>
    request<WriteResponse>(`/api/transactions/${dataType}/${id}/restore`, {
      method: 'POST',
    }),
  export: (dataType: string, fmt: 'csv' | 'xlsx', query: RecordQuery = {},
           columns?: string[]) =>
    saveAs(`/api/transactions/${dataType}/export/${fmt}`, {
      params: { ...recordParams(query), columns: columns?.join(',') },
    }),
};

export { ApiError } from './apiClient';
