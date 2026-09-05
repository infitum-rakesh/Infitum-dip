/**
 * Seller Portal -> Payment Gateways — /app/seller/billing/gateways (SB27).
 * Real gateway configuration cards from GET /api/payment-gateways
 * (backend/app/api/billing/payment_gateways.py) — one row per registered
 * provider (CUSTOM/STRIPE/RAZORPAY/PAYPAL/ADYEN), auto-created
 * NOT_CONFIGURED on first load. Configure/Test/Activate all hit real
 * endpoints; credential values are write-only here — the backend never
 * returns a decrypted value back to this page, only which fields are
 * saved (configured_credential_keys).
 */
import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, Loader2, Power, PowerOff, ShieldCheck, XCircle, Zap } from 'lucide-react';
import { Button, Modal, StatusBadge } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { paymentGatewayApi } from '@/services/api/seller/paymentGatewayApi.js';
import './payment-gateways.css';

const STATUS_META = {
  CONNECTED: { variant: 'success', label: 'Connected' },
  ERROR: { variant: 'danger', label: 'Error' },
  NOT_CONFIGURED: { variant: 'gray', label: 'Not Configured' },
};

const PROVIDER_NOTE = {
  CUSTOM: 'Self-hosted checkout + signed webhook — the reference implementation this platform live-verifies its whole payment architecture through.',
  STRIPE: 'Real REST integration against Stripe’s documented API — disclosed unverified, no live Stripe account connected.',
  RAZORPAY: 'Real REST integration against Razorpay’s documented API — disclosed unverified, no live Razorpay account connected.',
  PAYPAL: 'Real REST integration against PayPal’s documented API — disclosed unverified, no live PayPal account connected.',
  ADYEN: 'Not yet implemented in this platform.',
};

