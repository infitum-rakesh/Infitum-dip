import { useState, useCallback } from 'react';

/**
 * Generic async API hook.
 * Usage:
 *   const { data, loading, error, run } = useApi(myService.fetchItems);
 *   useEffect(() => { run(params); }, []);
 */
export function useApi(apiFn) {
  const [data, setData]       = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState(null);

  const run = useCallback(async (...args) => {
    setLoading(true);
    setError(null);
    try {
      const result = await apiFn(...args);
      setData(result);
      return { ok: true, data: result };
    } catch (err) {
      const msg = err.message || 'Request failed.';
      setError(msg);
      return { ok: false, error: msg };
    } finally {
      setLoading(false);
    }
  }, [apiFn]);

  const reset = useCallback(() => {
    setData(null);
    setError(null);
    setLoading(false);
  }, []);

  return { data, loading, error, run, reset };
}
