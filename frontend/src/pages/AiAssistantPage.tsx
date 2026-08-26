/**
 * AI assistant.
 *
 * Talks to `POST /api/chat` from Phase 3. Conversation context is held by the
 * backend, so a follow-up like "Dhaka only" or "গত মাসে?" needs no repetition
 * from the user — the frontend only carries the `conversation_id`.
 */

import { Bot, Loader2, MessageSquarePlus, Send, User as UserIcon } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { AiResponse, AnswerText } from '../components/AiResponse';
import { AnswerFeedback } from '../components/AnswerFeedback';
import { PageHeader } from '../components/PageHeader';
import { ExportButtons } from '../components/ExportButtons';
import { useT } from '../contexts/I18nContext';
import { chatService } from '../services';
import { formatDateTime } from '../utils/format';
import type { ChatMessageResponse, FeedbackRating } from '../types/api';

interface Turn {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  /** Present for a live answer; absent for one replayed from history. */
  response?: ChatMessageResponse;
  /**
   * The stored message, which is what a rating hangs off. Present for a live
   * answer and for a replayed one alike, which is what lets both be rated; a
   * failed request never became a message and has none.
   */
  messageId?: number;
  /** A verdict this reader already recorded, replayed from history. */
  feedback?: FeedbackRating | null;
  /** A failed request, which is the only thing shown in the error style. */
  isError?: boolean;
}

// "Low stock products" was here until the material stock model replaced the
// old one. It asked two things this data cannot answer: stock per product, and
// days of cover. A suggestion the assistant has to refuse teaches the wrong
// vocabulary, so it was replaced with the question the new model does answer.
const SUGGESTIONS = [
  'আজকের sales কত?',
  'এই মাসের target achievement কত?',
  'Top 20 outstanding customer দেখাও',
  'Material wise stock দেখাও',
  'Region-wise performance দেখাও',
  'আজকের management summary দাও',
];

