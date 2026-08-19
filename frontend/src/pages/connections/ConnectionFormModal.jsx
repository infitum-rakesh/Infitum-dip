import { useEffect, useState } from 'react';
import { Loader2, CheckCircle2, XCircle, AlertTriangle, PlayCircle, Pencil } from 'lucide-react';
import { Button, Modal, StatusBadge, PermissionGate } from '@/components/common/index.js';
import { CONNECTION_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { usePermission } from '@/hooks/usePermission.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { DynamicConnectionFields } from './DynamicConnectionFields.jsx';

const MASK = '••••••••';
const ENVIRONMENTS = ['PRODUCTION', 'STAGING', 'DEVELOPMENT', 'TEST'];

/**
 * ConnectionFormModal — handles create, edit and view. Which one is active
 * is just whether `editable` is true and whether `connection` was passed in;
 * there's no separate component per mode (avoids duplicating the whole form
 * three times).
 */
export function ConnectionFormModal({ open, onClose, catalogue, category, connectionType, connection, startEditable, onSaved }) {
  const { can } = usePermission();
  const canEditSecrets = can(CONNECTION_PERMISSIONS.CONNECTION_MANAGE_SECRET);

  const [name, setName] = useState('');
  const [environment, setEnvironment] = useState('DEVELOPMENT');
  const [config, setConfig] = useState({});
  const [saved, setSaved] = useState(null); // the persisted Connection, once created/loaded
  const [editable, setEditable] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  const type = connection?.connection_type ?? connectionType;
  const schema = catalogue?.schemas?.[type];

  useEffect(() => {
    if (!open || !schema) return;
    const defaults = {};
    schema.fields.forEach((f) => { if (f.default !== undefined) defaults[f.key] = f.default; });

    if (connection) {
      const initial = { ...defaults, ...connection.config };
      schema.fields.forEach((f) => { if (f.secret) initial[f.key] = ''; }); // never pre-fill secrets
      setConfig(initial);
      setName(connection.name);
      setEnvironment(connection.environment);
      setSaved(connection);
      setEditable(!!startEditable);
    } else {
      setConfig(defaults);
      setName('');
      setEnvironment('DEVELOPMENT');
      setSaved(null);
      setEditable(true);
    }
    setError(null);
    setTestResult(null);
  }, [open, connection, schema, startEditable]);

  if (!schema) return null;

  const isSecretSet = (key) => saved?.config?.[key] === MASK;

  const buildSubmitConfig = () => {
    const out = {};
    schema.fields.forEach((f) => {
      if (f.secret) {
        if (config[f.key]) out[f.key] = config[f.key]; // only send if the user actually typed something
      } else {
        out[f.key] = config[f.key];
      }
    });
    return out;
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      if (!saved) {
        const created = await connectionApi.create({
          name, environment, category, connection_type: type, config: buildSubmitConfig(),
        });
        setSaved(created);
        setEditable(false);
      } else {
        const updated = await connectionApi.update(saved.id, { name, environment, config: buildSubmitConfig() });
        setSaved(updated);
        setEditable(false);
      }
      onSaved();
    } catch (err) {
      setError(err.message || 'Failed to save connection.');
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    if (!saved) return;
    setTesting(true);
    setTestResult(null);
    try {
      const result = await connectionApi.test(saved.id);
      setTestResult(result);
      setSaved((s) => ({
        ...s, status: result.status, last_tested_at: result.tested_at, last_test_success: result.success, last_test_message: result.message,
      }));
      onSaved();
    } catch (err) {
      setTestResult({ success: false, message: err.message || 'Test failed.' });
    } finally {
      setTesting(false);
    }
  };

  const title = saved ? (editable ? `Edit ${saved.name}` : saved.name) : `New ${schema.label} Connection`;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>{editable ? 'Cancel' : 'Close'}</Button>
          {saved && (
            <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_TEST} fallback={null}>
              <Button variant="secondary" icon={testing ? <Loader2 size={14} className="conn-spin" /> : <PlayCircle size={14} />} loading={false} disabled={testing} onClick={handleTest}>
                {testing ? 'Testing…' : 'Test Connection'}
              </Button>
            </PermissionGate>
          )}
          {saved && !editable && (
            <PermissionGate {...CONNECTION_PERMISSIONS.CONNECTION_EDIT} fallback={null}>
              <Button variant="secondary" icon={<Pencil size={13} />} onClick={() => setEditable(true)}>Edit</Button>
            </PermissionGate>
          )}
          {editable && (
            <Button variant="primary" loading={saving} onClick={handleSave}>Save Connection</Button>
          )}
        </>
      }
    >
      {error && <div className="conn-form-error">{error}</div>}

      <div className="conn-field-grid" style={{ marginBottom: 4 }}>
        <div className="conn-field">
          <label className="conn-field-label">Connection Name<span className="conn-required">*</span></label>
          <input className="conn-input" value={name} disabled={!editable} onChange={(e) => setName(e.target.value)} placeholder={`e.g. ${schema.label} PROD`} />
        </div>
        <div className="conn-field">
          <label className="conn-field-label">Environment</label>
          <select className="conn-input" value={environment} disabled={!editable} onChange={(e) => setEnvironment(e.target.value)}>
            {ENVIRONMENTS.map((e) => <option key={e} value={e}>{e.charAt(0) + e.slice(1).toLowerCase()}</option>)}
          </select>
        </div>
      </div>

      <div className="conn-form-divider" />

      <DynamicConnectionFields
        fields={schema.fields}
        values={config}
        onChange={(key, val) => setConfig((c) => ({ ...c, [key]: val }))}
        editable={editable}
        isSecretSet={isSecretSet}
        secretsEditable={canEditSecrets}
      />

      {saved && (
        <div className="conn-status-row">
          <span className="docd-meta-label">Status</span>
          <StatusBadge variant={{ ACTIVE: 'success', ERROR: 'danger', UNSUPPORTED: 'warning', INACTIVE: 'gray', UNTESTED: 'gray' }[saved.status] ?? 'gray'} dot>{saved.status}</StatusBadge>
          {saved.last_tested_at && <span className="text-muted text-sm">Last tested {formatDateTime(saved.last_tested_at)}</span>}
          {saved.secrets_rotated_at && <span className="text-muted text-sm">Credentials rotated {formatDateTime(saved.secrets_rotated_at)}</span>}
        </div>
      )}

      {(testing || testResult) && (
        <div className={`conn-test-result ${testing ? 'testing' : testResult.status === 'UNSUPPORTED' ? 'unsupported' : testResult.success ? 'success' : 'failure'}`}>
          {testing ? (
            <><Loader2 size={16} className="conn-spin" /> Testing…</>
          ) : testResult.status === 'UNSUPPORTED' ? (
            <><AlertTriangle size={16} /> Not Yet Supported — {testResult.message}</>
          ) : testResult.success ? (
            <>
              <CheckCircle2 size={16} /> Connection Successful — {testResult.message}
              {(testResult.latency_ms != null || testResult.server_version) && (
                <span className="conn-test-result-detail">
                  {testResult.latency_ms != null ? `(${testResult.latency_ms} ms` : '('}
                  {testResult.server_version ? `${testResult.latency_ms != null ? ', ' : ''}${testResult.server_version}` : ''})
                </span>
              )}
            </>
          ) : (
            <><XCircle size={16} /> Connection Failed — {testResult.message}</>
          )}
        </div>
      )}
    </Modal>
  );
}
