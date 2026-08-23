/**
 * Error boundary.
 *
 * A rendering crash shows a recoverable message instead of a blank screen. The
 * technical detail goes to the console for developers; the user sees plain
 * language and never a stack trace.
 */

import { AlertTriangle } from 'lucide-react';
import { Component, type ErrorInfo, type ReactNode } from 'react';
import en from '../i18n/en.json';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Deliberately console-only: nothing here reaches the user interface.
    console.error('Unhandled UI error', error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.hasError) return this.props.children;
    if (this.props.fallback) return this.props.fallback;

    // The boundary can render above the i18n provider, so it uses English
    // directly rather than risking a second failure inside a hook.
    return (
      <div
        role="alert"
        className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center"
      >
        <AlertTriangle size={40} className="text-red-500" />
        <div>
          <h1 className="text-lg font-semibold">{en['error.title']}</h1>
          <p className="mt-1 text-sm text-slate-500">{en['error.body']}</p>
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={() => window.location.reload()}
        >
          {en['error.reload']}
        </button>
      </div>
    );
  }
}
