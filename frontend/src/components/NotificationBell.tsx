/**
 * Notification dropdown: unread count, read/unread items, mark-as-read.
 */

import { Bell, CheckCheck } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useT } from '../contexts/I18nContext';
import { alertService } from '../services';
import { formatDateTime, severityClass } from '../utils/format';

export function NotificationBell() {
  const t = useT();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const { data } = useQuery({
    queryKey: ['notifications'],
    queryFn: () => alertService.notifications(),
    refetchInterval: 60 * 1000,
  });

  const markRead = useMutation({
    mutationFn: (id: number) => alertService.markRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  });
  const markAll = useMutation({
    mutationFn: () => alertService.markAllRead(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  });

  useEffect(() => {
    function onPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, []);

  const unread = data?.unread_count ?? 0;
  const notifications = data?.notifications ?? [];

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        className="btn-ghost relative px-2"
        onClick={() => setOpen((value) => !value)}
        aria-label={`${t('notifications.title')}${unread ? ` (${unread} ${t('notifications.unread')})` : ''}`}
        aria-expanded={open}
      >
        <Bell size={18} />
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-600 px-1 text-[10px] font-semibold text-white">
            {unread > 9 ? '9+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-30 mt-1 w-80 max-w-[90vw] overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-900">
          <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2 dark:border-slate-800">
            <span className="text-sm font-semibold">{t('notifications.title')}</span>
            {unread > 0 && (
              <button
                type="button"
                className="inline-flex items-center gap-1 text-xs text-brand-600 hover:underline"
                onClick={() => markAll.mutate()}
              >
                <CheckCheck size={12} />
                {t('notifications.markAllRead')}
              </button>
            )}
          </div>

          <div className="max-h-80 overflow-y-auto">
            {notifications.length === 0 && (
              <p className="px-3 py-6 text-center text-xs text-slate-500">
                {t('notifications.empty')}
              </p>
            )}
            {notifications.map((notification) => (
              <button
                key={notification.notification_id}
                type="button"
                onClick={() => {
                  if (!notification.is_read) markRead.mutate(notification.notification_id);
                  if (notification.link) navigate(notification.link);
                  setOpen(false);
                }}
                className={`flex w-full flex-col gap-1 border-b border-slate-100 px-3 py-2 text-left last:border-0 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-800/60 ${
                  notification.is_read ? 'opacity-70' : ''
                }`}
              >
                <span className="flex items-center gap-2">
                  <span className={`badge ${severityClass(notification.severity)}`}>
                    {notification.severity}
                  </span>
                  <span className="truncate text-sm font-medium">{notification.title}</span>
                </span>
                {notification.body && (
                  <span className="line-clamp-2 text-xs text-slate-500">
                    {notification.body}
                  </span>
                )}
                <span className="text-[11px] text-slate-400">
                  {formatDateTime(notification.created_at)}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
