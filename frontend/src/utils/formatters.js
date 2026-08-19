/** Shared formatters — pure functions, no side-effects */

/** "2024-05-18T10:30:00Z" → "May 18, 2024" */
export function formatDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric',
  });
}

/** "2024-05-18T10:30:00Z" → "May 18, 2024 at 10:30 AM" */
export function formatDateTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

/** 1234567 → "1,234,567" */
export function formatNumber(n) {
  if (n == null) return '—';
  return new Intl.NumberFormat('en-US').format(n);
}

/** 0.9823 → "98.2%" */
export function formatPercent(ratio, decimals = 1) {
  if (ratio == null) return '—';
  return `${(ratio * 100).toFixed(decimals)}%`;
}

/** bytes → "1.2 MB" */
export function formatFileSize(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(1)} ${units[i]}`;
}

/** seconds → "1h 23m 45s" (compact) */
export function formatDuration(seconds) {
  if (!seconds) return '0s';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return [h && `${h}h`, m && `${m}m`, `${s}s`].filter(Boolean).join(' ');
}

/** "hello_world" → "Hello World" */
export function snakeToTitle(str) {
  return str
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** "Hello World" → "HW" (initials) */
export function initials(name = '') {
  return name.split(' ').slice(0, 2).map((w) => w[0] ?? '').join('').toUpperCase();
}

/** Truncate long strings */
export function truncate(str, max = 60) {
  if (!str) return '';
  return str.length > max ? `${str.slice(0, max)}…` : str;
}
