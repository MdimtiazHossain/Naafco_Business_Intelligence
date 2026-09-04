/**
 * The reason a map write was refused, as the server phrased it.
 *
 * A composition refusal answers 409 with `{error_code, message}` in `detail`;
 * `apiClient` maps only a string `detail` to the error's message, so the
 * object shape is unwrapped here. The message is the whole value of the
 * refusal to the person editing — "Sub-Territory has no boundary source" tells
 * them what to change — so it is shown verbatim rather than summarised.
 */

import { ApiError } from '../../services';

export function refusalMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const detail = (error.detail as { detail?: { message?: unknown } } | undefined)?.detail;
    if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
      return detail.message;
    }
    return error.message || fallback;
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}
