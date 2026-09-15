import { useEffect, useState } from "react";

/**
 * Returns `value` once it has stopped changing for `delayMs`.
 *
 * Only work keyed on the returned value is delayed: the caller still renders
 * the live value, so an input stays responsive while an expensive request
 * waits for typing to pause.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);

  return debounced;
}
