/**
 * A dialog.
 *
 * Native `<dialog>` is avoided deliberately: its top-layer rendering ignores the
 * app's stacking context, and its default backdrop cannot be themed alongside
 * the rest of the palette. What is *not* skipped is the behaviour a dialog owes
 * the user — Escape closes it, focus moves into it and is trapped while it is
 * open, the page behind it stops scrolling, and it is announced as a modal.
 */

import { X } from 'lucide-react';
import { useEffect, useRef, type ReactNode } from 'react';
import { useT } from '../contexts/I18nContext';

export interface ModalProps {
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** `lg` for forms, `md` for confirmations. */
  size?: 'md' | 'lg';
  /**
   * Where the panel sits. `center` is the dialog every form uses; `sheet` docks
   * it to the right edge, full height.
   *
   * A sheet is right when the dialog is a *detail view* of a row in a table
   * behind it — an invoice, opened from the invoice list. Keeping the table
   * visible alongside is the point: a reader comparing three invoices reads the
   * list and the detail together, and a centred dialog covers the row it came
   * from.
   *
   * Behaviour is identical either way — Escape, the focus trap, the scroll lock
   * and the overlay click are all shared, because they are what a dialog owes
   * the user regardless of where it is drawn. Only the placement classes differ.
   */
  placement?: 'center' | 'sheet';
}

const WIDTH = { md: 'max-w-lg', lg: 'max-w-3xl' } as const;

/**
 * How the overlay lays its panel out, and how the panel is capped.
 *
 * The sheet stays a full-height column on a wide screen and falls back to the
 * ordinary centred behaviour below `sm`: a 400px-wide phone has no room for a
 * side panel, and pinning one to the edge there would leave a sliver of table
 * nobody can read.
 */
const PLACEMENT = {
  center: {
    overlay: 'flex items-start justify-center p-3 sm:items-center sm:p-4',
    panel: 'max-h-[calc(100dvh-2rem)] w-full my-auto',
  },
  sheet: {
    overlay: 'flex items-start justify-center p-3 sm:items-stretch sm:justify-end sm:p-0',
    panel:
      'max-h-[calc(100dvh-2rem)] w-full my-auto'
      + ' sm:my-0 sm:h-full sm:max-h-none sm:rounded-none sm:border-y-0 sm:border-r-0',
  },
} as const;

export function Modal({
  open,
  title,
  description,
  onClose,
  children,
  footer,
  size = 'md',
  placement = 'center',
}: ModalProps) {
  const t = useT();
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;

    const previouslyFocused = document.activeElement as HTMLElement | null;
    const { overflow } = document.body.style;
    document.body.style.overflow = 'hidden';

    function focusables(): HTMLElement[] {
      if (!panel.current) return [];
      return Array.from(
        panel.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.offsetParent !== null);
    }

    focusables()[0]?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      // Trap: without this, tabbing walks into the page behind the dialog,
      // where every control is inert to the eye but not to the keyboard.
      const items = focusables();
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.body.style.overflow = overflow;
      previouslyFocused?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className={`fixed inset-0 z-50 overflow-y-auto bg-slate-900/50 ${PLACEMENT[placement].overlay}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        /*
          Capped to the viewport and laid out as a column, so the header and the
          footer are always reachable and only the body scrolls.

          Without the cap a dialog whose header carries a long description grew
          past the bottom of a short phone screen and took its footer — the Save
          and Cancel buttons — with it. `100dvh` rather than `100vh` because a
          mobile browser's address bar is part of `vh` but not of the space the
          page can actually use.
        */
        className={`card flex flex-col ${WIDTH[size]} ${PLACEMENT[placement].panel} shadow-xl`}
      >
        <div className="card-header shrink-0">
          <div className="min-w-0">
            <h2 className="card-title">{title}</h2>
            {description && (
              <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            className="btn-ghost px-2 py-1"
            onClick={onClose}
            aria-label={t('common.cancel')}
          >
            <X size={16} />
          </button>
        </div>
        {/* `min-h-0` lets this flex child shrink below its content so the
            scrollbar lands here rather than on the dialog. */}
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
        {footer && (
          <div className="flex shrink-0 flex-wrap justify-end gap-2 border-t border-slate-200 p-3 dark:border-slate-800">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
