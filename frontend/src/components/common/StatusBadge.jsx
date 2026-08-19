/**
 * StatusBadge — consistent status colours across the app.
 * variant: 'success'|'warning'|'danger'|'info'|'primary'|'gray'
 * dot: show status dot prefix
 */
export function StatusBadge({ children, variant = 'gray', dot = false, size = 'sm', className = '' }) {
  return (
    <span className={`status-badge status-badge-${variant} status-badge-${size} ${className}`}>
      {dot && <span className="status-dot" aria-hidden="true" />}
      {children}
    </span>
  );
}
