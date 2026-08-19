/**
 * Relative time label for incident timestamps — "10 mins ago", "3 hours
 * ago", matching the spec's table example. Local to Incidents, same
 * pattern as pages/schedules/scheduleFormatters.js (a shared formatters.js
 * helper wasn't there already, so this stays scoped to the module that
 * needs it rather than growing an unrelated shared file).
 */
export function relativeTime(iso) {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diffSec = Math.max(0, Math.round((now - then) / 1000));

  if (diffSec < 60) return 'Just now';
  const mins = Math.round(diffSec / 60);
  if (mins < 60) return `${mins} min${mins === 1 ? '' : 's'} ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}
