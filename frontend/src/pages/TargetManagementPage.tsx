/**
 * Target Management: building a target rather than reading one.
 *
 * The Target page beside this one reports achievement against targets that
 * already exist. This is where the target is set — and the two are separate
 * sections because seeing a number and deciding it are different privileges.
 *
 * Six tabs, in the order the work happens: **Planning** fixes the scope,
 * **Country Target** is where the one typed figure is entered, **Historical
 * Analysis** reads the actual sales it will be allocated by, **Auto Allocation**
 * runs the engine, **Target Review** is the hierarchy a manager judges before
 * signing, **Approval** is the chain it travels up, **My Approvals** is what is
 * waiting on the person reading, **Versions** is the chain of revisions beneath
 * one plan and where an approved one is locked, **Comparison** is what moved
 * between two of them, **Audit Trail** is the record of every decision taken,
 * and **Overview** is where every plan has got to.
 *
 * Overview is drawn first because it is the tab somebody arrives on, and it
 * claims nothing the other tabs do not: counts of workflow state and figures
 * somebody typed. Achievement against a locked target is the Target page's
 * question, deliberately absent here — a dashboard that recomputed a figure
 * another screen already reports is how two screens come to disagree.
 *
 * Locking sits on the Versions tab rather than beside Approve, because it is
 * not another signature: it is the act that writes into `fact_target`, after
 * which the Target page, every export and the AI assistant are reading these
 * figures and a corrected transfer price can no longer change them.
 *
 * Approving and revising are drawn from two different actions, deliberately.
 * A Sales Officer may raise a revision against their own sub-territory and must
 * never sign one off; the Revise button and the Approve button are therefore
 * gated on `REVISE` and `APPROVE` separately rather than on one "can act" flag.
 *
 * Nothing on this page decides what a user may do. `options.actions` comes from
 * the backend and only decides what is *drawn*; every endpoint re-resolves the
 * same permission and refuses a hand-typed request regardless.
 *
 * Tab and selected plan live in the URL, as filter state does everywhere else in
 * this app, so a link to a particular plan's version history is shareable and
 * the back button works.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { GitBranch, Plus } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { StatCard } from '../components/KpiCard';
import { PageHeader, Section } from '../components/PageHeader';
import { EmptyState, QueryState } from '../components/States';
import { CountryTargetGrid } from '../components/targetmgmt/CountryTargetGrid';
import { AllocationPanel } from '../components/targetmgmt/AllocationPanel';
import {
  AdjustmentPanel,
  AllocationRuns,
} from '../components/targetmgmt/AllocationRuns';
import {
  ReadinessGate,
  ReadinessSummary,
} from '../components/targetmgmt/ReadinessGate';
import { ReviewTree } from '../components/targetmgmt/ReviewTree';
import { ApprovalMatrixEditor } from '../components/targetmgmt/ApprovalMatrixEditor';
import { AuditTrail } from '../components/targetmgmt/AuditTrail';
import { CountryTargetUpload } from '../components/targetmgmt/CountryTargetUpload';
import { TargetDashboard } from '../components/targetmgmt/TargetDashboard';
import { VersionComparison } from '../components/targetmgmt/VersionComparison';
import { LockPanel } from '../components/targetmgmt/LockPanel';
import { ApprovalPanel } from '../components/targetmgmt/ApprovalPanel';
import { MyApprovals } from '../components/targetmgmt/MyApprovals';
import { RevisionDialog } from '../components/targetmgmt/RevisionDialog';
import { HistoricalAnalysis } from '../components/targetmgmt/HistoricalAnalysis';
import { useT } from '../contexts/I18nContext';
import { targetManagementService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatDateTime } from '../utils/format';
import type {
  TargetLockResult,
  TargetUploadPreview,
  TargetUploadResult,
  TargetMatrixRow,
  TargetPeriod,
  TargetPlan,
  TargetPlanStatus,
  TargetReviewRow,
  TargetVersion,
} from '../types/api';

type Tab =
  | 'overview'
  | 'planning'
  | 'country'
  | 'history'
  | 'allocation'
  | 'review'
  | 'approval'
  | 'queue'
  | 'versions'
  | 'compare'
  | 'trail';

/**
 * In the order the work happens: fix the scope, type the country volumes, then
 * version the result through approval. The screens between the second and third
 * — allocation, review, approval — arrive on later steps and slot in here.
 */
const TABS: Tab[] = [
  'overview', 'planning', 'country', 'history', 'allocation', 'review',
  'approval', 'queue', 'versions', 'compare', 'trail',
];

/**
 * Status colours, declared once so the plan table, the version list and the
 * page header can never disagree about what "approved" looks like.
 *
 * Green is a target that is settled, amber one still moving, red one sent back,
 * slate one superseded. Colour is never the only signal — the status is spelled
 * out in words in every place this is used.
 */
/**
 * Rows per page of the audit trail. Paged rather than scrolled because a
 * plan's trail grows without bound — every edit, allocation, revision,
 * signature and lock — and it is the record that gets more valuable as it
 * gets longer, so it must not get slower at the same rate.
 */
const AUDIT_PAGE_SIZE = 50;

const STATUS_TONE: Record<TargetPlanStatus, string> = {
  DRAFT: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  ALLOCATION_IN_PROGRESS:
    'bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-300',
  ALLOCATED: 'bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-300',
  UNDER_REVIEW: 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
  PARTIALLY_APPROVED:
    'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
  APPROVED: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300',
  REJECTED: 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300',
  LOCKED: 'bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-200',
  REVISED: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400',
};