export default function PaymentGateways() {
  const [gateways, setGateways] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [configureId, setConfigureId] = useState(null);
  const [credentials, setCredentials] = useState({});
  const [webhookSecret, setWebhookSecret] = useState('');
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [formError, setFormError] = useState(null);

  // `silent` reloads (after Save/Test/Activate/Deactivate, or an
  // environment switch, all while a Configure modal may be open) must
  // never toggle the page-level `loading` flag — that flag's early
  // return replaces this whole component's render output including any
  // open Modal, so every action would otherwise flash the page to a
  // spinner and silently close whatever modal the user had open. Found
  // live: Save Credentials' own reload was doing exactly this.
  const load = useCallback((opts = {}) => {
    if (!opts.silent) setLoading(true);
    return paymentGatewayApi.list()
      .then((d) => { setGateways(d.items); setError(null); })
      .catch((err) => setError(err?.response?.data?.detail || 'Could not load payment gateways.'))
      .finally(() => { if (!opts.silent) setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);

  const configuring = gateways.find((g) => g.id === configureId);

  function openConfigure(g) {
    setConfigureId(g.id);
    setCredentials({});
    setWebhookSecret('');
    setTestResult(null);
    setFormError(null);
  }

  async function handleSaveCredentials() {
    if (!configuring) return;
    setSaving(true);
    setFormError(null);
    try {
      const body = {
        credentials_environment: configuring.environment,
        credentials,
        ...(webhookSecret ? { webhook_secret: webhookSecret } : {}),
      };
      await paymentGatewayApi.update(configuring.id, body);
      await load({ silent: true });
      setCredentials({});
      setWebhookSecret('');
    } catch (err) {
      setFormError(err?.response?.data?.detail || 'Could not save credentials.');
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    if (!configuring) return;
    setTesting(true);
    setTestResult(null);
    try {
      const result = await paymentGatewayApi.testConnection(configuring.id, configuring.environment);
      setTestResult(result);
      await load({ silent: true });
    } finally {
      setTesting(false);
    }
  }

  async function handleToggleEnvironment(env) {
    if (!configuring) return;
    setActionBusy(true);
    setFormError(null);
    try {
      await paymentGatewayApi.update(configuring.id, { set_environment: env });
      await load({ silent: true });
    } catch (err) {
      setFormError(err?.response?.data?.detail || 'Could not switch environment.');
    } finally {
      setActionBusy(false);
    }
  }

  async function handleActivate() {
    if (!configuring) return;
    setActionBusy(true);
    setFormError(null);
    try {
      await paymentGatewayApi.activate(configuring.id);
      await load({ silent: true });
    } catch (err) {
      setFormError(err?.response?.data?.detail || 'Could not activate this gateway.');
    } finally {
      setActionBusy(false);
    }
  }

  async function handleDeactivate() {
    if (!configuring) return;
    setActionBusy(true);
    try {
      await paymentGatewayApi.deactivate(configuring.id);
      await load({ silent: true });
    } finally {
      setActionBusy(false);
    }
  }

  if (loading) return <div className="pg-loading"><Loader2 size={20} className="pg-spin" /> Loading payment gateways…</div>;
  if (error) return <div className="pg-banner error"><AlertTriangle size={15} /> {error}</div>;

  return (
    <div className="pg-page">
      <div className="pg-header">
        <h1>Payment Gateways</h1>
        <p className="pg-subtitle">Configure and activate the processors customers can pay invoices through.</p>
      </div>

      <div className="pg-grid">
        {gateways.map((g) => {
          const meta = STATUS_META[g.status] || STATUS_META.NOT_CONFIGURED;
          const configuredCount = (g.configured_credential_keys?.[g.environment] || []).length;
          const totalFields = Object.keys(g.credential_fields || {}).length;
          return (
            <div key={g.id} className={`pg-card${g.is_active ? ' pg-card-active' : ''}`}>
              <div className="pg-card-head">
                <div className="pg-card-title">
                  <Zap size={16} />
                  <span>{g.display_name}</span>
                </div>
                {g.is_active && <span className="pg-active-pill">Active</span>}
              </div>
              <div className="pg-card-badges">
                <StatusBadge variant={meta.variant} dot>{meta.label}</StatusBadge>
                <StatusBadge variant={g.environment === 'LIVE' ? 'danger' : 'gray'}>{g.environment}</StatusBadge>
              </div>
              <p className="pg-card-note">{PROVIDER_NOTE[g.provider] || ''}</p>
              <div className="pg-card-meta">
                <span>{configuredCount}/{totalFields} credential fields saved ({g.environment})</span>
                {g.live_verified_at && <span>LIVE verified {formatDateTime(g.live_verified_at)}</span>}
              </div>
              <Button variant="secondary" onClick={() => openConfigure(g)}>Configure</Button>
            </div>
          );
        })}
      </div>

      {configuring && (
        <Modal open onClose={() => setConfigureId(null)} title={`Configure ${configuring.display_name}`} size="md">
          <div className="pg-modal">
            <div className="pg-env-toggle">
              {['TEST', 'LIVE'].map((env) => (
                <button
                  key={env}
                  className={`pg-env-btn${configuring.environment === env ? ' active' : ''}`}
                  onClick={() => handleToggleEnvironment(env)}
                  disabled={actionBusy}
                >
                  {env}
                </button>
              ))}
            </div>

            <div className="pg-fields">
              {Object.entries(configuring.credential_fields || {}).map(([key, label]) => {
                const alreadySaved = (configuring.configured_credential_keys?.[configuring.environment] || []).includes(key);
                return (
                  <div className="pg-field" key={key}>
                    <label>{label} {alreadySaved && <span className="pg-saved-tag">saved</span>}</label>
                    <input
                      type="password"
                      className="pg-input"
                      placeholder={alreadySaved ? '••••••••  (leave blank to keep)' : `Enter ${label}`}
                      value={credentials[key] || ''}
                      onChange={(e) => setCredentials((c) => ({ ...c, [key]: e.target.value }))}
                    />
                  </div>
                );
              })}
              <div className="pg-field">
                <label>Webhook Secret {configuring.has_webhook_secret?.[configuring.environment] && <span className="pg-saved-tag">saved</span>}</label>
                <input
                  type="password"
                  className="pg-input"
                  placeholder="Leave blank to keep existing"
                  value={webhookSecret}
                  onChange={(e) => setWebhookSecret(e.target.value)}
                />
              </div>
            </div>

            {formError && <div className="pg-inline-error">{formError}</div>}

            <div className="pg-modal-actions">
              <Button variant="primary" onClick={handleSaveCredentials} disabled={saving}>
                {saving ? 'Saving…' : 'Save Credentials'}
              </Button>
              <Button variant="secondary" onClick={handleTest} disabled={testing}>
                <ShieldCheck size={14} /> {testing ? 'Testing…' : 'Test Connection'}
              </Button>
              {configuring.is_active ? (
                <Button variant="danger" onClick={handleDeactivate} disabled={actionBusy}>
                  <PowerOff size={14} /> Deactivate
                </Button>
              ) : (
                <Button variant="success" onClick={handleActivate} disabled={actionBusy || configuring.status !== 'CONNECTED'}>
                  <Power size={14} /> Activate
                </Button>
              )}
            </div>

            {testResult && (
              <div className={`pg-test-result ${testResult.success ? 'ok' : 'fail'}`}>
                {testResult.success ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                <span>{testResult.detail}{testResult.latency_ms ? ` (${Math.round(testResult.latency_ms)}ms)` : ''}</span>
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
