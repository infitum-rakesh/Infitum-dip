import { Button } from '@/components/common/Button.jsx';

/**
 * PlaceholderPage — used for routes not yet implemented.
 * Each module will replace this with its real implementation.
 */
export function PlaceholderPage({ title, description, icon = '🚧', phase, actions = [] }) {
  return (
    <div className="placeholder-page">
      <div className="placeholder-inner">
        <div className="placeholder-icon">{icon}</div>
        <h2 className="placeholder-title">{title}</h2>
        {description && <p className="placeholder-desc">{description}</p>}
        {phase && (
          <div className="placeholder-phase">
            Implementation: <strong>{phase}</strong>
          </div>
        )}
        {actions.length > 0 && (
          <div className="placeholder-actions">
            {actions.map((action, i) => (
              <Button key={i} variant={action.variant || 'secondary'} onClick={action.onClick}>
                {action.label}
              </Button>
            ))}
          </div>
        )}
      </div>

      <style>{`
        .placeholder-page { display: flex; align-items: center; justify-content: center; min-height: 400px; padding: 48px; }
        .placeholder-inner { text-align: center; max-width: 480px; }
        .placeholder-icon { font-size: 52px; margin-bottom: 20px; }
        .placeholder-title { font-size: var(--fs-3xl); font-weight: var(--fw-bold); color: var(--text-primary); margin-bottom: 10px; }
        .placeholder-desc { font-size: var(--fs-md); color: var(--text-muted); line-height: 1.7; margin-bottom: 16px; }
        .placeholder-phase { display: inline-block; background: var(--color-primary-light); color: var(--color-primary); font-size: var(--fs-sm); padding: 4px 12px; border-radius: var(--radius-full); margin-bottom: 20px; }
        .placeholder-actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
      `}</style>
    </div>
  );
}