export default function AiAssistantPage() {
  const t = useT();
  const queryClient = useQueryClient();
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const endRef = useRef<HTMLDivElement>(null);

  const { data: conversations } = useQuery({
    queryKey: ['conversations'],
    queryFn: () => chatService.conversations(),
  });

  const ask = useMutation({
    mutationFn: (message: string) => chatService.send(message, conversationId),
    onSuccess: (response) => {
      setConversationId(response.conversation_id);
      setTurns((previous) => [
        ...previous,
        {
          id: `a-${Date.now()}`,
          role: 'assistant',
          text: response.answer,
          response,
          messageId: response.message_id ?? undefined,
        },
      ]);
      void queryClient.invalidateQueries({ queryKey: ['conversations'] });
    },
    onError: (error) => {
      setTurns((previous) => [
        ...previous,
        {
          id: `e-${Date.now()}`,
          role: 'assistant',
          text: (error as Error).message,
          isError: true,
        },
      ]);
    },
  });

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [turns, ask.isPending]);

  function submit(message: string) {
    const text = message.trim();
    if (!text || ask.isPending) return;
    setTurns((previous) => [
      ...previous,
      { id: `u-${Date.now()}`, role: 'user', text },
    ]);
    setInput('');
    ask.mutate(text);
  }

  async function openConversation(id: string) {
    const history = await chatService.history(id);
    setConversationId(id);
    setTurns(
      history.messages.map((message) => ({
        id: String(message.message_id),
        role: message.role,
        text: message.message,
        messageId: message.message_id,
        feedback: message.feedback,
      })),
    );
  }

  function newChat() {
    setConversationId(undefined);
    setTurns([]);
  }

  const lastAssistant = [...turns].reverse().find((turn) => turn.response)?.response;
  const exportRows = (lastAssistant?.data?.rows ?? []) as Record<string, unknown>[];

  return (
    <>
      <PageHeader
        title={t('ai.title')}
        actions={
          <>
            {exportRows.length > 0 && (
              <ExportButtons
                reportName={lastAssistant?.intent ?? t('ai.title')}
                rows={exportRows}
                compact
              />
            )}
            <button type="button" className="btn-secondary" onClick={newChat}>
              <MessageSquarePlus size={14} />
              {t('ai.newChat')}
            </button>
          </>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[16rem_1fr]">
        <aside className="card hidden max-h-[70vh] overflow-y-auto p-2 lg:block">
          <p className="px-2 py-1 text-[11px] font-semibold uppercase text-slate-400">
            {t('ai.conversations')}
          </p>
          {(conversations?.conversations ?? []).map((conversation) => (
            <button
              key={conversation.conversation_id}
              type="button"
              onClick={() => void openConversation(conversation.conversation_id)}
              className={`w-full rounded-lg px-2 py-2 text-left text-xs hover:bg-slate-100 dark:hover:bg-slate-800 ${
                conversation.conversation_id === conversationId
                  ? 'bg-brand-50 dark:bg-brand-950'
                  : ''
              }`}
            >
              <span className="block truncate font-medium">
                {conversation.title ?? conversation.conversation_id.slice(0, 8)}
              </span>
              <span className="block text-[10px] text-slate-400">
                {formatDateTime(conversation.last_message_at ?? conversation.started_at)}
              </span>
            </button>
          ))}
          {(conversations?.conversations?.length ?? 0) === 0 && (
            <p className="px-2 py-3 text-xs text-slate-400">{t('notifications.empty')}</p>
          )}
        </aside>

        <section className="card flex h-[70vh] flex-col">
          <div className="flex-1 space-y-4 overflow-y-auto p-4">
            {turns.length === 0 && (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <Bot size={36} className="text-brand-500" />
                <div>
                  <p className="text-sm font-semibold">{t('ai.emptyTitle')}</p>
                  <p className="mt-1 text-xs text-slate-500">{t('ai.emptyBody')}</p>
                </div>
                <div className="mt-2 flex max-w-lg flex-wrap justify-center gap-2">
                  {SUGGESTIONS.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      onClick={() => submit(suggestion)}
                      className="rounded-full border border-slate-200 px-3 py-1.5 text-xs hover:border-brand-400 hover:bg-brand-50 dark:border-slate-700 dark:hover:bg-slate-800"
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {turns.map((turn) => (
              <div
                key={turn.id}
                className={`flex gap-3 ${turn.role === 'user' ? 'justify-end' : ''}`}
              >
                {turn.role === 'assistant' && (
                  <span className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-700 dark:bg-brand-950 dark:text-brand-300">
                    <Bot size={15} />
                  </span>
                )}
                <div
                  className={`max-w-[85%] rounded-2xl px-4 py-3 ${
                    turn.role === 'user'
                      ? 'bg-brand-600 text-white'
                      : 'bg-slate-100 dark:bg-slate-800'
                  }`}
                >
                  {turn.role === 'user' ? (
                    <p className="text-sm">{turn.text}</p>
                  ) : turn.response ? (
                    <AiResponse response={turn.response} />
                  ) : turn.isError ? (
                    <p className="text-sm text-red-600 dark:text-red-400">{turn.text}</p>
                  ) : (
                    // Replayed from history: the prose is all the backend kept,
                    // so its table has to come out of the text.
                    <AnswerText text={turn.text} withTables />
                  )}
                  {/*
                    Rated by message, not by payload — so a replayed answer is
                    as rateable as a live one. A failed request never became a
                    message, so it has no id and offers no thumbs.
                  */}
                  {turn.role === 'assistant' && !turn.isError && turn.messageId ? (
                    <AnswerFeedback
                      messageId={turn.messageId}
                      initial={turn.feedback}
                    />
                  ) : null}
                </div>
                {turn.role === 'user' && (
                  <span className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-200 dark:bg-slate-700">
                    <UserIcon size={15} />
                  </span>
                )}
              </div>
            ))}

            {ask.isPending && (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 size={16} className="animate-spin" />
                {t('ai.thinking')}
              </div>
            )}
            <div ref={endRef} />
          </div>

          {turns.length > 0 && (
            <div className="flex flex-wrap gap-1.5 border-t border-slate-200 px-3 py-2 dark:border-slate-800">
              {SUGGESTIONS.slice(0, 3).map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => submit(suggestion)}
                  className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          )}

          <form
            onSubmit={(event: FormEvent) => {
              event.preventDefault();
              submit(input);
            }}
            className="flex items-center gap-2 border-t border-slate-200 p-3 dark:border-slate-800"
          >
            <input
              className="input"
              placeholder={t('ai.placeholder')}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              disabled={ask.isPending}
              aria-label={t('ai.placeholder')}
            />
            <button
              type="submit"
              className="btn-primary shrink-0"
              disabled={ask.isPending || !input.trim()}
            >
              <Send size={16} />
              <span className="hidden sm:inline">{t('ai.send')}</span>
            </button>
          </form>
        </section>
      </div>
    </>
  );
}
