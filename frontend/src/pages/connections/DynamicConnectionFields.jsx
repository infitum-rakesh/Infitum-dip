/**
 * DynamicConnectionFields — renders a connection type's field list from the
 * schema the backend serves at GET /connections/types (connection_service.
 * FIELD_SCHEMAS is the single source of truth; this component never
 * hardcodes per-type field lists).
 *
 * Secret fields are handled specially: in edit mode they render empty with
 * a "leave blank to keep unchanged" placeholder, and are only included in
 * the submitted payload if the user actually types a new value — the
 * masked "••••••••" the backend returns is never treated as an editable value.
 *
 * "agent_select" (TALLY_PRIME_AGENT's agent_installation_id — Multi-Client
 * TallyPrime Integration Platform) is the one field type whose options
 * aren't static: unlike a plain "select" (field.options, an inline array
 * from FIELD_SCHEMAS), its choices are the org's own enrolled Tally Agent
 * installations, fetched once here (not per-field-render) from GET
 * /tally-agents when any visible field needs it.
 */
import { useEffect, useState } from 'react';
import tallyAgentApi from '@/services/api/tallyAgentApi.js';

const SECRET_PLACEHOLDER_SET = '•••••••• (already set — leave blank to keep unchanged)';
const SECRET_PLACEHOLDER_UNSET = 'Not set';

export function DynamicConnectionFields({ fields, values, onChange, editable, isSecretSet, secretsEditable = true }) {
  const visibleFields = fields.filter((f) => {
    if (!f.show_if) return true;
    return values[f.show_if.field] === f.show_if.equals;
  });

  const needsAgentOptions = visibleFields.some((f) => f.type === 'agent_select');
  const [agentInstallations, setAgentInstallations] = useState(null); // null = not fetched yet

  useEffect(() => {
    if (!needsAgentOptions || agentInstallations !== null) return;
    tallyAgentApi.listInstallations()
      .then((r) => setAgentInstallations(r.items ?? []))
      .catch(() => setAgentInstallations([]));
  }, [needsAgentOptions, agentInstallations]);

  return (
    <div className="conn-field-grid">
      {visibleFields.map((f) => (
        <FieldInput
          key={f.key} field={f} values={values} onChange={onChange}
          editable={editable && (!f.secret || secretsEditable)}
          isSecretSet={isSecretSet}
          agentInstallations={agentInstallations}
        />
      ))}
    </div>
  );
}

function FieldInput({ field, values, onChange, editable, isSecretSet, agentInstallations }) {
  const value = values[field.key] ?? '';
  const set = (v) => onChange(field.key, v);

  if (field.type === 'checkbox') {
    return (
      <label className="conn-field conn-field-checkbox">
        <input type="checkbox" checked={!!value} disabled={!editable} onChange={(e) => set(e.target.checked)} />
        <span>{field.label}</span>
      </label>
    );
  }

  return (
    <div className="conn-field">
      <label className="conn-field-label">
        {field.label}
        {field.required && <span className="conn-required">*</span>}
      </label>

      {field.type === 'select' ? (
        <select className="conn-input" value={value} disabled={!editable} onChange={(e) => set(e.target.value)}>
          <option value="">Select…</option>
          {(field.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : field.type === 'agent_select' ? (
        agentInstallations === null ? (
          <select className="conn-input" disabled>
            <option>Loading agents…</option>
          </select>
        ) : (
          <select className="conn-input" value={value} disabled={!editable} onChange={(e) => set(e.target.value)}>
            <option value="">Select…</option>
            {agentInstallations
              .filter((a) => a.status === 'ENROLLED')
              .map((a) => (
                <option key={a.id} value={String(a.id)}>
                  {a.name} — {a.is_connected ? 'online' : 'offline'}{a.hostname ? ` (${a.hostname})` : ''}
                </option>
              ))}
            {agentInstallations.filter((a) => a.status === 'ENROLLED').length === 0 && (
              <option value="" disabled>No enrolled agents yet — create one under Tally Agents</option>
            )}
          </select>
        )
      ) : field.type === 'textarea' ? (
        <textarea
          className="conn-input conn-textarea"
          value={value}
          disabled={!editable}
          placeholder={field.secret ? (isSecretSet?.(field.key) ? SECRET_PLACEHOLDER_SET : field.placeholder) : field.placeholder}
          onChange={(e) => set(e.target.value)}
        />
      ) : field.secret ? (
        <input
          type="password"
          className="conn-input"
          value={value}
          disabled={!editable}
          autoComplete="new-password"
          placeholder={editable ? (isSecretSet?.(field.key) ? SECRET_PLACEHOLDER_SET : (field.placeholder ?? SECRET_PLACEHOLDER_UNSET)) : (isSecretSet?.(field.key) ? '••••••••' : SECRET_PLACEHOLDER_UNSET)}
          onChange={(e) => set(e.target.value)}
        />
      ) : (
        <input
          type={field.type === 'number' ? 'number' : 'text'}
          className="conn-input"
          value={value}
          disabled={!editable}
          placeholder={field.placeholder}
          onChange={(e) => set(field.type === 'number' ? e.target.value.replace(/[^0-9]/g, '') : e.target.value)}
        />
      )}
    </div>
  );
}