function StatusPill({ status }: { status: TargetPlanStatus }) {
  return (
    <span
      className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${
        STATUS_TONE[status] ?? STATUS_TONE.DRAFT
      }`}
    >
      {status.replace(/_/g, ' ')}
    </span>
  );
}

export default function TargetManagementPage() {
  const t = useT();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [error, setError] = useState<string | null>(null);

  const tab = (searchParams.get('tab') as Tab) ?? 'planning';
  const selectedPlanId = Number(searchParams.get('plan')) || null;

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setSearchParams(next, { replace: true });
  };

  const options = useQuery({
    queryKey: ['target-management-options'],
    queryFn: () => targetManagementService.options(),
  });

  const plans = useQuery({
    queryKey: ['target-management-plans'],
    queryFn: () => targetManagementService.plans({ limit: 200 }),
  });

  const versions = useQuery({
    queryKey: ['target-management-versions', selectedPlanId],
    queryFn: () => targetManagementService.versions(selectedPlanId as number),
    enabled: selectedPlanId !== null,
  });

  const planRows = plans.data?.plans ?? [];
  const versionRows = versions.data?.versions ?? [];
  const actions = options.data?.actions ?? {};
  const canCreate = actions.CREATE === true;
  const canEdit = actions.EDIT === true;
  /*
   * Two flags, never one. Signing off on the figure a sales force is measured
   * on and asking for it to be changed are held by different people: a Sales
   * Officer revises and never approves.
   */
  const canApprove = actions.APPROVE === true;
  const canRevise = actions.REVISE === true;
  /*
   * Loading a country target from a file is its own action, so a planner
   * who may adjust one figure by hand and one who may load three hundred
   * at once can be different people.
   */
  const canUpload = actions.UPLOAD === true;

  // Not memoised: `planRows` is a fresh array on every render (the `?? []`
  // fallback), so a memo would recompute anyway while claiming not to.
  const selectedPlan =
    planRows.find((plan) => plan.plan_id === selectedPlanId) ?? null;

  /**
   * The country target is always the *current* version's.
   *
   * A superseded version's numbers are readable through Versions, but this is
   * the entry screen, and typing into a version something newer has replaced
   * would edit a target nothing downstream will read.
   */
  const currentVersionId = selectedPlan?.current_version_id ?? null;

  const countryTarget = useQuery({
    queryKey: ['target-management-country', currentVersionId],
    queryFn: () => targetManagementService.countryTarget(currentVersionId as number),
    enabled: tab === 'country' && currentVersionId !== null,
  });

  const availableMaterials = useQuery({
    queryKey: ['target-management-materials', currentVersionId],
    queryFn: () =>
      targetManagementService.availableMaterials(currentVersionId as number),
    enabled: tab === 'country' && currentVersionId !== null,
  });

  const historyQuery = useQuery({
    queryKey: ['target-management-history', currentVersionId],
    queryFn: () => targetManagementService.history(currentVersionId as number),
    enabled: tab === 'history' && currentVersionId !== null,
  });

  const allocation = useQuery({
    queryKey: ['target-management-allocation', currentVersionId],
    queryFn: () => targetManagementService.allocation(currentVersionId as number),
    enabled: tab === 'allocation' && currentVersionId !== null,
  });

  const readiness = useQuery({
    queryKey: ['target-management-readiness', currentVersionId],
    queryFn: () => targetManagementService.readiness(currentVersionId as number),
    enabled: tab === 'allocation' && currentVersionId !== null,
  });

  const runs = useQuery({
    queryKey: ['target-management-runs', currentVersionId],
    queryFn: () =>
      targetManagementService.allocationRuns(currentVersionId as number),
    enabled: tab === 'allocation' && currentVersionId !== null,
  });

  const adjustments = useQuery({
    queryKey: ['target-management-adjustments', currentVersionId],
    queryFn: () =>
      targetManagementService.adjustments(currentVersionId as number),
    enabled: tab === 'allocation' && currentVersionId !== null,
  });

  /**
   * The review tree, narrowed to one material when the reader picks one.
   *
   * The material lives in the URL beside the tab, so a link to "Glyfon across
   * the hierarchy" is shareable — the same rule filter state follows everywhere
   * else in this app.
   */
  const reviewMaterial = searchParams.get('material');

  const reviewTree = useQuery({
    queryKey: ['target-management-review', currentVersionId, reviewMaterial],
    queryFn: () =>
      targetManagementService.review(currentVersionId as number, reviewMaterial),
    enabled: tab === 'review' && currentVersionId !== null,
  });

  const approvalState = useQuery({
    queryKey: ['target-management-approval', currentVersionId],
    queryFn: () =>
      targetManagementService.approvalState(currentVersionId as number),
    enabled: tab === 'approval' && currentVersionId !== null,
  });

  const approvalMatrix = useQuery({
    queryKey: ['target-management-matrix'],
    queryFn: () => targetManagementService.approvalMatrix(),
    enabled: tab === 'approval',
  });

  const queue = useQuery({
    queryKey: ['target-management-queue'],
    queryFn: () => targetManagementService.myApprovals(),
    enabled: tab === 'queue',
  });

  const lockState = useQuery({
    queryKey: ['target-management-lock', currentVersionId],
    queryFn: () => targetManagementService.lockState(currentVersionId as number),
    enabled: tab === 'versions' && currentVersionId !== null,
  });

  /**
   * The trail is paged and filtered on the server, so both live in the URL
   * beside the tab — a link to "every lock on this plan" is shareable, the
   * same rule filter state follows everywhere else in this app.
   */
  const trailAction = searchParams.get('audit_action');
  const trailPage = Number(searchParams.get('audit_page') ?? '0') || 0;

  const auditTrail = useQuery({
    queryKey: ['target-management-trail', selectedPlanId, trailAction, trailPage],
    queryFn: () =>
      targetManagementService.auditTrail({
        ...(selectedPlanId ? { plan_id: selectedPlanId } : {}),
        ...(trailAction ? { action: trailAction } : {}),
        limit: AUDIT_PAGE_SIZE,
        offset: trailPage * AUDIT_PAGE_SIZE,
      }),
    enabled: tab === 'trail',
  });

  /** The overview's year filter, in the URL like every other filter. */
  const financialYear = searchParams.get('fy');

  /**
   * Which version the current one is compared against. In the URL beside
   * the tab, so a link to "V1 against V3" is shareable — the rule filter
   * state follows everywhere else in this app.
   */
  const baseVersionId = Number(searchParams.get('base') ?? '') || null;

  const compareOptions = useQuery({
    queryKey: ['target-management-compare-options', selectedPlanId],
    queryFn: () =>
      targetManagementService.compareOptions(selectedPlanId as number),
    enabled: tab === 'compare' && selectedPlanId !== null,
  });

  const comparison = useQuery({
    queryKey: ['target-management-compare', currentVersionId, baseVersionId,
               reviewMaterial],
    queryFn: () =>
      targetManagementService.compareVersions(
        currentVersionId as number, baseVersionId as number, reviewMaterial),
    enabled:
      tab === 'compare' && currentVersionId !== null && baseVersionId !== null,
  });

  const overview = useQuery({
    queryKey: ['target-management-dashboard', financialYear],
    queryFn: () => targetManagementService.dashboard(financialYear),
    enabled: tab === 'overview',
  });

  const factorCatalogue = useQuery({
    queryKey: ['target-management-factors'],
    queryFn: () => targetManagementService.allocationFactors(),
    enabled: tab === 'allocation',
    // The catalogue is a declaration, not data: it changes when the engine
    // changes, which is a deploy, not a click.
    staleTime: Infinity,
  });

  /**
   * Which node the revision dialog is open on, or `null`.
   *
   * Held here rather than inside `ReviewTree` because the mutation, its error
   * and the invalidation all live at this level — the tree draws rows and
   * reports a click, and decides nothing about what happens next.
   */
  const [revising, setRevising] = useState<TargetReviewRow | null>(null);
  const [lockResult, setLockResult] = useState<TargetLockResult | null>(null);
  /*
   * The staged upload and its outcome, held here rather than in the
   * component: the preview is what `apply` acts on, and the component that
   * draws it decides nothing about what happens next.
   */
  const [uploadPreview, setUploadPreview] =
    useState<TargetUploadPreview | null>(null);
  const [uploadResult, setUploadResult] =
    useState<TargetUploadResult | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  /**
   * Everything on this page that changes the approval state invalidates the
   * same four queries, because they are four views of one thing: an approved
   * revision moves the allocation, which moves the review tree, which changes
   * what blocks the chain, which changes what is in anyone's queue.
   */
  const refreshApprovalViews = () => {
    void queryClient.invalidateQueries({ queryKey: ['target-management-review'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-approval'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-queue'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-versions'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-plans'] });
  };

  const createRevision = useMutation({
    mutationFn: (body: {
      level: string;
      node_code: string;
      material_code?: string | null;
      requested_volume: string;
      reason: string;
    }) =>
      targetManagementService.createRevision(currentVersionId as number, body),
    onSuccess: () => {
      setRevising(null);
      setError(null);
      refreshApprovalViews();
    },
    onError: (err: Error) => setError(err.message),
  });

  const decideRevision = useMutation({
    mutationFn: (input: {
      versionId: number;
      revisionId: number;
      approve: boolean;
    }) =>
      targetManagementService.decideRevision(input.versionId, input.revisionId, {
        approve: input.approve,
      }),
    onSuccess: () => {
      setError(null);
      refreshApprovalViews();
    },
    onError: (err: Error) => setError(err.message),
  });

  const actOnVersion = useMutation({
    mutationFn: async (input: {
      act: 'submit' | 'approve' | 'reject' | 'send-back';
      comment: string;
    }): Promise<{ blockers: string[] }> => {
      const id = currentVersionId as number;
      if (input.act === 'submit') {
        return targetManagementService.submitForApproval(id, input.comment);
      }
      if (input.act === 'approve') {
        return targetManagementService.approveVersion(id, input.comment);
      }
      if (input.act === 'reject') {
        return targetManagementService.rejectVersion(id, input.comment);
      }
      return targetManagementService.sendVersionBack(id, input.comment);
    },
    onSuccess: () => {
      setError(null);
      refreshApprovalViews();
    },
    onError: (err: Error) => setError(err.message),
  });

  const lockVersion = useMutation({
    mutationFn: (comment: string) =>
      targetManagementService.lockVersion(currentVersionId as number, comment),
    onSuccess: (result) => {
      // Reported rather than implied: a lock is the one act here that
      // reaches outside this module, and how many rows it wrote is what
      // tells a planner it did what they expected.
      setError(null);
      setLockResult(result);
      void queryClient.invalidateQueries({ queryKey: ['target-management-lock'] });
      void queryClient.invalidateQueries({ queryKey: ['target-management-trail'] });
      refreshApprovalViews();
    },
    onError: (err: Error) => setError(err.message),
  });

  const previewUpload = useMutation({
    mutationFn: (file: File) =>
      targetManagementService.previewCountryTargetUpload(
        currentVersionId as number, file),
    onSuccess: (data) => {
      setUploadError(null);
      setUploadResult(null);
      setUploadPreview(data);
    },
    onError: (err: Error) => {
      setUploadPreview(null);
      setUploadError(err.message);
    },
  });

  const applyUpload = useMutation({
    mutationFn: (token: string) =>
      targetManagementService.applyCountryTargetUpload(
        currentVersionId as number, token),
    onSuccess: (data) => {
      setUploadError(null);
      setUploadPreview(null);
      setUploadResult(data);
      void queryClient.invalidateQueries({
        queryKey: ['target-management-country'] });
      void queryClient.invalidateQueries({
        queryKey: ['target-management-trail'] });
    },
    onError: (err: Error) => setUploadError(err.message),
  });

  const saveMatrix = useMutation({
    mutationFn: (
      entries: Array<Partial<TargetMatrixRow> & { role: string; fields_present: string[] }>,
    ) => targetManagementService.updateApprovalMatrix(entries),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ['target-management-matrix'] });
      void queryClient.invalidateQueries({ queryKey: ['target-management-approval'] });
    },
    onError: (err: Error) => setError(err.message),
  });

  /**
   * The reader's own adjustment limit, used only to warn *before* they commit
   * that a change of this size will escalate. `null` is unlimited and is also
   * what an unloaded matrix looks like — in both cases no warning is shown,
   * which is right: the backend routes the request either way, and a warning
   * guessed from missing configuration would be worse than none.
   */
  const myLimitPercent =
    approvalState.data?.my_matrix?.adjustment_limit_percent ?? null;

  const runningJobId =
    allocation.data?.job && ['QUEUED', 'PROCESSING'].includes(allocation.data.job.status)
      ? allocation.data.job.job_id
      : null;

  /**
   * Poll only while a run is actually in flight.
   *
   * `refetchInterval` returns false once the job reaches a terminal state, so a
   * finished allocation stops costing a request every two seconds — and the
   * poll reads the durable job row, which is why it survives a page reload.
   */
  const jobPoll = useQuery({
    queryKey: ['target-management-job', runningJobId],
    queryFn: () => targetManagementService.allocationJob(runningJobId as string),
    enabled: runningJobId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'QUEUED' || status === 'PROCESSING' ? 2000 : false;
    },
  });

  const polledJob = jobPoll.data ?? allocation.data?.job ?? null;
  const settledStatus =
    polledJob && !['QUEUED', 'PROCESSING'].includes(polledJob.status)
      ? polledJob.status
      : null;

  /**
   * A run that has just finished changes the reconciliation and the version's
   * status, so the surrounding state is refetched once — in an effect, because
   * invalidating during render would re-enter the render it was called from.
   *
   * Keyed on the settled status rather than on the job, so it fires on the
   * transition and not on every poll that returns the same finished job.
   */
  useEffect(() => {
    if (!settledStatus) return;
    void queryClient.invalidateQueries({ queryKey: ['target-management-allocation'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-plans'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-runs'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-readiness'] });
  }, [settledStatus, queryClient]);

  /** Refetch both lists: a new version changes the plan row's current version. */
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['target-management-plans'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-versions'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-options'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-country'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-materials'] });
    // The basis table shows the current target beside each material's history,
    // so a country-target save changes it too.
    void queryClient.invalidateQueries({ queryKey: ['target-management-history'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-allocation'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-readiness'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-runs'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-adjustments'] });
    void queryClient.invalidateQueries({ queryKey: ['target-management-review'] });
  };

  /**
   * A 409 from this API is a state refusal with a message worth reading — a
   * scope already planned, a version already approved. It is shown verbatim
   * rather than replaced with a generic failure, because the reason is the
   * whole value to a planner.
   */
  const onError = (err: unknown) => {
    const detail = (err as { detail?: { message?: string } })?.detail;
    setError(detail?.message ?? (err as Error)?.message ?? t('common.error'));
  };

  const createPlan = useMutation({
    mutationFn: targetManagementService.createPlan,
    onSuccess: (data) => {
      setError(null);
      refresh();
      setParam('plan', String(data.plan.plan_id));
    },
    onError,
  });

  const createVersion = useMutation({
    mutationFn: ({ planId, reason }: { planId: number; reason: string }) =>
      targetManagementService.createVersion(planId, reason),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError,
  });

  const setStatus = useMutation({
    mutationFn: ({ versionId, status }: { versionId: number; status: TargetPlanStatus }) =>
      targetManagementService.setVersionStatus(versionId, status),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError,
  });

  const startAllocation = useMutation({
    mutationFn: () =>
      targetManagementService.startAllocation(currentVersionId as number),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError,
  });

  const setAdjustment = useMutation({
    mutationFn: (body: {
      level: string;
      node_code: string;
      adjustment_volume: number;
      reason: string;
    }) => targetManagementService.setAdjustment(currentVersionId as number, body),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError,
  });

  const removeAdjustment = useMutation({
    mutationFn: (adjustmentId: number) =>
      targetManagementService.removeAdjustment(currentVersionId as number,
                                               adjustmentId),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError,
  });

  const saveCountry = useMutation({
    mutationFn: (lines: { material_code: string; target_volume: string }[]) =>
      targetManagementService.setCountryTarget(currentVersionId as number, lines),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError,
  });

  const planColumns = [
    { key: 'plan_code', header: t('targetMgmt.col.plan') },
    { key: 'financial_year', header: t('targetMgmt.col.financialYear') },
    { key: 'period_label', header: t('targetMgmt.col.period') },
    { key: 'sales_line_code', header: t('targetMgmt.col.salesLine') },
    {
      key: 'current_version_no',
      header: t('targetMgmt.col.version'),
      align: 'right' as const,
      render: (row: TargetPlan) =>
        row.current_version_no === null ? '—' : `V${row.current_version_no}`,
    },
    {
      key: 'status',
      header: t('targetMgmt.col.status'),
      render: (row: TargetPlan) => <StatusPill status={row.status} />,
    },
    { key: 'company_code', header: t('targetMgmt.col.company'), hidden: true },
    { key: 'bu_code', header: t('targetMgmt.col.businessUnit'), hidden: true },
    { key: 'created_by', header: t('targetMgmt.col.createdBy'), hidden: true },
    {
      key: 'created_at',
      header: t('targetMgmt.col.created'),
      hidden: true,
      render: (row: TargetPlan) => formatDateTime(row.created_at),
    },
  ];

  return (
    <>
      <PageHeader
        title={t('targetMgmt.title')}
        description={t('targetMgmt.subtitle')}
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard label={t('targetMgmt.kpiPlans')} value={String(plans.data?.total ?? 0)} />
        <StatCard
          label={t('targetMgmt.kpiOpen')}
          value={String(
            planRows.filter(
              (plan) => plan.status !== 'LOCKED' && plan.status !== 'APPROVED',
            ).length,
          )}
        />
        <StatCard
          label={t('targetMgmt.kpiLocked')}
          value={String(planRows.filter((plan) => plan.status === 'LOCKED').length)}
        />
      </div>

      <div className="mt-4 flex gap-1 border-b border-slate-200 dark:border-slate-700">
        {TABS.map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => setParam('tab', key)}
            className={`px-3 py-2 text-sm font-medium ${
              tab === key
                ? 'border-b-2 border-brand-600 text-brand-700 dark:text-brand-300'
                : 'text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
            }`}
          >
            {t(`targetMgmt.tab.${key}`)}
          </button>
        ))}
      </div>

      {error && (
        <div
          role="alert"
          className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300"
        >
          {error}
        </div>
      )}

      {tab === 'overview' && (
        <div className="mt-4">
          <QueryState
            isLoading={overview.isLoading}
            error={overview.error}
            onRetry={() => void overview.refetch()}
          >
            {overview.data && (
              <TargetDashboard
                data={overview.data}
                financialYear={financialYear}
                onFinancialYearChange={(year) => setParam('fy', year)}
                onOpenPlan={(planId, nextTab) => {
                  setParam('plan', String(planId));
                  setParam('tab', nextTab);
                }}
              />
            )}
          </QueryState>
        </div>
      )}

      {tab === 'planning' && (
        <>
          {canCreate && (
            <PlanForm
              busy={createPlan.isPending}
              onSubmit={(body) => createPlan.mutate(body)}
              options={options.data}
            />
          )}

          <Section title={t('targetMgmt.plansTitle')} className="mt-4">
            <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
              {t('targetMgmt.plansHelp')}
            </p>
            <QueryState
              isLoading={plans.isLoading}
              error={plans.error}
              onRetry={() => void plans.refetch()}
            >
              {planRows.length === 0 ? (
                <EmptyState message={t('targetMgmt.noPlans')} />
              ) : (
                <DataTable
                  tableId="target-management.plans"
                  rows={planRows}
                  columns={planColumns}
                  rowKey={(row) => String((row as TargetPlan).plan_id)}
                  onRowClick={(row) => {
                    setParam('plan', String((row as TargetPlan).plan_id));
                    setParam('tab', 'versions');
                  }}
                  searchable
                />
              )}
            </QueryState>
          </Section>
        </>
      )}

      {tab === 'country' && (
        <>
          {!selectedPlan ? (
            <Section title={t('targetMgmt.countryTitle')} className="mt-4">
              <EmptyState message={t('targetMgmt.pickPlan')} />
            </Section>
          ) : (
            <QueryState
              isLoading={countryTarget.isLoading}
              error={countryTarget.error}
              onRetry={() => void countryTarget.refetch()}
            >
              {countryTarget.data && (
                <>
                <CountryTargetGrid
                  lines={countryTarget.data.lines}
                  totals={countryTarget.data.totals}
                  notes={countryTarget.data.notes}
                  editable={countryTarget.data.editable}
                  canEdit={canEdit}
                  available={availableMaterials.data?.materials ?? []}
                  saving={saveCountry.isPending}
                  onSave={(lines) => saveCountry.mutate(lines)}
                />
                <CountryTargetUpload
                  preview={uploadPreview}
                  result={uploadResult}
                  editable={countryTarget.data.editable}
                  canUpload={canUpload}
                  busy={previewUpload.isPending || applyUpload.isPending}
                  error={uploadError}
                  onTemplate={() =>
                    void targetManagementService.countryTargetTemplate(
                      currentVersionId as number)
                  }
                  onFile={(file) => previewUpload.mutate(file)}
                  onApply={() =>
                    uploadPreview &&
                    applyUpload.mutate(uploadPreview.upload_token)
                  }
                  onDismiss={() => {
                    setUploadPreview(null);
                    setUploadError(null);
                  }}
                />
                </>
              )}
            </QueryState>
          )}
        </>
      )}

      {tab === 'history' && (
        <>
          {!selectedPlan ? (
            <Section title={t('targetMgmt.historyTitle')} className="mt-4">
              <EmptyState message={t('targetMgmt.pickPlan')} />
            </Section>
          ) : (
            <QueryState
              isLoading={historyQuery.isLoading}
              error={historyQuery.error}
              onRetry={() => void historyQuery.refetch()}
            >
              {historyQuery.data && <HistoricalAnalysis data={historyQuery.data} />}
            </QueryState>
          )}
        </>
      )}

      {tab === 'allocation' && (
        <>
          {!selectedPlan ? (
            <Section title={t('targetMgmt.allocationTitle')} className="mt-4">
              <EmptyState message={t('targetMgmt.pickPlan')} />
            </Section>
          ) : (
            <QueryState
              isLoading={allocation.isLoading}
              error={allocation.error}
              onRetry={() => void allocation.refetch()}
            >
              {allocation.data && (
                <>
                  {readiness.data && (
                    <ReadinessSummary readiness={readiness.data} />
                  )}
                  <AllocationPanel
                    state={allocation.data}
                    catalogue={factorCatalogue.data}
                    job={polledJob}
                    canEdit={canEdit}
                    starting={startAllocation.isPending}
                    onStart={() => startAllocation.mutate()}
                  />
                  {readiness.data && <ReadinessGate readiness={readiness.data} />}
                  <AdjustmentPanel
                    stored={adjustments.data?.adjustments ?? []}
                    applied={polledJob?.result?.adjustments ?? []}
                    canEdit={canEdit}
                    editable={allocation.data.editable}
                    saving={setAdjustment.isPending}
                    onAdd={(body) => setAdjustment.mutate(body)}
                    onRemove={(id) => removeAdjustment.mutate(id)}
                  />
                  <AllocationRuns runs={runs.data?.runs ?? []} />
                </>
              )}
            </QueryState>
          )}
        </>
      )}

      {tab === 'review' && (
        <>
          {!selectedPlan ? (
            <Section title={t('targetMgmt.reviewTitle')} className="mt-4">
              <EmptyState message={t('targetMgmt.pickPlan')} />
            </Section>
          ) : (
            <QueryState
              isLoading={reviewTree.isLoading}
              error={reviewTree.error}
              onRetry={() => void reviewTree.refetch()}
            >
              {reviewTree.data && (
                <ReviewTree
                  data={reviewTree.data}
                  onMaterialChange={(material: string | null) =>
                    setParam('material', material)
                  }
                  canRevise={canRevise && reviewTree.data.rows.length > 0}
                  onRevise={setRevising}
                />
              )}
            </QueryState>
          )}
        </>
      )}

      {tab === 'approval' && (
        <>
          {!selectedPlan ? (
            <Section title={t('targetMgmt.approval.chainTitle')} className="mt-4">
              <EmptyState message={t('targetMgmt.pickPlan')} />
            </Section>
          ) : (
            <div className="mt-4 space-y-4">
              <QueryState
                isLoading={approvalState.isLoading}
                error={approvalState.error}
                onRetry={() => void approvalState.refetch()}
              >
                {approvalState.data && (
                  <ApprovalPanel
                    state={approvalState.data}
                    canApprove={canApprove}
                    submittable={
                      canEdit && approvalState.data.version?.status === 'ALLOCATED'
                    }
                    busy={actOnVersion.isPending}
                    error={null}
                    onSubmit={(comment) =>
                      actOnVersion.mutate({ act: 'submit', comment })
                    }
                    onApprove={(comment) =>
                      actOnVersion.mutate({ act: 'approve', comment })
                    }
                    onReject={(comment) =>
                      actOnVersion.mutate({ act: 'reject', comment })
                    }
                    onSendBack={(comment) =>
                      actOnVersion.mutate({ act: 'send-back', comment })
                    }
                  />
                )}
              </QueryState>

              <QueryState
                isLoading={approvalMatrix.isLoading}
                error={approvalMatrix.error}
                onRetry={() => void approvalMatrix.refetch()}
              >
                {approvalMatrix.data && (
                  <ApprovalMatrixEditor
                    data={approvalMatrix.data}
                    editable={canEdit}
                    busy={saveMatrix.isPending}
                    error={null}
                    onSave={(entries) => saveMatrix.mutate(entries)}
                  />
                )}
              </QueryState>
            </div>
          )}
        </>
      )}

      {tab === 'queue' && (
        <div className="mt-4">
          <QueryState
            isLoading={queue.isLoading}
            error={queue.error}
            onRetry={() => void queue.refetch()}
          >
            {queue.data && (
              <MyApprovals
                data={queue.data}
                busy={decideRevision.isPending}
                onOpenVersion={(planId) => {
                  setParam('plan', String(planId));
                  setParam('tab', 'approval');
                }}
                onDecide={(versionId, revisionId, approve) =>
                  decideRevision.mutate({ versionId, revisionId, approve })
                }
              />
            )}
          </QueryState>
        </div>
      )}

      {tab === 'compare' && (
        <div className="mt-4 space-y-4">
          {!selectedPlan ? (
            <Section title={t('targetMgmt.compare.rowsTitle')}>
              <EmptyState message={t('targetMgmt.pickPlan')} />
            </Section>
          ) : (
            <>
              <Section title={t('targetMgmt.compare.pickTitle')}>
                <label className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-slate-600 dark:text-slate-400">
                    {t('targetMgmt.compare.against')}
                  </span>
                  <select
                    className="input"
                    aria-label={t('targetMgmt.compare.against')}
                    value={baseVersionId ?? ''}
                    onChange={(event) => setParam('base', event.target.value || null)}
                  >
                    <option value="">{t('targetMgmt.compare.pickOne')}</option>
                    {(compareOptions.data?.versions ?? [])
                      .filter((option) => option.version_id !== currentVersionId)
                      .map((option) => (
                        <option
                          key={option.version_id}
                          value={option.version_id}
                          disabled={!option.has_allocation}
                        >
                          V{option.version_no}
                          {option.has_allocation
                            ? ''
                            : ` — ${t('targetMgmt.compare.notAllocated')}`}
                        </option>
                      ))}
                  </select>
                </label>
              </Section>
              {baseVersionId === null ? (
                <Section title={t('targetMgmt.compare.rowsTitle')}>
                  <EmptyState message={t('targetMgmt.compare.pickPrompt')} />
                </Section>
              ) : (
                <QueryState
                  isLoading={comparison.isLoading}
                  error={comparison.error}
                  onRetry={() => void comparison.refetch()}
                >
                  {comparison.data && (
                    <VersionComparison
                      data={comparison.data}
                      onMaterialChange={(material) =>
                        setParam('material', material)
                      }
                    />
                  )}
                </QueryState>
              )}
            </>
          )}
        </div>
      )}

      {tab === 'trail' && (
        <div className="mt-4">
          <QueryState
            isLoading={auditTrail.isLoading}
            error={auditTrail.error}
            onRetry={() => void auditTrail.refetch()}
          >
            {auditTrail.data && (
              <AuditTrail
                data={auditTrail.data}
                action={trailAction}
                pageSize={AUDIT_PAGE_SIZE}
                page={trailPage}
                onActionChange={(next) => {
                  setParam('audit_action', next);
                  setParam('audit_page', null);
                }}
                onPageChange={(next) =>
                  setParam('audit_page', next === 0 ? null : String(next))
                }
              />
            )}
          </QueryState>
        </div>
      )}

      {revising && (
        <RevisionDialog
          row={revising}
          materialCode={reviewMaterial}
          limitPercent={myLimitPercent}
          busy={createRevision.isPending}
          error={createRevision.error ? createRevision.error.message : null}
          onClose={() => setRevising(null)}
          onSubmit={(body) => createRevision.mutate(body)}
        />
      )}

      {tab === 'versions' && (
        <Section
          title={
            selectedPlan
              ? `${t('targetMgmt.versionsTitle')} — ${selectedPlan.plan_code}`
              : t('targetMgmt.versionsTitle')
          }
          className="mt-4"
          actions={
            selectedPlan &&
            canCreate && (
              <NewVersionButton
                busy={createVersion.isPending}
                onSubmit={(reason) =>
                  createVersion.mutate({ planId: selectedPlan.plan_id, reason })
                }
                label={t('targetMgmt.newVersion')}
                prompt={t('targetMgmt.reasonPrompt')}
              />
            )
          }
        >
          {!selectedPlan ? (
            <EmptyState message={t('targetMgmt.pickPlan')} />
          ) : (
            <>
              <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
                {t('targetMgmt.versionsHelp')}
              </p>
              {lockResult && (
                <p className="mb-3 rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300">
                  {t('targetMgmt.lock.written', {
                    written: String(lockResult.rows_written),
                    inserted: String(lockResult.rows_inserted),
                    updated: String(lockResult.rows_updated),
                    voided: String(lockResult.rows_voided),
                  })}
                </p>
              )}
              {lockState.data && (
                <div className="mb-4">
                  <LockPanel
                    state={lockState.data}
                    canLock={canApprove}
                    busy={lockVersion.isPending}
                    error={null}
                    onLock={(comment) => lockVersion.mutate(comment)}
                  />
                </div>
              )}
              <QueryState
                isLoading={versions.isLoading}
                error={versions.error}
                onRetry={() => void versions.refetch()}
              >
                <ol className="space-y-3">
                  {versionRows.map((version) => (
                    <VersionRow
                      key={version.version_id}
                      version={version}
                      canEdit={canEdit}
                      busy={setStatus.isPending}
                      onStatus={(status) =>
                        setStatus.mutate({ versionId: version.version_id, status })
                      }
                      t={t}
                    />
                  ))}
                </ol>
              </QueryState>
            </>
          )}
        </Section>
      )}
    </>
  );
}

/**
 * The plan scope form.
 *
 * Business Unit is narrowed by the chosen Company and Sales Line by the chosen
 * Business Unit, because the master data states those parents — offering a
 * sales line that belongs to another business unit would let a planner build a
 * scope the backend then refuses.
 */
function PlanForm({
  options,
  busy,
  onSubmit,
}: {
  options?: {
    companies: { code: string; name: string }[];
    business_units: { code: string; name: string; company_code: string }[];
    sales_lines: { code: string; name: string; bu_code: string }[];
    periods: { code: TargetPeriod; label: string }[];
    financial_years?: string[];
  };
  busy: boolean;
  onSubmit: (body: {
    financial_year: string;
    target_period: TargetPeriod;
    company_code: string;
    bu_code: string;
    sales_line_code: string;
    basis_financial_years?: string | null;
  }) => void;
}) {
  const t = useT();
  const [financialYear, setFinancialYear] = useState('');
  const [period, setPeriod] = useState<TargetPeriod>('FY');
  const [company, setCompany] = useState('');
  const [businessUnit, setBusinessUnit] = useState('');
  const [salesLine, setSalesLine] = useState('');

  const years = options?.financial_years ?? [];
  const units = (options?.business_units ?? []).filter(
    (unit) => !company || unit.company_code === company,
  );
  const lines = (options?.sales_lines ?? []).filter(
    (line) => !businessUnit || line.bu_code === businessUnit,
  );

  const ready = Boolean(
    (financialYear || years[0]) && period && company && businessUnit && salesLine,
  );

  return (
    <Section title={t('targetMgmt.createTitle')} className="mt-4">
      <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
        {t('targetMgmt.createHelp')}
      </p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Field label={t('targetMgmt.col.financialYear')}>
          <select
            className="input"
            value={financialYear || years[0] || ''}
            onChange={(event) => setFinancialYear(event.target.value)}
          >
            {years.map((year) => (
              <option key={year} value={year}>
                {year}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('targetMgmt.col.period')}>
          <select
            className="input"
            value={period}
            onChange={(event) => setPeriod(event.target.value as TargetPeriod)}
          >
            {(options?.periods ?? []).map((option) => (
              <option key={option.code} value={option.code}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('targetMgmt.col.company')}>
          <select
            className="input"
            value={company}
            onChange={(event) => {
              setCompany(event.target.value);
              // Clearing both is what stops a scope the master data refuses:
              // a business unit under the previous company is not a valid
              // child of this one.
              setBusinessUnit('');
              setSalesLine('');
            }}
          >
            <option value="">{t('common.select')}</option>
            {(options?.companies ?? []).map((option) => (
              <option key={option.code} value={option.code}>
                {option.code} · {option.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('targetMgmt.col.businessUnit')}>
          <select
            className="input"
            value={businessUnit}
            disabled={!company}
            onChange={(event) => {
              setBusinessUnit(event.target.value);
              setSalesLine('');
            }}
          >
            <option value="">{t('common.select')}</option>
            {units.map((option) => (
              <option key={option.code} value={option.code}>
                {option.code} · {option.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('targetMgmt.col.salesLine')}>
          <select
            className="input"
            value={salesLine}
            disabled={!businessUnit}
            onChange={(event) => setSalesLine(event.target.value)}
          >
            <option value="">{t('common.select')}</option>
            {lines.map((option) => (
              <option key={option.code} value={option.code}>
                {option.code} · {option.name}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div className="mt-4 flex items-center justify-between gap-3 border-t border-slate-100 pt-4 dark:border-slate-800">
        <p className="text-xs text-slate-500 dark:text-slate-400">
          {t('targetMgmt.createNote')}
        </p>
        <button
          type="button"
          className="btn-primary"
          disabled={!ready || busy}
          onClick={() =>
            onSubmit({
              financial_year: financialYear || years[0],
              target_period: period,
              company_code: company,
              bu_code: businessUnit,
              sales_line_code: salesLine,
            })
          }
        >
          <Plus size={14} />
          {t('targetMgmt.createPlan')}
        </button>
      </div>
    </Section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-300">
        {label}
      </span>
      {children}
    </label>
  );
}

/**
 * One version in the chain.
 *
 * The transitions offered are the ones this version can actually make, not a
 * fixed set: a locked version offers none, and a draft cannot jump to approved.
 * The backend refuses an illegal transition regardless — this only avoids
 * drawing a button whose sole outcome is an error.
 */
function VersionRow({
  version,
  canEdit,
  busy,
  onStatus,
  t,
}: {
  version: TargetVersion;
  canEdit: boolean;
  busy: boolean;
  onStatus: (status: TargetPlanStatus) => void;
  t: (key: string) => string;
}) {
  const next = NEXT_STATUSES[version.status] ?? [];
  return (
    <li
      className={`rounded-lg border p-3 ${
        version.is_current
          ? 'border-brand-200 bg-brand-50/40 dark:border-brand-900 dark:bg-brand-950/20'
          : 'border-slate-200 dark:border-slate-700'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold text-slate-900 dark:text-slate-100">
          {version.label}
        </span>
        <StatusPill status={version.status} />
        {version.is_current && (
          <span className="rounded-full bg-brand-100 px-2 py-0.5 text-[10px] font-bold tracking-wide text-brand-700 dark:bg-brand-950/60 dark:text-brand-300">
            {t('targetMgmt.current')}
          </span>
        )}
        <span className="ml-auto text-xs text-slate-500 dark:text-slate-400">
          {version.created_by ?? '—'} · {formatDateTime(version.created_at)}
        </span>
      </div>
      {version.reason && (
        <p className="mt-1.5 text-sm text-slate-600 dark:text-slate-300">
          {version.reason}
        </p>
      )}
      <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
        {t('targetMgmt.countryLines')}: {version.country_line_count ?? 0}
      </p>
      {canEdit && next.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {next.map((status) => (
            <button
              key={status}
              type="button"
              className="btn-secondary text-xs"
              disabled={busy}
              onClick={() => onStatus(status)}
            >
              {status.replace(/_/g, ' ')}
            </button>
          ))}
        </div>
      )}
    </li>
  );
}

