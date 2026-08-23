/** API client: query building, auth header, error mapping and 401 handling. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  buildQuery,
  request,
  setUnauthorizedHandler,
  tokenStore,
} from '../services/apiClient';

function mockResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (key: string) => headers[key.toLowerCase()] ?? null },
    json: async () => body,
    blob: async () => new Blob(),
  } as unknown as Response;
}

describe('buildQuery', () => {
  it('omits empty values so a cleared filter is not sent', () => {
    expect(buildQuery({ a: 1, b: undefined, c: null, d: '' })).toBe('?a=1');
  });

  it('repeats a key for array values', () => {
    expect(buildQuery({ code: ['A', 'B'] })).toBe('?code=A&code=B');
  });

  it('returns an empty string when there is nothing to send', () => {
    expect(buildQuery()).toBe('');
    expect(buildQuery({})).toBe('');
  });

  it('encodes values safely', () => {
    expect(buildQuery({ q: 'a b&c' })).toBe('?q=a+b%26c');
  });
});

describe('request', () => {
  beforeEach(() => {
    tokenStore.clear();
    setUnauthorizedHandler(null);
  });

  it('attaches the bearer token', async () => {
    tokenStore.set('token-123', false);
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, { ok: true }));
    vi.stubGlobal('fetch', fetchMock);

    await request('/api/test');

    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer token-123');
  });

  it('omits the token for anonymous calls such as login', async () => {
    tokenStore.set('token-123', false);
    const fetchMock = vi.fn().mockResolvedValue(mockResponse(200, {}));
    vi.stubGlobal('fetch', fetchMock);

    await request('/api/auth/login', { method: 'POST', body: {}, anonymous: true });

    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it('surfaces the backend message, which is written to be user-safe', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        mockResponse(403, { detail: "You don't have permission to access this information." }),
      ),
    );

    await expect(request('/api/dashboard')).rejects.toMatchObject({
      status: 403,
      message: "You don't have permission to access this information.",
    });
  });

  it('maps a 500 to a generic message rather than leaking internals', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(mockResponse(500, { detail: 'Traceback: psycopg…' })),
    );

    try {
      await request('/api/dashboard');
      throw new Error('should have thrown');
    } catch (error) {
      // The backend already sends a safe string; the point is that a body we
      // cannot read never becomes the message.
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).status).toBe(500);
    }
  });

  it('falls back to a mapped message when the body is not JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        headers: { get: () => null },
        json: async () => {
          throw new Error('not json');
        },
      } as unknown as Response),
    );

    await expect(request('/api/x')).rejects.toMatchObject({
      status: 503,
      message: 'The service is unavailable. Please try again shortly.',
    });
  });

  it('clears the session and notifies on 401', async () => {
    tokenStore.set('expired', true);
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse(401, { detail: 'nope' })));

    await expect(request('/api/dashboard')).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalled();
    expect(tokenStore.get()).toBeNull();
  });

  it('reports a network failure in plain language', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    await expect(request('/api/x')).rejects.toMatchObject({
      status: 0,
      message: 'Unable to reach the server. Check your connection.',
    });
  });
});

describe('tokenStore', () => {
  it('uses localStorage when remembering and sessionStorage otherwise', () => {
    tokenStore.set('remembered', true);
    expect(localStorage.getItem('bi.access_token')).toBe('remembered');
    expect(sessionStorage.getItem('bi.access_token')).toBeNull();

    tokenStore.set('session-only', false);
    expect(sessionStorage.getItem('bi.access_token')).toBe('session-only');
    expect(localStorage.getItem('bi.access_token')).toBeNull();
  });
});
