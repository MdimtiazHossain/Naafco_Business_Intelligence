/**
 * The single HTTP client. Every service goes through here; no component ever
 * calls `fetch` itself.
 *
 * Responsibilities kept in one place: the base URL, the bearer token, JSON
 * encoding, and turning an HTTP failure into an `ApiError` that carries a
 * message safe to show a user. Backend detail strings are already user-safe by
 * design, so they are surfaced; anything unexpected falls back to a generic
 * message rather than leaking a stack trace.
 */

const RAW_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
export const API_BASE_URL = RAW_BASE.replace(/\/$/, '');

const TOKEN_KEY = 'bi.access_token';

export class ApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }

  /** True when the session is gone and the user must sign in again. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

/** Messages for statuses where the backend text is not user-facing. */
const STATUS_MESSAGES: Record<number, string> = {
  401: 'Your session has expired. Please sign in again.',
  403: "You don't have permission to access this information.",
  404: 'Report not found.',
  408: 'The request took too long. Please try again.',
  429: 'Too many requests. Please wait a moment and try again.',
  500: 'Something went wrong. Please try again.',
  502: 'The service is unavailable. Please try again shortly.',
  503: 'The service is unavailable. Please try again shortly.',
};

export const tokenStore = {
  get(): string | null {
    try {
      return localStorage.getItem(TOKEN_KEY) ?? sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(token: string, remember: boolean): void {
    try {
      const store = remember ? localStorage : sessionStorage;
      store.setItem(TOKEN_KEY, token);
      (remember ? sessionStorage : localStorage).removeItem(TOKEN_KEY);
    } catch {
      /* storage can be unavailable in private mode; the session stays in memory */
    }
  },
  clear(): void {
    try {
      localStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* nothing to clear */
    }
  },
};

/** Called when a request comes back 401, so the app can return to login. */
type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

/**
 * Query parameters.
 *
 * Typed as `object` rather than `Record<string, unknown>` so that a plain
 * interface (`ReportQuery`, `GlobalFilters`) can be passed directly — TypeScript
 * does not consider an interface assignable to an index signature.
 */
export type QueryParams = object;

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  params?: QueryParams;
  signal?: AbortSignal;
  /** Skip the automatic sign-out on 401 (used by the login call itself). */
  anonymous?: boolean;
}

export function buildQuery(params?: QueryParams): string {
  if (!params) return '';
  const search = new URLSearchParams();
  Object.entries(params as Record<string, unknown>).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    if (Array.isArray(value)) {
      value.forEach((item) => item != null && search.append(key, String(item)));
    } else {
      search.append(key, String(value));
    }
  });
  const query = search.toString();
  return query ? `?${query}` : '';
}

async function toApiError(response: Response): Promise<ApiError> {
  let detail: unknown;
  let message = STATUS_MESSAGES[response.status] ?? 'Something went wrong. Please try again.';
  try {
    const body = await response.json();
    detail = body;
    // FastAPI puts the user-safe text in `detail`; a 422 gives an array.
    if (typeof body?.detail === 'string') {
      message = body.detail;
    } else if (Array.isArray(body?.detail) && body.detail.length > 0) {
      message = 'Please check the values you entered.';
    }
  } catch {
    /* a non-JSON error body tells us nothing useful; keep the mapped message */
  }
  return new ApiError(response.status, message, detail);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, params, signal, anonymous = false } = options;
  const token = tokenStore.get();

  const headers: Record<string, string> = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (token && !anonymous) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}${buildQuery(params)}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') throw error;
    throw new ApiError(0, 'Unable to reach the server. Check your connection.');
  }

  if (response.status === 401 && !anonymous) {
    tokenStore.clear();
    onUnauthorized?.();
  }
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;

  return (await response.json()) as T;
}

/**
 * Send `multipart/form-data` — file uploads.
 *
 * The Content-Type header is deliberately *not* set: the browser must add it
 * itself so it can append the multipart boundary. Setting it by hand produces a
 * body the server cannot parse.
 */
