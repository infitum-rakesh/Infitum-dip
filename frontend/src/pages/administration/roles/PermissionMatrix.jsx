/**
 * PermissionMatrix — category × action checkbox grid, shared by
 * CreateRoleModal and RoleDetails' Permissions tab. Categories and actions
 * are always passed in from the API response (roleApi.list()'s
 * `categories`/`licensed_categories`/`category_actions`) — never
 * hardcoded here, so a new category (or a category-specific action set)
 * added on the backend just shows up.
 *
 * Most categories share the same generic 6-action set, but a category can
 * offer its own — e.g. "SQL Workspace" offers WRITE/EXPORT/AI_ASSISTANT/
 * DDL/DML/PRODUCTION_ACCESS instead of CREATE/EDIT/MANAGE (see backend
 * role_service.CATEGORY_ACTIONS). The table still shows ONE consistent
 * column set (the union of every category's actions) so the grid doesn't
 * jump around row to row — a column that doesn't apply to a given row
 * renders as a plain "—", not a checkbox, and can't be toggled.
 *
 * value: Set of "Category::ACTION" strings (granted cells)
 * onChange: (nextSet) => void — omit for a read-only matrix
 */
import { Lock } from 'lucide-react';
import './roles.css';

const DEFAULT_ACTIONS = ['VIEW', 'CREATE', 'EDIT', 'EXECUTE', 'DELETE', 'MANAGE'];
const ACTION_LABEL = {
  VIEW: 'View', CREATE: 'Create', EDIT: 'Edit', EXECUTE: 'Execute', DELETE: 'Delete', MANAGE: 'Manage',
  WRITE: 'Write', EXPORT: 'Export', AI_ASSISTANT: 'AI Assistant', DML: 'DML', DDL: 'DDL',
  PRODUCTION_ACCESS: 'Production Access',
};

const key = (category, action) => `${category}::${action}`;

export function PermissionMatrix({ categories, licensedCategories, categoryActions, value, onChange, readOnly = false }) {
  const licensedSet = new Set(licensedCategories);
  const readOnlyEffective = readOnly || !onChange;
  const actionsFor = (cat) => categoryActions?.[cat] ?? DEFAULT_ACTIONS;

  // One shared column set for the whole table: DEFAULT_ACTIONS first (the
  // familiar/common order), then any category-specific extras appended in
  // the order they first appear.
  const allActions = categoryActions
    ? [...new Set([...DEFAULT_ACTIONS, ...categories.flatMap((c) => actionsFor(c))])]
    : DEFAULT_ACTIONS;

  const toggleCell = (category, action) => {
    if (readOnlyEffective || !licensedSet.has(category) || !actionsFor(category).includes(action)) return;
    const k = key(category, action);
    const next = new Set(value);
    next.has(k) ? next.delete(k) : next.add(k);
    onChange(next);
  };

  const toggleRow = (category) => {
    if (readOnlyEffective || !licensedSet.has(category)) return;
    const rowActions = actionsFor(category);
    const allGranted = rowActions.every((a) => value.has(key(category, a)));
    const next = new Set(value);
    rowActions.forEach((a) => (allGranted ? next.delete(key(category, a)) : next.add(key(category, a))));
    onChange(next);
  };

  return (
    <div className="rol-matrix-wrap">
      <table className="rol-matrix">
        <thead>
          <tr>
            <th>Category</th>
            {allActions.map((a) => <th key={a}>{ACTION_LABEL[a] ?? a}</th>)}
          </tr>
        </thead>
        <tbody>
          {categories.map((cat) => {
            const unlicensed = !licensedSet.has(cat);
            const applicable = new Set(actionsFor(cat));
            return (
              <tr key={cat} className={unlicensed ? 'unlicensed' : ''}>
                <td className="rol-matrix-cat-cell" onClick={() => toggleRow(cat)} title={unlicensed ? 'Not licensed for this organization' : 'Click to toggle all actions'}>
                  {cat}
                  {unlicensed && <span className="rol-matrix-lock"><Lock size={9} style={{ verticalAlign: -1 }} /> not licensed</span>}
                </td>
                {allActions.map((action) => (
                  <td key={action}>
                    {applicable.has(action) ? (
                      <input
                        type="checkbox"
                        checked={value.has(key(cat, action))}
                        disabled={readOnlyEffective || unlicensed}
                        onChange={() => toggleCell(cat, action)}
                      />
                    ) : (
                      <span className="rol-matrix-na" aria-hidden="true">—</span>
                    )}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function cellsToSet(permissions) {
  return new Set((permissions ?? []).map((p) => key(p.category, p.action)));
}

export function setToCells(set) {
  return [...set].map((k) => { const [category, action] = k.split('::'); return { category, action }; });
}