/**
 * Which status may follow which — the browser's copy of the backend's table.
 *
 * A copy, and therefore a risk: the authority is `plans.ALLOWED_TRANSITIONS`,
 * which refuses anything this list gets wrong. It exists only so a button whose
 * one possible outcome is a 409 is not drawn in the first place.
 */
const NEXT_STATUSES: Partial<Record<TargetPlanStatus, TargetPlanStatus[]>> = {
  DRAFT: ['ALLOCATION_IN_PROGRESS', 'UNDER_REVIEW'],
  ALLOCATION_IN_PROGRESS: ['ALLOCATED', 'DRAFT'],
  ALLOCATED: ['UNDER_REVIEW', 'ALLOCATION_IN_PROGRESS', 'DRAFT'],
  UNDER_REVIEW: ['PARTIALLY_APPROVED', 'APPROVED', 'REJECTED'],
  PARTIALLY_APPROVED: ['APPROVED', 'REJECTED', 'UNDER_REVIEW'],
  APPROVED: ['LOCKED', 'UNDER_REVIEW'],
  REJECTED: ['DRAFT', 'ALLOCATION_IN_PROGRESS'],
  LOCKED: [],
  REVISED: [],
};

/** Creating a version requires a reason, so the button collects one first. */
function NewVersionButton({
  busy,
  onSubmit,
  label,
  prompt,
}: {
  busy: boolean;
  onSubmit: (reason: string) => void;
  label: string;
  prompt: string;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState('');

  if (!open) {
    return (
      <button type="button" className="btn-secondary" onClick={() => setOpen(true)}>
        <GitBranch size={14} />
        {label}
      </button>
    );
  }
  return (
    <div className="flex items-center gap-2">
      <input
        className="input w-64"
        placeholder={prompt}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        autoFocus
      />
      <button
        type="button"
        className="btn-primary"
        disabled={busy || !reason.trim()}
        onClick={() => {
          onSubmit(reason.trim());
          setReason('');
          setOpen(false);
        }}
      >
        {label}
      </button>
      <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
        ×
      </button>
    </div>
  );
}
