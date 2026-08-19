import { useLocation, Link } from 'react-router-dom';
import { ChevronRight, Home } from 'lucide-react';

const LABELS = {
  app: null, dashboard: 'Dashboard', catalog: 'Data Catalog',
  connections: 'Connections', 'doc-intel': 'Document Intelligence',
  documents: 'Documents', review: 'Review Queue',
  pipeline: 'Pipeline Builder', 'pipeline-runs': 'Pipeline Runs',
  transform: 'Transform Workspace', sql: 'SQL Workspace',
  validation: 'Validation', reconciliation: 'Reconciliation',
  lineage: 'Data Lineage', monitoring: 'Monitoring', incidents: 'Incidents',
  schedules: 'Schedules', admin: 'Admin', users: 'Users', roles: 'Roles',
  orgs: 'Organizations', licenses: 'Licenses', audit: 'Audit Logs',
  settings: 'Settings', seller: 'Seller',
};

export function Breadcrumb() {
  const { pathname } = useLocation();
  const parts = pathname.split('/').filter(Boolean);

  const crumbs = parts
    .map((part, i) => ({ label: LABELS[part] ?? part, to: '/' + parts.slice(0, i + 1).join('/') }))
    .filter((c) => c.label !== null);

  return (
    <nav className="breadcrumb" aria-label="Breadcrumb">
      {crumbs.map((crumb, i) => (
        <span key={crumb.to} className="bc-item">
          {i > 0 && <ChevronRight size={12} className="bc-sep" />}
          {i === crumbs.length - 1
            ? <span className="bc-current">{crumb.label}</span>
            : <Link to={crumb.to} className="bc-link">{crumb.label}</Link>}
        </span>
      ))}
    </nav>
  );
}
