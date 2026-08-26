/**
 * Thumbs up / down on one answer, with an optional note.
 *
 * Kept out of `AiResponse` on purpose: that component renders the *payload* of a
 * live answer, while this is about the stored message and has to appear on
 * replayed turns too, which carry prose and a message id but no payload. Both
 * paths render this the same way.
 *
 * A note is only asked for on a thumbs-down. A reader who liked the answer has
 * nothing to correct, and asking anyway is the kind of friction that stops
 * people rating at all — which would cost the signal this whole feature exists
 * to collect.
 */

import { Check, Loader2, ThumbsDown, ThumbsUp } from 'lucide-react';
import { useState } from 'react';
import { useT } from '../contexts/I18nContext';
import { chatService } from '../services';
import type { FeedbackRating } from '../types/api';

interface Props {
  messageId: number;
  /** A verdict already recorded by this reader, from history. */
  initial?: FeedbackRating | null;
}

export function AnswerFeedback({ messageId, initial = null }: Props) {
  const t = useT();
  const [rating, setRating] = useState<FeedbackRating | null>(initial);
  const [noteOpen, setNoteOpen] = useState(false);
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [sanitized, setSanitized] = useState(false);
  const [failed, setFailed] = useState(false);

  async function send(next: FeedbackRating, expected?: string) {
    setSaving(true);
    setFailed(false);
    // Optimistic, because the thumb is the whole interaction: waiting a round
    // trip to fill it in reads as a dead button.
    setRating(next);
    try {
      const result = await chatService.feedback(messageId, next, expected);
      setSanitized(result.sanitized);
      if (expected !== undefined) {
        setSaved(true);
        setNoteOpen(false);
      }
    } catch {
      setRating(initial);
      setFailed(true);
    } finally {
      setSaving(false);
    }
  }

  function choose(next: FeedbackRating) {
    if (saving) return;
    void send(next);
    // Only a thumbs-down opens the note box, and only the first time: a reader
    // who already left one is not asked again.
    setNoteOpen(next === 'DOWN' && !saved);
  }

  const buttonClass = (value: FeedbackRating) =>
    [
      'inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors',
      rating === value
        ? 'bg-slate-200 text-slate-900 dark:bg-slate-700 dark:text-slate-100'
        : 'text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-300',
    ].join(' ');

  return (
    <div className="mt-2 border-t border-slate-100 pt-2 dark:border-slate-800">
      <div className="flex items-center gap-1">
        <span className="mr-1 text-[11px] text-slate-400">
          {t('ai.feedbackPrompt')}
        </span>
        <button
          type="button"
          className={buttonClass('UP')}
          onClick={() => choose('UP')}
          aria-label={t('ai.feedbackUp')}
          aria-pressed={rating === 'UP'}
          disabled={saving}
        >
          <ThumbsUp size={14} />
        </button>
        <button
          type="button"
          className={buttonClass('DOWN')}
          onClick={() => choose('DOWN')}
          aria-label={t('ai.feedbackDown')}
          aria-pressed={rating === 'DOWN'}
          disabled={saving}
        >
          <ThumbsDown size={14} />
        </button>
        {saving && <Loader2 size={12} className="animate-spin text-slate-400" />}
        {saved && !saving && (
          <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600 dark:text-emerald-400">
            <Check size={12} />
            {t('ai.feedbackThanks')}
          </span>
        )}
        {failed && (
          <span className="text-[11px] text-red-600 dark:text-red-400">
            {t('ai.feedbackFailed')}
          </span>
        )}
      </div>

      {noteOpen && (
        <div className="mt-2 space-y-2">
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            rows={2}
            maxLength={2000}
            placeholder={t('ai.feedbackNotePlaceholder')}
            className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-700 placeholder:text-slate-400 focus:border-slate-400 focus:outline-none dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void send('DOWN', note.trim() || undefined)}
              disabled={saving}
              className="rounded-md bg-slate-800 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-slate-200 dark:text-slate-900 dark:hover:bg-white"
            >
              {t('ai.feedbackSubmit')}
            </button>
            <button
              type="button"
              onClick={() => setNoteOpen(false)}
              className="text-[11px] text-slate-400 hover:text-slate-600 dark:hover:text-slate-300"
            >
              {t('ai.feedbackSkip')}
            </button>
          </div>
        </div>
      )}

      {sanitized && (
        // Said plainly rather than silently storing something other than what
        // was typed.
        <p className="mt-1.5 text-[11px] text-amber-600 dark:text-amber-400">
          {t('ai.feedbackSanitized')}
        </p>
      )}
    </div>
  );
}
