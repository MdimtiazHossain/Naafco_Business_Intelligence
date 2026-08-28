/**
 * Hierarchical target review: every node, and whether it adds up.
 *
 * The screen a manager opens to judge a target before signing it. Country down
 * to Customer, each row carrying what it was allocated, what it sold last year,
 * and whether its own figure agrees with the sum of its children.
 *
 * **The tree is a filtered flat list, not a nested one.** The backend sends
 * rows in reading order with a `depth`, and expansion decides which of them are
 * drawn — which means this is still a `DataTable`, so hiding a column, moving
 * it and resizing it work here exactly as they do on every other report table
 * in the app. A bespoke tree component would have lost all three.
 *
 * **Recon variance is displayed even though it is always zero.** The allocation
 * engine distributes with largest remainder, so children cannot come to more or
 * less than their parent — but a claim that is checked and shown is worth more
 * than one that is merely true, and this is the column a manager looks at before
 * signing.
 *
 * Nothing here computes a figure. Growth, achievement and variance all arrive
 * calculated, because a browser-side sum would be a second opinion about numbers
 * that must have exactly one.
 */

import { ChevronDown, ChevronRight, Scale, TriangleAlert } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Section } from '../PageHeader';
import { EmptyState } from '../States';
import { useT } from '../../contexts/I18nContext';
import { DataTable } from '../../tables/DataTable';
import { formatAmount, formatPercent, formatQuantity } from '../../utils/format';
import type { TargetReviewResponse, TargetReviewRow } from '../../types/api';

/** A figure that could not be computed — never a dash, which reads as zero. */
function NotAvailable({ title }: { title?: string }) {
  return (
    <span className="cursor-help text-slate-400 dark:text-slate-500" title={title}>
      n/a
    </span>
  );
}

const STATUS_TONE: Record<string, string> = {
  ALLOCATED: 'bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-300',
  UNDER_REVIEW: 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
  PARTIALLY_APPROVED:
    'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
  APPROVED:
    'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300',
  REJECTED: 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300',
  LOCKED: 'bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-200',
};

function nodeKey(row: TargetReviewRow): string {
  return `${row.level}:${row.node_code}`;
}

