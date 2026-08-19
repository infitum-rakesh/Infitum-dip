export function LoadingSpinner({ size = 'md', label = 'Loading…' }) {
  const px = size === 'sm' ? 16 : size === 'lg' ? 36 : 24;
  return (
    <div className="loading-spinner-wrap" role="status" aria-label={label}>
      <span
        className="loading-spinner"
        style={{ width: px, height: px, borderWidth: size === 'sm' ? 2 : 3 }}
      />
    </div>
  );
}

export function FullPageSpinner() {
  return (
    <div className="full-page-spinner">
      <LoadingSpinner size="lg" />
    </div>
  );
}
