/**
 * Per-row actions.
 *
 * Two or three actions are buttons; more collapse into a `⋮` menu, and on a
 * narrow screen they always do — a row of five buttons is what turns a mobile
 * table into a horizontal scroll of controls rather than of data.
 *
 * An action the user may not perform is not rendered. That is a convenience:
 * the endpoint behind each one re-resolves the same permission, so hiding it
 * saves a pointless 403 rather than providing the protection.
 */

import { MoreVertical } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useT } from '../contexts/I18nContext';

export interface RowAction {
  key: string;
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  /** Renders in red and sorts to the bottom of the menu. */
  danger?: boolean;
  disabled?: boolean;
  title?: string;
}

/** Beyond this many, the buttons become a menu. */
const INLINE_LIMIT = 3;

export function RowActions({ actions }: { actions: RowAction[] }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    function onPointerDown(event: MouseEvent) {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  if (actions.length === 0) return null;

  const inline = actions.length <= INLINE_LIMIT;

  return (
    <div ref={container} className="relative flex justify-end" data-testid="row-actions">
      {/* Buttons on a wide screen when there are few enough; the menu is the
          fallback and the only form on mobile. */}
      {inline && (
        <div className="hidden items-center gap-1 sm:flex">
          {actions.map((action) => (
            <button
              key={action.key}
              type="button"
              disabled={action.disabled}
              title={action.title ?? action.label}
              onClick={(event) => {
                event.stopPropagation();
                action.onSelect();
              }}
              className={`rounded px-2 py-1 text-xs font-medium transition-colors disabled:opacity-40 ${
                action.danger
                  ? 'text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/40'
                  : 'text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800'
              }`}
            >
              {action.label}
            </button>
          ))}
        </div>
      )}

      <div className={inline ? 'sm:hidden' : ''}>
        <button
          type="button"
          className="btn-ghost px-1.5 py-1"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={t('table.actions')}
          onClick={(event) => {
            event.stopPropagation();
            setOpen((value) => !value);
          }}
        >
          <MoreVertical size={16} />
        </button>
        {open && (
          <div
            role="menu"
            className="absolute right-0 z-30 mt-1 w-44 overflow-hidden rounded-lg border border-slate-200 bg-white py-1 shadow-lg dark:border-slate-700 dark:bg-slate-900"
          >
            {actions.map((action) => (
              <button
                key={action.key}
                type="button"
                role="menuitem"
                disabled={action.disabled}
                onClick={(event) => {
                  event.stopPropagation();
                  setOpen(false);
                  action.onSelect();
                }}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm disabled:opacity-40 ${
                  action.danger
                    ? 'text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/40'
                    : 'hover:bg-slate-100 dark:hover:bg-slate-800'
                }`}
              >
                {action.icon}
                {action.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
