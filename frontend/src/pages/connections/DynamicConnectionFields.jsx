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
 */
const SECRET_PLACEHOLDER_SET = '•••••••• (already set — leave blank to keep unchanged)';
const SECRET_PLACEHOLDER_UNSET = 'Not set';

export function DynamicConnectionFields({ fields, values, onChange, editable, isSecretSet, secretsEditable = true }) {
  const visibleFields = fields.filter((f) => {
    if (!f.show_if) return true;
    return values[f.show_if.field] === f.show_if.equals;
  });

  return (
    <div className="conn-field-grid">
      {visibleFields.map((f) => (
        <FieldInput
          key={f.key} field={f} values={values} onChange={onChange}
          editable={editable && (!f.secret || secretsEditable)}
          isSecretSet={isSecretSet}
        />
      ))}
    </div>
  );
}

function FieldInput({ field, values, onChange, editable, isSecretSet }) {
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
