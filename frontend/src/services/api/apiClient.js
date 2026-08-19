/**
 * Axios instance — all API calls in the app must go through this.
 * Interceptors handle: auth headers, token refresh, error normalisation.
 */
import axios from 'axios';
import { STORAGE_KEYS } from '@/utils/constants.js';

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api';

const apiClient = axios.create({
  baseURL: BASE_URL,
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
});

// ── Request interceptor — attach access token ────────────────
apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN);
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// ── Response interceptor — normalise errors + refresh ────────
let isRefreshing = false;
let failedQueue = [];

const processQueue = (error, token = null) => {
  failedQueue.forEach((prom) => (error ? prom.reject(error) : prom.resolve(token)));
  failedQueue = [];
};

apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;

    if (error.response?.status === 401 && !original._retry) {
      const refreshToken = localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN);

      if (!refreshToken) {
        _clearSession();
        return Promise.reject(normaliseError(error));
      }

      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject });
        }).then((token) => {
          original.headers.Authorization = `Bearer ${token}`;
          return apiClient(original);
        });
      }

      original._retry = true;
      isRefreshing = true;

      try {
        const { data } = await axios.post(`${BASE_URL}/auth/refresh`, {
          refresh_token: refreshToken,
        });
        localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, data.access_token);
        localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, data.refresh_token);
        processQueue(null, data.access_token);
        original.headers.Authorization = `Bearer ${data.access_token}`;
        return apiClient(original);
      } catch (refreshError) {
        processQueue(refreshError, null);
        _clearSession();
        return Promise.reject(normaliseError(refreshError));
      } finally {
        isRefreshing = false;
      }
    }

    return Promise.reject(normaliseError(error));
  }
);

function _clearSession() {
  Object.values(STORAGE_KEYS).forEach((k) => localStorage.removeItem(k));
  window.location.href = '/login';
}

/**
 * FastAPI's `detail` is usually a string, but on a 422 it's a *list* of
 * pydantic error objects ({type, loc, msg, input, ctx}), and some paths
 * return a single such object. Either shape rendered directly as a React
 * child crashes with "Objects are not valid as a React child" — always
 * reduce it down to a plain string here instead.
 */
function extractDetailMessage(detail) {
  if (!detail) return null;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === 'string' ? d : d?.msg ?? JSON.stringify(d)))
      .filter(Boolean)
      .join('; ') || null;
  }
  if (typeof detail === 'object') return detail.msg ?? JSON.stringify(detail);
  return String(detail);
}

/** Converts Axios errors into a consistent shape for the UI */
export function normaliseError(error) {
  if (error.response) {
    const { status, data } = error.response;
    const message = extractDetailMessage(data?.detail)
      || (typeof data?.message === 'string' ? data.message : null)
      || 'An unexpected error occurred.';
    return { status, message, data: data || null };
  }
  if (error.request) {
    return { status: 0, message: 'No response from server. Check your connection.', data: null };
  }
  return { status: -1, message: error.message || 'Request failed.', data: null };
}

export default apiClient;
