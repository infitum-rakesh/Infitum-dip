/**
 * Docs — public reference page describing the platform's real modules
 * (linked from the Home page nav). Every capability listed here is an
 * actual, implemented feature of this app, not marketing copy for
 * something that doesn't exist — "Open in App" links go to the real
 * routes (gated by the normal login/module-license checks once clicked).
 */
import { Link } from 'react-router-dom';
import { ArrowRight, Check } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth.js';
import './docs.css';

const DOC_SECTIONS = [
  {
    id: 'getting-started', icon: '🚀', color: '#4F46E5', title: 'Getting Started',
    desc: 'Every new organization starts on a 14-day free trial with full module access — no credit card required. Register, verify your email, and you land on the Dashboard with real-time KPIs for documents processed, pipeline runs and validation pass rate.',
    capabilities: [
      'Self-serve registration creates your organization and a 14-day trial',
      'Dashboard shows live counts — nothing is a placeholder',
      'Invite teammates from Administration → Users with a role (Org Admin, Data Engineer, Analyst, Viewer)',
      'Every module below is gated by both your role and your organization\'s licensed modules',
    ],
    appLink: { label: 'Open Dashboard', to: '/app/dashboard' },
  },
  {
    id: 'connections', icon: '🔗', color: '#14B8A6', title: 'Connections',
    desc: 'A Connection is a saved, tested credential to a real external system. Every source and target used anywhere in the platform — Pipeline Builder, SQL Workspace, Column Mapping — points back to one of these.',
    capabilities: [
      'Databases: PostgreSQL, MySQL, SQL Server, Oracle, Snowflake, SAP HANA, Microsoft Fabric, Vertica',
      'Applications: Salesforce, NetSuite (SuiteQL + SuiteTalk SOAP)',
      'Files & storage: SFTP, FTP, Local File, Network File (SMB), Azure Blob, AWS S3',
      'APIs: generic REST and SOAP endpoints',
      'AI providers: OpenAI, Anthropic, Ollama — used by AI Extraction and the SQL Workspace AI Assistant',
    ],
    appLink: { label: 'Open Connections', to: '/app/connections' },
  },
  {
    id: 'document-intelligence', icon: '📄', color: '#EF4444', title: 'Document Intelligence',
    desc: 'Upload PDFs, scans or images and the platform runs them through a real OCR engine (Tesseract by default, or a configured cloud provider — AWS Textract, Google Vision, Azure Form Recognizer) followed by AI-driven field extraction.',
    capabilities: [
      'Bulk upload with automatic duplicate-file detection',
      'AI Extraction against a configurable schema — including table/line-item extraction',
      'Confidence-scored fields route low-confidence documents to the Review Queue for manual correction',
      'Manual structuring panel for documents that need hand correction',
    ],
    appLink: { label: 'Open Document Intelligence', to: '/app/doc-intel' },
  },
  {
    id: 'transform', icon: '⚙️', color: '#3B82F6', title: 'Transform Workspace & Pipeline Builder',
    desc: 'A drag-and-drop canvas for building multi-stage pipelines: source → transform → quality → target. Filter, select, rename, cast, sort, deduplicate, join, lookup, aggregate, calculated columns and conditional logic nodes, plus a dedicated Column Mapping tool for straight object-to-object loads.',
    capabilities: [
      'Every Save creates a new, immutable pipeline version — nothing is overwritten',
      'Run Pipeline executes the real DAG node-by-node with a live log viewer',
      'Column Mapping auto-suggests source→target column matches with confidence scoring',
      'Run Load bulk-writes to the target with automatic row-by-row fallback on constraint violations',
    ],
    appLink: { label: 'Open Pipeline Builder', to: '/app/pipeline' },
  },
  {
    id: 'data-quality', icon: '✅', color: '#10B981', title: 'Validation & Reconciliation',
    desc: 'Validation runs a configurable rule set (not-null, range, format/regex, uniqueness, referential integrity, custom SQL condition) against a live object. Reconciliation compares a source and target object at the count, checksum, key-match and field level.',
    capabilities: [
      'Every rule\'s pass/fail counts and failing records are downloadable',
      'Reconciliation exceptions are categorized as missing, extra or mismatched',
      'Both integrate with Monitoring\'s Data Quality tab and can trigger Incidents',
    ],
    appLink: { label: 'Open Validation', to: '/app/validation' },
  },
  {
    id: 'monitoring-lineage', icon: '📊', color: '#F59E0B', title: 'Monitoring & Data Lineage',
    desc: 'Monitoring reads live off real pipeline execution history — run status, SLA tracking, performance and configurable alert rules that can auto-create an Incident. Data Lineage is computed on demand from your actual saved pipeline definitions and column mappings, never a separately maintained copy — so it can never drift out of sync, and a pipeline version\'s lineage never changes after the fact.',
    capabilities: [
      'SLA definitions per pipeline (expected duration, failure tolerance) with Met/Warning/Breached status',
      'Alert rules with pluggable notification channels (in-app, email, webhook, Teams)',
      'Interactive lineage graph — pan, zoom, click a node for details',
      'Column-level lineage with real transformation expressions, and impact analysis (what breaks if this changes)',
    ],
    appLink: { label: 'Open Monitoring', to: '/app/monitoring' },
  },
  {
    id: 'sql-workspace', icon: '💾', color: '#0EA5E9', title: 'SQL Workspace',
    desc: 'A full SQL editor against any Connection — schema browser, syntax highlighting and autocomplete, a resizable results grid, and an AI Assistant that turns plain-language questions into SQL against your real connected schema.',
    capabilities: [
      'Runs real SELECT/INSERT/UPDATE/DELETE/DDL statements — including Salesforce SOQL',
      'Per-statement permission tiers (view/execute/write/DDL/production access) enforced server-side',
      'Query history, saved/shared/favorite queries, and background exports for large result sets',
    ],
    appLink: { label: 'Open SQL Workspace', to: '/app/sql-workspace' },
  },
  {
    id: 'ops', icon: '⏰', color: '#8B5CF6', title: 'Scheduling & Incidents',
    desc: 'Schedule any pipeline on a recurring cadence, and every failure — pipeline, validation, reconciliation, SLA breach — can automatically open an Incident with a full timeline, evidence and assignment workflow.',
    capabilities: [
      'Cron and simple-interval scheduling with retry/notification settings',
      'Incidents track severity, root cause, timeline events and linked evidence',
      'Cross-navigation: an Incident links straight back to its pipeline run and lineage graph',
    ],
    appLink: { label: 'Open Incidents', to: '/app/incidents' },
  },
  {
    id: 'admin', icon: '🛡️', color: '#EC4899', title: 'Administration',
    desc: 'Org Admins manage users, custom roles with a granular permission matrix, module licenses, a merged audit trail across every module, and organization-wide settings.',
    capabilities: [
      'Custom roles clamp to whatever modules your organization is actually licensed for',
      'Audit Logs merge login events, role changes, settings changes and data-access events into one searchable trail',
      'Settings covers general, data retention, document intelligence defaults, AI provider, notifications and security',
    ],
    appLink: { label: 'Open Administration', to: '/app/admin/users' },
  },
];

