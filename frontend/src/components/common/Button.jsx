import './Button.css';

/**
 * Button — reusable across the entire application.
 * Props:
 *   variant:  'primary' | 'secondary' | 'ghost' | 'danger' | 'success' | 'warning'
 *   size:     'sm' | 'md' | 'lg'
 *   loading:  boolean
 *   icon:     ReactNode (left icon)
 *   iconRight:ReactNode (right icon)
 *   full:     boolean (full width)
 */
export function Button({
  children,
  variant = 'primary',
  size = 'md',
  loading = false,
  icon,
  iconRight,
  full = false,
  className = '',
  disabled,
  ...rest
}) {
  return (
    <button
      className={[
        'btn',
        `btn-${variant}`,
        `btn-${size}`,
        full ? 'btn-full' : '',
        loading ? 'btn-loading' : '',
        className,
      ].filter(Boolean).join(' ')}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? (
        <span className="btn-spinner" aria-hidden="true" />
      ) : (
        icon && <span className="btn-icon-left">{icon}</span>
      )}
      {children && <span className="btn-label">{children}</span>}
      {iconRight && !loading && <span className="btn-icon-right">{iconRight}</span>}
    </button>
  );
}
