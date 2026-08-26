/**
 * Agent Learning: the queue of questions the assistant handled badly, and the
 * vocabulary approved from it.
 *
 * Three tabs, in the order the work actually happens: a reviewer reads the
 * **queue**, proposes a meaning for a phrase, then approves it under
 * **vocabulary**. **Examples** is the same approval shape for worked questions.
 *
 * Every list and every choice on this page comes from the backend — the alias
 * kinds, the keywords an alias may point at, the statuses. Nothing about the
 * agent's vocabulary is restated here, so a keyword added on the server appears
 * in this form with no change to the browser.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Plus, Undo2, X } from 'lucide-react';
import { useState } from 'react';
import { StatCard } from '../components/KpiCard';
import { PageHeader, Section } from '../components/PageHeader';
import { EmptyState, QueryState } from '../components/States';
import { useT } from '../contexts/I18nContext';
import { learningService } from '../services';
import { DataTable } from '../tables/DataTable';
import { formatDateTime } from '../utils/format';
import type { AliasKind, LearningExample, LearningSignal, TermAlias } from '../types/api';

type Tab = 'queue' | 'vocabulary' | 'examples';

/** Status colours, one place, so the three tables agree. */
function StatusPill({ status }: { status: string }) {
  const tone =
    status === 'ACTIVE'
      ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'
      : status === 'PROPOSED' || status === 'NEW'
        ? 'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300'
        : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300';
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${tone}`}>
      {status}
    </span>
  );
}

export default function AgentLearningPage() {
  const t = useT();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>('queue');
  const [proposeFor, setProposeFor] = useState<LearningSignal | null>(null);
  const [error, setError] = useState<string | null>(null);

  const options = useQuery({
    queryKey: ['learning-options'],
    queryFn: () => learningService.options(),
  });

  const signals = useQuery({
    queryKey: ['learning-signals'],
    queryFn: () => learningService.signals({ limit: 200 }),
  });

  const aliases = useQuery({
    queryKey: ['learning-aliases'],
    queryFn: () => learningService.aliases(),
  });

  const examples = useQuery({
    queryKey: ['learning-examples'],
    queryFn: () => learningService.examples(),
  });

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['learning-signals'] });
    void queryClient.invalidateQueries({ queryKey: ['learning-aliases'] });
    void queryClient.invalidateQueries({ queryKey: ['learning-examples'] });
  }

  /**
   * Every decision goes through here so a refusal is shown rather than swallowed.
   *
   * The backend answers 409 with the reason — "that phrase already means sales",
   * "that keyword doesn't exist" — and the reason is the whole value to a
   * reviewer, who can act on it.
   */
  const decide = useMutation({
    mutationFn: (work: () => Promise<unknown>) => work(),
    onSuccess: () => {
      setError(null);
      setProposeFor(null);
      refresh();
    },
    onError: (caught) => setError((caught as Error).message),
  });

  const signalRows = signals.data?.signals ?? [];
  const aliasRows = aliases.data?.aliases ?? [];
  const exampleRows = examples.data?.examples ?? [];
  const activeAliases = aliasRows.filter((row) => row.status === 'ACTIVE').length;
  const pending = signalRows.filter((row) => row.status === 'NEW').length;

  return (
    <>
      <PageHeader title={t('learning.title')} description={t('learning.subtitle')} />

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard label={t('learning.kpiOpen')} value={String(pending)} />
        <StatCard label={t('learning.kpiActive')} value={String(activeAliases)} />
        <StatCard label={t('learning.kpiExamples')} value={String(exampleRows.length)} />
      </div>

      <div className="mt-4 flex gap-1 border-b border-slate-200 dark:border-slate-700">
        {(['queue', 'vocabulary', 'examples'] as Tab[]).map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`px-3 py-2 text-sm font-medium ${
              tab === key
                ? 'border-b-2 border-brand-600 text-brand-700 dark:text-brand-300'
                : 'text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
            }`}
          >
            {t(`learning.tab.${key}`)}
          </button>
        ))}
      </div>

      {error && (
        <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {tab === 'queue' && (
        <Section title={t('learning.queueTitle')}>
          <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
            {t('learning.queueHelp')}
          </p>
          <QueryState
            isLoading={signals.isLoading}
            error={signals.error}
            onRetry={() => void signals.refetch()}
          >
            {signalRows.length === 0 ? (
              <EmptyState message={t('learning.noSignalsBody')} />
            ) : (
              <DataTable
                tableId="agent-learning.signals"
                rows={signalRows}
                rowKey={(row) => String((row as LearningSignal).signal_id)}
                searchable
                pageSize={20}
                columns={[
                  { key: 'phrase', header: t('learning.phrase') },
                  { key: 'signal_type', header: t('learning.signalType') },
                  { key: 'occurrences', header: t('learning.occurrences') },
                  {
                    key: 'status',
                    header: t('learning.status'),
                    render: (row) => <StatusPill status={(row as LearningSignal).status} />,
                  },
                  {
                    key: 'last_seen_at',
                    header: t('learning.lastSeen'),
                    render: (row) => formatDateTime((row as LearningSignal).last_seen_at),
                  },
                  {
                    key: 'actions',
                    header: '',
                    render: (row) => {
                      const signal = row as LearningSignal;
                      return (
                        <div className="flex gap-1">
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1 text-xs"
                            onClick={() => {
                              setError(null);
                              setProposeFor(signal);
                            }}
                          >
                            <Plus size={13} /> {t('learning.teach')}
                          </button>
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1 text-xs"
                            onClick={() =>
                              decide.mutate(() =>
                                learningService.setSignalStatus(signal.signal_id, 'DISMISSED'),
                              )
                            }
                          >
                            <X size={13} /> {t('learning.dismiss')}
                          </button>
                        </div>
                      );
                    },
                  },
                ]}
              />
            )}
          </QueryState>
        </Section>
      )}

      {tab === 'vocabulary' && (
        <Section title={t('learning.vocabTitle')}>
          <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
            {t('learning.vocabHelp')}
          </p>
          <QueryState
            isLoading={aliases.isLoading}
            error={aliases.error}
            onRetry={() => void aliases.refetch()}
          >
            {aliasRows.length === 0 ? (
              <EmptyState message={t('learning.noAliasesBody')} />
            ) : (
              <DataTable
                tableId="agent-learning.aliases"
                rows={aliasRows}
                rowKey={(row) => String((row as TermAlias).alias_id)}
                searchable
                pageSize={20}
                columns={[
                  { key: 'phrase', header: t('learning.phrase') },
                  { key: 'alias_kind', header: t('learning.kind') },
                  { key: 'target', header: t('learning.means') },
                  {
                    key: 'status',
                    header: t('learning.status'),
                    render: (row) => <StatusPill status={(row as TermAlias).status} />,
                  },
                  { key: 'approved_by', header: t('learning.decidedBy') },
                  {
                    key: 'actions',
                    header: '',
                    render: (row) => {
                      const alias = row as TermAlias;
                      if (alias.status === 'ACTIVE') {
                        return (
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1 text-xs"
                            onClick={() =>
                              decide.mutate(() => learningService.retireAlias(alias.alias_id))
                            }
                          >
                            <Undo2 size={13} /> {t('learning.retire')}
                          </button>
                        );
                      }
                      if (alias.status !== 'PROPOSED') return null;
                      return (
                        <div className="flex gap-1">
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1 text-xs text-emerald-700 dark:text-emerald-400"
                            onClick={() =>
                              decide.mutate(() => learningService.approveAlias(alias.alias_id))
                            }
                          >
                            <Check size={13} /> {t('learning.approve')}
                          </button>
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1 text-xs"
                            onClick={() =>
                              decide.mutate(() => learningService.rejectAlias(alias.alias_id))
                            }
                          >
                            <X size={13} /> {t('learning.reject')}
                          </button>
                        </div>
                      );
                    },
                  },
                ]}
              />
            )}
          </QueryState>
        </Section>
      )}

      {tab === 'examples' && (
        <Section title={t('learning.examplesTitle')}>
          <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
            {t('learning.examplesHelp')}
          </p>
          <QueryState
            isLoading={examples.isLoading}
            error={examples.error}
            onRetry={() => void examples.refetch()}
          >
            {exampleRows.length === 0 ? (
              <EmptyState message={t('learning.noExamplesBody')} />
            ) : (
              <DataTable
                tableId="agent-learning.examples"
                rows={exampleRows}
                rowKey={(row) => String((row as LearningExample).example_id)}
                searchable
                pageSize={20}
                columns={[
                  { key: 'question', header: t('learning.question') },
                  { key: 'tool_name', header: t('learning.tool') },
                  { key: 'use_count', header: t('learning.used') },
                  {
                    key: 'status',
                    header: t('learning.status'),
                    render: (row) => <StatusPill status={(row as LearningExample).status} />,
                  },
                  {
                    key: 'actions',
                    header: '',
                    render: (row) => {
                      const example = row as LearningExample;
                      if (example.status === 'ACTIVE') {
                        return (
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1 text-xs"
                            onClick={() =>
                              decide.mutate(() => learningService.retireExample(example.example_id))
                            }
                          >
                            <Undo2 size={13} /> {t('learning.retire')}
                          </button>
                        );
                      }
                      if (example.status !== 'PROPOSED') return null;
                      return (
                        <div className="flex gap-1">
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1 text-xs text-emerald-700 dark:text-emerald-400"
                            onClick={() =>
                              decide.mutate(() =>
                                learningService.approveExample(example.example_id),
                              )
                            }
                          >
                            <Check size={13} /> {t('learning.approve')}
                          </button>
                          <button
                            type="button"
                            className="btn-ghost px-2 py-1 text-xs"
                            onClick={() =>
                              decide.mutate(() => learningService.rejectExample(example.example_id))
                            }
                          >
                            <X size={13} /> {t('learning.reject')}
                          </button>
                        </div>
                      );
                    },
                  },
                ]}
              />
            )}
          </QueryState>
        </Section>
      )}

      {proposeFor && options.data && (
        <ProposeDialog
          signal={proposeFor}
          kinds={options.data.alias_kinds}
          targets={options.data.alias_targets}
          tools={options.data.tools}
          busy={decide.isPending}
          onCancel={() => setProposeFor(null)}
          onSubmitAlias={(body) => decide.mutate(() => learningService.proposeAlias(body))}
          onSubmitExample={(body) => decide.mutate(() => learningService.proposeExample(body))}
        />
      )}
    </>
  );
}

interface ProposeBody {
  phrase: string;
  alias_kind: AliasKind;
  entity_type?: string | null;
  entity_code?: string | null;
  target_keyword?: string | null;
  language?: string | null;
  signal_id?: number | null;
  notes?: string | null;
}

interface ExampleBody {
  question: string;
  tool_name: string;
  language?: string | null;
}

/**
 * Teach one phrase a meaning, or pin one whole question to a tool.
 *
 * Two modes because the queue holds two kinds of problem. A word the assistant
 * did not know is a **term**; a question it understood every word of but still
 * routed badly is a **question**, and no single word would fix it.
 *
 * Either way this writes a PROPOSED row and stops: approving is a separate act,
 * in the tab for it, so that proposing and approving can be done by different
 * people — which is the whole point of having a review step.
 */
function ProposeDialog({
  signal,
  kinds,
  targets,
  tools,
  busy,
  onCancel,
  onSubmitAlias,
  onSubmitExample,
}: {
  signal: LearningSignal;
  kinds: AliasKind[];
  targets: Record<AliasKind, string[]>;
  tools: { name: string; description: string }[];
  busy: boolean;
  onCancel: () => void;
  onSubmitAlias: (body: ProposeBody) => void;
  onSubmitExample: (body: ExampleBody) => void;
}) {
  const t = useT();
  const [mode, setMode] = useState<'term' | 'question'>('term');
  const [kind, setKind] = useState<AliasKind>(kinds[0] ?? 'METRIC');
  const [keyword, setKeyword] = useState('');
  const [entityType, setEntityType] = useState('');
  const [entityCode, setEntityCode] = useState('');
  const [tool, setTool] = useState('');

  const isEntity = kind === 'ENTITY';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-xl bg-white p-4 shadow-xl dark:bg-slate-900">
        <h2 className="text-base font-semibold">{t('learning.teachTitle')}</h2>
        <p className="mt-1 text-sm text-slate-500">
          {t('learning.teachPhrase')}: <strong>{signal.phrase}</strong>
        </p>

        <div className="mt-3 flex gap-1 rounded-md bg-slate-100 p-0.5 dark:bg-slate-800">
          {(['term', 'question'] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setMode(value)}
              className={`flex-1 rounded px-2 py-1 text-xs font-medium ${
                mode === value ? 'bg-white shadow-sm dark:bg-slate-700' : 'text-slate-500'
              }`}
            >
              {t(`learning.mode.${value}`)}
            </button>
          ))}
        </div>

        {mode === 'question' ? (
          <label className="mt-3 block text-xs font-medium text-slate-500">
            {t('learning.answeredBy')}
            <select
              value={tool}
              onChange={(event) => setTool(event.target.value)}
              className="mt-1 w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
            >
              <option value="">—</option>
              {tools.map((entry) => (
                <option key={entry.name} value={entry.name}>
                  {entry.name}
                </option>
              ))}
            </select>
            <span className="mt-1 block text-[11px] font-normal text-slate-400">
              {t('learning.answeredByHelp')}
            </span>
          </label>
        ) : (
          <>
            <label className="mt-3 block text-xs font-medium text-slate-500">
              {t('learning.kind')}
              <select
                value={kind}
                onChange={(event) => {
                  setKind(event.target.value as AliasKind);
                  setKeyword('');
                }}
                className="mt-1 w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
              >
                {kinds.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>

            {mode === 'term' && isEntity ? (
              <>
                <label className="mt-3 block text-xs font-medium text-slate-500">
                  {t('learning.entityType')}
                  <select
                    value={entityType}
                    onChange={(event) => setEntityType(event.target.value)}
                    className="mt-1 w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                  >
                    <option value="">—</option>
                    {(targets.ENTITY ?? []).map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="mt-3 block text-xs font-medium text-slate-500">
                  {t('learning.entityCode')}
                  <input
                    value={entityCode}
                    onChange={(event) => setEntityCode(event.target.value)}
                    className="mt-1 w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                  />
                  {/* The backend refuses a code no master record has, and says so. */}
                  <span className="mt-1 block text-[11px] font-normal text-slate-400">
                    {t('learning.entityCodeHelp')}
                  </span>
                </label>
              </>
            ) : (
              <label className="mt-3 block text-xs font-medium text-slate-500">
                {t('learning.means')}
                <select
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                >
                  <option value="">—</option>
                  {(targets[kind] ?? []).map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button type="button" className="btn-ghost text-sm" onClick={onCancel}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn-primary text-sm"
            disabled={
              busy ||
              (mode === 'question' ? !tool : isEntity ? !entityType || !entityCode : !keyword)
            }
            onClick={() =>
              mode === 'question'
                ? onSubmitExample({
                    // The raw sample where there is one: the question as it was
                    // actually typed teaches better than the reduced key.
                    question: signal.raw_sample || signal.phrase,
                    tool_name: tool,
                    language: signal.language,
                  })
                : onSubmitAlias({
                    phrase: signal.phrase,
                    alias_kind: kind,
                    language: signal.language,
                    signal_id: signal.signal_id,
                    ...(isEntity
                      ? { entity_type: entityType, entity_code: entityCode }
                      : { target_keyword: keyword }),
                  })
            }
          >
            {t('learning.propose')}
          </button>
        </div>
      </div>
    </div>
  );
}