export default function Docs() {
  const { isAuthenticated } = useAuth();
  return (
    <div className="docs">
      <nav className="docs-nav">
        <div className="docs-nav-inner">
          <Link to="/" className="docs-logo">
            <div className="docs-mark">DI</div>
            <span className="docs-name">DataIntelligence Docs</span>
          </Link>
          <div className="docs-nav-actions">
            <Link to="/" className="docs-nav-link">← Back to Home</Link>
            {isAuthenticated ? (
              <Link to="/app/dashboard" className="docs-nav-btn">Dashboard</Link>
            ) : (
              <Link to="/register" className="docs-nav-btn">Start Free Trial</Link>
            )}
          </div>
        </div>
      </nav>

      <div className="docs-body">
        <div className="docs-toc">
          {DOC_SECTIONS.map((s) => (
            <a key={s.id} href={`#${s.id}`} className="docs-toc-link">{s.title}</a>
          ))}
        </div>

        <div className="docs-content">
          <div className="docs-header">
            <h1 className="docs-h1">Documentation</h1>
            <p className="docs-sub">
              What every module actually does, and where to find it. This reflects the real, implemented
              platform — not a roadmap.
            </p>
          </div>

          {DOC_SECTIONS.map((s) => (
            <section key={s.id} id={s.id} className="docs-section">
              <div className="docs-section-head">
                <div className="docs-section-icon" style={{ background: `${s.color}20`, color: s.color }}>{s.icon}</div>
                <h2 className="docs-h2">{s.title}</h2>
              </div>
              <p className="docs-section-desc">{s.desc}</p>
              <div className="docs-cap-grid">
                {s.capabilities.map((c) => (
                  <div key={c} className="docs-cap-item"><Check size={13} strokeWidth={2.5} />{c}</div>
                ))}
              </div>
              <Link to={s.appLink.to} className="docs-open-link">
                {s.appLink.label} <ArrowRight size={13} />
              </Link>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
