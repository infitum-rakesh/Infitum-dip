import { Outlet } from 'react-router-dom';
import './AuthLayout.css';

export function AuthLayout() {
  return (
    <div className="auth-shell">
      <div className="auth-brand-panel">
        <div className="auth-brand-content">
          <a href="/" className="auth-brand-logo auth-brand-logo-link" aria-label="Back to home">
            <div className="auth-brand-mark">DI</div>
            <span className="auth-brand-name">DataIntelligence</span>
          </a>
          <h2 className="auth-brand-title">
            Transform Any Data Into<br />
            <span className="auth-brand-gradient">Trusted Intelligence</span>
          </h2>
          <p className="auth-brand-sub">
            AI-powered document processing, data transformation,
            validation and reconciliation in one enterprise platform.
          </p>
          {/* Pipeline visualization */}
          <div className="auth-pipeline-viz">
            {[
              { icon: '📄', label: 'Documents' },
              { icon: '🔍', label: 'OCR + AI' },
              { icon: '⚙️', label: 'Transform' },
              { icon: '✅', label: 'Validate' },
              { icon: '🎯', label: 'Target' },
            ].map((step, i, arr) => (
              <div key={step.label} className="viz-step-wrap">
                <div className="viz-step">
                  <div className="viz-icon">{step.icon}</div>
                  <span className="viz-label">{step.label}</span>
                </div>
                {i < arr.length - 1 && <div className="viz-arrow">→</div>}
              </div>
            ))}
          </div>

          <div className="auth-stats">
            {[
              { val: '10M+', label: 'Documents processed' },
              { val: '99.2%', label: 'OCR accuracy' },
              { val: '500+', label: 'Enterprise customers' },
            ].map((s) => (
              <div key={s.label} className="auth-stat">
                <span className="auth-stat-val">{s.val}</span>
                <span className="auth-stat-label">{s.label}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="auth-form-panel">
        <Outlet />
      </div>
    </div>
  );
}