export async function requestForm<T>(
  path: string,
  form: FormData,
  options: { method?: 'POST' | 'PATCH'; params?: QueryParams; signal?: AbortSignal } = {},
): Promise<T> {
  const { method = 'POST', params, signal } = options;
  const token = tokenStore.get();

  const headers: Record<string, string> = { Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}${buildQuery(params)}`, {
      method,
      headers,
      body: form,
      signal,
    });
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') throw error;
    throw new ApiError(0, 'Unable to reach the server. Check your connection.');
  }

  if (response.status === 401) {
    tokenStore.clear();
    onUnauthorized?.();
  }
  if (!response.ok) throw await toApiError(response);
  return (await response.json()) as T;
}

/**
 * Send `multipart/form-data` and report how much of it has gone.
 *
 * `fetch` cannot do this: it exposes a stream for the *response* but nothing for
 * the request body, so the only way to know how many bytes have actually left
 * the browser is `XMLHttpRequest.upload.onprogress`. That is the whole reason
 * this exists alongside `requestForm` — the figure it reports is measured, not
 * estimated, which is what the upload progress indicator promises.
 *
 * `onProgress` receives 0-100 for the bytes sent. It reaches 100 when the last
 * byte is handed to the network, which is when the *server's* work begins — the
 * caller polls the job endpoint for that half.
 */
export function requestFormWithProgress<T>(
  path: string,
  form: FormData,
  options: {
    params?: QueryParams;
    signal?: AbortSignal;
    onProgress?: (percent: number) => void;
  } = {},
): Promise<T> {
  const { params, signal, onProgress } = options;
  const token = tokenStore.get();

  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE_URL}${path}${buildQuery(params)}`);
    xhr.setRequestHeader('Accept', 'application/json');
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    // Deliberately no Content-Type: the browser must set it so the multipart
    // boundary matches the body it builds.

    if (onProgress) {
      xhr.upload.onprogress = (event) => {
        // `lengthComputable` is false for a body of unknown size; reporting a
        // number there would be a guess, so nothing is emitted instead.
        if (event.lengthComputable && event.total > 0) {
          onProgress(Math.round((event.loaded / event.total) * 100));
        }
      };
    }

    xhr.onload = () => {
      if (xhr.status === 401) {
        tokenStore.clear();
        onUnauthorized?.();
      }
      let body: unknown;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = undefined;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
        return;
      }
      const detail = (body as { detail?: unknown } | undefined)?.detail;
      const message =
        typeof detail === 'string'
          ? detail
          : (STATUS_MESSAGES[xhr.status] ?? 'Something went wrong. Please try again.');
      reject(new ApiError(xhr.status, message, body));
    };

    xhr.onerror = () =>
      reject(new ApiError(0, 'Unable to reach the server. Check your connection.'));
    xhr.ontimeout = () =>
      reject(new ApiError(408, 'The request took too long. Please try again.'));
    xhr.onabort = () => {
      const error = new Error('Aborted');
      error.name = 'AbortError';
      reject(error);
    };

    if (signal) {
      if (signal.aborted) {
        xhr.abort();
        return;
      }
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }

    xhr.send(form);
  });
}

/** Download an attachment (exports, templates, error reports). */
export async function requestBlob(
  path: string,
  options: RequestOptions = {},
): Promise<{ blob: Blob; filename: string }> {
  const { method = 'POST', body, params } = options;
  const token = tokenStore.get();

  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(`${API_BASE_URL}${path}${buildQuery(params)}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401) {
    tokenStore.clear();
    onUnauthorized?.();
  }
  if (!response.ok) throw await toApiError(response);

  const disposition = response.headers.get('content-disposition') ?? '';
  const match = /filename="?([^"]+)"?/.exec(disposition);
  return { blob: await response.blob(), filename: match?.[1] ?? 'report' };
}
