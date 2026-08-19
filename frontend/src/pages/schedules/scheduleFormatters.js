/**
 * Relative day/time labels for schedule runs — "Today 02:00 AM", "Tomorrow
 * 02:00 AM", etc. Always rendered in the SCHEDULE's own configured
 * timezone (matching the "Frequency" column, e.g. "Every Day 02:00 AM"),
 * not the viewer's browser timezone — otherwise the two columns would
 * describe the same event with two different clock times.
 */
function partsInTz(date, timeZone) {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const parts = Object.fromEntries(fmt.formatToParts(date).map((p) => [p.type, p.value]));
  return { y: +parts.year, m: +parts.month, d: +parts.day };
}

export function relativeRunLabel(iso, timeZone = 'UTC') {
  if (!iso) return '—';
  const date = new Date(iso);
  const target = partsInTz(date, timeZone);
  const nowInTz = partsInTz(new Date(), timeZone);

  const dOnly = Date.UTC(target.y, target.m - 1, target.d);
  const nowOnly = Date.UTC(nowInTz.y, nowInTz.m - 1, nowInTz.d);
  const diffDays = Math.round((dOnly - nowOnly) / 86_400_000);

  const time = new Intl.DateTimeFormat('en-US', { timeZone, hour: 'numeric', minute: '2-digit', hour12: true }).format(date);
  const weekday = new Intl.DateTimeFormat('en-US', { timeZone, weekday: 'long' }).format(date);
  const monthDay = new Intl.DateTimeFormat('en-US', { timeZone, month: 'short', day: 'numeric', year: 'numeric' }).format(date);

  if (diffDays === 0) return `Today ${time}`;
  if (diffDays === 1) return `Tomorrow ${time}`;
  if (diffDays === -1) return `Yesterday ${time}`;
  if (diffDays > 1 && diffDays <= 6) return `${weekday} ${time}`;
  if (diffDays < -1 && diffDays >= -6) return `Last ${weekday} ${time}`;
  return `${monthDay} ${time}`;
}