export function ReviewTree({
  data,
  onMaterialChange,
  onRevise,
  canRevise = false,
}: {
  data: TargetReviewResponse;
  onMaterialChange: (material: string | null) => void;
  /**
   * Raise a revision against one node. Omitted where revising is not on offer,
   * which is what keeps the column absent rather than present-and-inert — a
   * button whose only outcome is a refusal is worse than no button.
   */
  onRevise?: (row: TargetReviewRow) => void;
  canRevise?: boolean;
}) {
  const t = useT();

  /**
   * Which nodes are open.
   *
   * Seeded to the top two levels rather than to everything: a full customer
   * tree is thousands of rows, and a screen that opens with all of them expanded
   * is a screen nobody can read. Collapsed nodes keep their state, so drilling
   * into a region and back out does not lose the branches already opened.
   */
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    for (const row of data.rows) {
      if (row.depth <= 1 && row.has_children) initial.add(nodeKey(row));
    }
    return initial;
  });

  /**
   * A row is drawn when every ancestor above it is open.
   *
   * Computed in one pass over the flat list: because the rows arrive in reading
   * order, an ancestor is always seen before its descendants, so the deepest
   * closed ancestor can be tracked as a single depth rather than by walking up
   * a parent chain per row.
   */
  const visible = useMemo(() => {
    const out: TargetReviewRow[] = [];
    let hiddenBelow: number | null = null;
    for (const row of data.rows) {
      if (hiddenBelow !== null && row.depth > hiddenBelow) continue;
      hiddenBelow = null;
      out.push(row);
      if (row.has_children && !expanded.has(nodeKey(row))) {
        hiddenBelow = row.depth;
      }
    }
    return out;
  }, [data.rows, expanded]);

  const toggle = (row: TargetReviewRow) => {
    setExpanded((current) => {
      const next = new Set(current);
      const key = nodeKey(row);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const columns = [
    {
      key: 'name',
      header: t('targetMgmt.levelNode'),
      render: (row: TargetReviewRow) => (
        <span
          className="flex items-center gap-1.5"
          style={{ paddingLeft: `${row.depth * 16}px` }}
        >
          {row.has_children ? (
            <button
              type="button"
              className="rounded p-0.5 text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
              aria-label={
                expanded.has(nodeKey(row))
                  ? t('targetMgmt.collapse')
                  : t('targetMgmt.expand')
              }
              aria-expanded={expanded.has(nodeKey(row))}
              onClick={() => toggle(row)}
            >
              {expanded.has(nodeKey(row)) ? (
                <ChevronDown size={14} />
              ) : (
                <ChevronRight size={14} />
              )}
            </button>
          ) : (
            <span className="w-[22px]" />
          )}
          <span className="min-w-[72px] text-[10px] uppercase tracking-wide text-slate-400">
            {t(`targetMgmt.level.${row.level}`)}
          </span>
          <span
            className={
              row.depth <= 1
                ? 'font-semibold text-slate-900 dark:text-slate-50'
                : 'text-slate-700 dark:text-slate-200'
            }
          >
            {row.node_code}
            {row.name ? ` · ${row.name}` : ''}
          </span>
        </span>
      ),
    },
    {
      key: 'target_volume',
      header: t('targetMgmt.col.targetVolume'),
      align: 'right' as const,
      render: (row: TargetReviewRow) => (
        <span className="font-semibold tabular-nums">
          {formatQuantity(row.target_volume)}
          {row.adjusted && (
            <span
              className="ml-1 cursor-help text-amber-600 dark:text-amber-400"
              title={t('targetMgmt.adjustedFromSystem', {
                system: formatQuantity(row.system_volume),
              })}
            >
              *
            </span>
          )}
        </span>
      ),
    },
    {
      key: 'target_quantity',
      header: t('targetMgmt.col.calculatedQuantity'),
      align: 'right' as const,
      render: (row: TargetReviewRow) =>
        row.target_quantity === null ? (
          <NotAvailable
            title={t('targetMgmt.missingDerivation', {
              materials: row.missing_derivation.slice(0, 5).join(', '),
            })}
          />
        ) : (
          <span className="tabular-nums text-slate-500">
            {formatQuantity(row.target_quantity)}
          </span>
        ),
    },
    {
      key: 'target_value',
      header: t('targetMgmt.col.calculatedValue'),
      align: 'right' as const,
      render: (row: TargetReviewRow) =>
        row.target_value === null ? (
          <NotAvailable />
        ) : (
          <span className="tabular-nums text-slate-500">
            {formatAmount(row.target_value)}
          </span>
        ),
    },
    {
      key: 'previous_year_volume',
      header: t('targetMgmt.previousYear'),
      align: 'right' as const,
      render: (row: TargetReviewRow) =>
        row.previous_year_volume === null ? (
          <NotAvailable title={t('targetMgmt.noSalesLastYear')} />
        ) : (
          <span className="tabular-nums">
            {formatQuantity(row.previous_year_volume)}
          </span>
        ),
    },
    {
      key: 'growth_percent',
      header: t('targetMgmt.col.growth'),
      align: 'right' as const,
      render: (row: TargetReviewRow) =>
        row.growth_percent === null ? (
          <NotAvailable title={t('targetMgmt.growthUndefined')} />
        ) : (
          <span
            className={`font-medium tabular-nums ${
              row.growth_percent >= 0
                ? 'text-emerald-700 dark:text-emerald-400'
                : 'text-red-600 dark:text-red-400'
            }`}
          >
            {formatPercent(row.growth_percent, { signed: true })}
          </span>
        ),
    },
    {
      key: 'achievement_percent',
      header: t('targetMgmt.achievement'),
      align: 'right' as const,
      render: (row: TargetReviewRow) =>
        row.achievement_percent === null ? (
          <NotAvailable title={t('targetMgmt.noActualsYet')} />
        ) : (
          <span className="tabular-nums">
            {formatPercent(row.achievement_percent)}
          </span>
        ),
    },
    {
      key: 'recon_variance',
      header: t('targetMgmt.reconVariance'),
      align: 'right' as const,
      render: (row: TargetReviewRow) => (
        <span
          className={`tabular-nums ${
            row.recon_variance === 0
              ? 'text-emerald-700 dark:text-emerald-400'
              : 'font-semibold text-red-700 dark:text-red-400'
          }`}
        >
          {formatQuantity(row.recon_variance)}
        </span>
      ),
    },
    {
      key: 'pending_revisions',
      header: t('targetMgmt.pendingRevision'),
      align: 'right' as const,
      render: (row: TargetReviewRow) =>
        row.pending_revisions === 0 ? (
          <span className="text-slate-400">—</span>
        ) : (
          <span className="font-semibold text-amber-700 dark:text-amber-400">
            {row.pending_revisions}
          </span>
        ),
    },
    {
      key: 'status',
      header: t('targetMgmt.col.status'),
      render: (row: TargetReviewRow) => (
        <span
          className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${
            STATUS_TONE[row.status] ?? STATUS_TONE.ALLOCATED
          }`}
        >
          {row.status.replace(/_/g, ' ')}
        </span>
      ),
    },
    /*
     * Offered only when the caller can actually act on it. The column is added
     * to the list rather than rendered disabled, because a control that is
     * always there and never works teaches a reader to ignore it.
     */
    ...(canRevise && onRevise
      ? [{
          key: 'revise',
          header: t('targetMgmt.col.revise'),
          render: (row: TargetReviewRow) => (
            <button
              type="button"
              onClick={() => onRevise(row)}
              className="rounded border border-slate-300 px-2 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              {t('targetMgmt.col.revise')}
            </button>
          ),
        }]
      : []),
  ];

  return (
    <>
      <ReconciliationStrip data={data} />

      <Section
        title={t('targetMgmt.reviewTitle')}
        className="mt-4"
        actions={
          <div className="flex items-center gap-2">
            <select
              className="input"
              aria-label={t('targetMgmt.col.material')}
              value={data.material_code ?? ''}
              onChange={(event) => onMaterialChange(event.target.value || null)}
            >
              <option value="">{t('targetMgmt.allMaterials')}</option>
              {data.materials.map((material) => (
                <option key={material} value={material}>
                  {material}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn-secondary text-xs"
              onClick={() =>
                setExpanded(
                  new Set(
                    data.rows.filter((row) => row.has_children).map(nodeKey),
                  ),
                )
              }
            >
              {t('targetMgmt.expandAll')}
            </button>
            <button
              type="button"
              className="btn-secondary text-xs"
              onClick={() => setExpanded(new Set())}
            >
              {t('targetMgmt.collapseAll')}
            </button>
          </div>
        }
      >
        <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
          {data.scope}
        </p>

        {data.rows.length === 0 ? (
          <EmptyState message={data.notes[0] ?? t('targetMgmt.noTree')} />
        ) : (
          <DataTable
            tableId="target-management.review"
            rows={visible}
            columns={columns}
            rowKey={(row) => nodeKey(row as TargetReviewRow)}
            searchable
            dense
          />
        )}

        {data.notes.length > 0 && data.rows.length > 0 && (
          <ul className="mt-3 space-y-1">
            {data.notes.map((note) => (
              <li
                key={note}
                className="text-xs italic text-slate-500 dark:text-slate-400"
              >
                {note}
              </li>
            ))}
          </ul>
        )}
      </Section>
    </>
  );
}

/**
 * The reconciliation bar the design carries on every allocation screen.
 *
 * Prominent because it is the gate: final approval is blocked while any level
 * disagrees with its children, so a manager needs to see at a glance which side
 * of it they are on.
 */
function ReconciliationStrip({ data }: { data: TargetReviewResponse }) {
  const t = useT();
  const { balanced, mismatched_nodes, node_count, target_volume } =
    data.reconciliation;
  if (node_count === 0) return null;

  return (
    <div
      className={`mt-4 flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3 ${
        balanced
          ? 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-900 dark:bg-emerald-950/20'
          : 'border-red-200 bg-red-50/60 dark:border-red-900 dark:bg-red-950/20'
      }`}
    >
      {balanced ? (
        <Scale size={18} className="text-emerald-700 dark:text-emerald-400" />
      ) : (
        <TriangleAlert size={18} className="text-red-600 dark:text-red-400" />
      )}
      <span
        className={`text-xs font-bold uppercase tracking-wide ${
          balanced
            ? 'text-emerald-800 dark:text-emerald-300'
            : 'text-red-700 dark:text-red-300'
        }`}
      >
        {balanced ? t('targetMgmt.balanced') : t('targetMgmt.mismatch')}
      </span>
      <span className="text-xs text-slate-600 dark:text-slate-400">
        {t('targetMgmt.nodesChecked', { count: String(node_count) })}
      </span>
      <span className="ml-auto flex items-baseline gap-2">
        <span className="text-xs text-slate-600 dark:text-slate-400">
          {t('targetMgmt.totalVolume')}
        </span>
        <strong className="tabular-nums text-slate-900 dark:text-slate-50">
          {formatQuantity(target_volume)}
        </strong>
      </span>
      {!balanced && (
        <span className="w-full text-xs text-red-700 dark:text-red-300">
          {t('targetMgmt.mismatchedNodes', {
            count: String(mismatched_nodes),
          })}
        </span>
      )}
    </div>
  );
}
