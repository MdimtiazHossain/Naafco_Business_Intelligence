import { useEffect, useState } from 'react';

/**
 * Delay a rapidly changing value.
 *
 * Used for search boxes so a request is issued once the user pauses rather than
 * on every keystroke.
 */
export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return debounced;
}
