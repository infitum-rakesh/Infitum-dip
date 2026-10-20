import { useEffect, useState } from 'react';

/**
 * Real-time match against a CSS media query (e.g. '(max-width: 1024px)') —
 * re-evaluates on viewport resize/orientation change via matchMedia's own
 * change event, never a one-time check at mount (so rotating a tablet or
 * resizing a browser window updates layout live, not just on next reload).
 */
export function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => (
    typeof window !== 'undefined' && 'matchMedia' in window ? window.matchMedia(query).matches : false
  ));

  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [query]);

  return matches;
}
