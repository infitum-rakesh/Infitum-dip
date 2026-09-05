/**
 * AnomalyConfiguration — /app/anomaly/configuration
 * Org-wide defaults for new Anomaly Jobs: scoring bands, retention,
 * baseline source, auto-incident-on-critical. Backed by the real
 * GET/PUT /anomaly/configuration routes (anomaly_service.get_configuration/
 * update_configuration), which live inside the same OrganizationSettings
 * JSON blob every other Settings section uses — but gated on
 * ANOMALY_DETECTION permissions instead of Settings, and genuinely wired:
 * AnomalyJobEditor.jsx's create-new-job path reads these to pre-fill a
 * brand-new job's threshold_config/baseline_config/retention_days/
 * on_critical, not just "stored and displayed".
 */
import { useEffect, useState } from 'react';
import { Settings2, Save, Plus, Trash2 } from 'lucide-react';
import { Button } from '@/components/common/index.js';
import { anomalyApi, BASELINE_SOURCES } from '@/services/api/anomalyApi.js';
import './anomaly-detection.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

const SEVERITY_COLORS = { NORMAL: '#10B981', LOW: '#3B82F6', MEDIUM: '#F59E0B', HIGH: '#F97316', CRITICAL: '#EF4444' };

function ScoreBandsEditor({ bands, onChange, disabled }) {
  const update = (i, field, value) => {
    const next = bands.map((b, idx) => (idx === i ? { ...b, [field]: value } : b));
    onChange(next);
  };
  const addBand = () => onChange([...bands, { upper: (bands[bands.length - 1]?.upper || 0) + 1, label: 'NEW' }]);
  const removeBand = (i) => onChange(bands.filter((_, idx) => idx !== i));

  return (
    <div className="anm-col-table">
      <div className="anm-col-table-head" style={{ gridTemplateColumns: '24px 1fr 1fr 32px' }}>
        <span></span><span>Upper Bound</span><span>Label</span><span></span>
      </div>
      {bands.map((b, i) => (
        <div key={i} className="anm-col-row" style={{ gridTemplateColumns: '24px 1fr 1fr 32px' }}>
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: SEVERITY_COLORS[b.label] || '#94A3B8' }} />
          <input
            type="number" step="0.1" className="anm-input" style={{ width: '100%' }} disabled={disabled}
            value={b.upper} onChange={(e) => update(i, 'upper', Number(e.target.value))}
          />
          <input
            type="text" className="anm-input" style={{ width: '100%' }} disabled={disabled}
            value={b.label} onChange={(e) => update(i, 'label', e.target.value.toUpperCase())}
          />
          {!disabled && (
            <button type="button" className="anm-icon-btn" onClick={() => removeBand(i)} title="Remove band">
              <Trash2 size={12} />
            </button>
          )}
        </div>
      ))}
      {!disabled && (
        <div style={{ padding: 8 }}>
          <button type="button" className="anm-col-toolbar-btn" onClick={addBand}>
            <Plus size={11} style={{ verticalAlign: -1 }} /> Add band
          </button>
        </div>
      )}
    </div>
  );
}

export default function AnomalyConfiguration() {
  const [config, setConfig] = useState(null);
  const [savedSnapshot, setSavedSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  const load = () => {
    setLoading(true); setError('');
    anomalyApi.getConfiguration()
      .then((c) => { setConfig(c); setSavedSnapshot(JSON.stringify(c)); })
      .catch((err) => setError(apiErrorMessage(err, 'Could not load anomaly configuration.')))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const dirty = config && savedSnapshot !== JSON.stringify(config);

  const save = async () => {
    setSaving(true); setError(''); setSaved(false);
    try {
      const updated = await anomalyApi.updateConfiguration(config);
      setConfig(updated);
      setSavedSnapshot(JSON.stringify(updated));
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (err) {
      setError(apiErrorMessage(err, 'Could not save configuration.'));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div><p style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</p></div>;

  return (
    <div className="anm-page">
      <div className="anm-header">
        <div>
          <h1 className="anm-title"><Settings2 size={20} style={{ verticalAlign: -3, marginRight: 8 }} />Anomaly Configuration</h1>
          <p className="anm-subtitle">Org-wide defaults a new anomaly job is pre-filled with — existing jobs keep their own saved settings.</p>
        </div>
        <div className="anm-actions">
          {saved && <span style={{ fontSize: 12.5, color: 'var(--color-success-dark)', fontWeight: 600 }}>Saved</span>}
          <Button variant="primary" icon={<Save size={14} />} loading={saving} disabled={!dirty} onClick={save}>
            Save Defaults
          </Button>
        </div>
      </div>

      {error && <div className="anm-error-banner">{error}</div>}

      {config && (
        <div className="anm-setup" style={{ padding: 0 }}>
          <div>
            <div className="anm-setup-section-title">Default Scoring Bands</div>
            <span className="anm-setup-section-hint" style={{ display: 'block', marginBottom: 10 }}>
              A new job's threshold_config starts with these bands (ascending upper bounds — the last one is the ceiling regardless of its stated value). Existing jobs are unaffected.
            </span>
            <ScoreBandsEditor
              bands={config.default_score_bands}
              onChange={(bands) => setConfig({ ...config, default_score_bands: bands })}
            />
          </div>

          <div>
            <div className="anm-setup-section-title">Default Baseline Source</div>
            <div className="anm-options">
              {BASELINE_SOURCES.map((b) => (
                <div
                  key={b.id} className={`anm-option-card ${config.default_baseline_source === b.id ? 'selected' : ''}`}
                  onClick={() => setConfig({ ...config, default_baseline_source: b.id })}
                >
                  <div className="anm-option-check">
                    {config.default_baseline_source === b.id && <span style={{ color: 'white', fontSize: 10, fontWeight: 700 }}>✓</span>}
                  </div>
                  <div>
                    <div className="anm-option-title">{b.label}</div>
                    <div className="anm-option-desc">{b.desc}</div>
                  </div>
                </div>
              ))}
            </div>
            <div style={{ marginTop: 12 }}>
              <label className="anm-checkbox-row">
                <input
                  type="checkbox" checked={config.default_require_baseline_approval}
                  onChange={(e) => setConfig({ ...config, default_require_baseline_approval: e.target.checked })}
                />
                Require manual approval before a computed baseline is used, by default
              </label>
            </div>
          </div>

          <div className="anm-field-row">
            <div className="anm-field">
              <label className="anm-checkbox-row">
                <input
                  type="checkbox" checked={config.default_create_incident_on_critical}
                  onChange={(e) => setConfig({ ...config, default_create_incident_on_critical: e.target.checked })}
                />
                Create an incident automatically on a CRITICAL result, by default
              </label>
            </div>
            <div className="anm-field">
              <label className="anm-label">Default Retention (days, optional)</label>
              <input
                type="number" min="1" className="anm-input" placeholder="Keep forever"
                value={config.default_retention_days ?? ''}
                onChange={(e) => setConfig({ ...config, default_retention_days: e.target.value ? Number(e.target.value) : null })}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
