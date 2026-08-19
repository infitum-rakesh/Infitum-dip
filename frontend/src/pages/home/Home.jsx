import { Link } from 'react-router-dom';
import { ArrowRight, Play, Check, ChevronRight, ChevronDown } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth.js';
import './home.css';

// ── Static data ────────────────────────────────────────────────

const PIPELINE_STEPS = [
  { num: 1, icon: '📄', label: 'PDF / Image',        desc: 'Upload any document, scan or file', color: '#EF4444', bg: 'rgba(239,68,68,0.14)' },
  { num: 2, icon: '🔬', label: 'Image Scanner',       desc: 'Deskew, enhance, classify pages',   color: '#F59E0B', bg: 'rgba(245,158,11,0.14)' },
  { num: 3, icon: '📝', label: 'OCR Engine',           desc: 'Extract text with bounding boxes',  color: '#8B5CF6', bg: 'rgba(139,92,246,0.14)' },
  { num: 4, icon: '🧠', label: 'AI Extraction',        desc: 'Schema-driven field extraction',    color: '#4F46E5', bg: 'rgba(79,70,229,0.18)', isAI: true },
  { num: 5, icon: '🗂️', label: 'Structured Dataset',  desc: 'Clean, typed, profiled dataset',    color: '#10B981', bg: 'rgba(16,185,129,0.14)' },
  { num: 6, icon: '⚙️', label: 'Transformation',       desc: 'Visual pipeline, join, aggregate',  color: '#3B82F6', bg: 'rgba(59,130,246,0.14)' },
  { num: 7, icon: '✅', label: 'Validation',            desc: 'Rules engine, quality scoring',     color: '#10B981', bg: 'rgba(16,185,129,0.14)' },
  { num: 8, icon: '⚖️', label: 'Reconciliation',       desc: 'Source-to-target comparison',       color: '#F59E0B', bg: 'rgba(245,158,11,0.14)' },
  { num: 9, icon: '🎯', label: 'Target',                desc: 'Load to DB, API, cloud, SFTP',      color: '#10B981', bg: 'rgba(16,185,129,0.18)' },
];

const FEATURES = [
  { icon: '📄', title: 'Document Intelligence', desc: 'Convert PDFs, scanned images and unstructured documents into structured datasets using AI-powered OCR and extraction.', color: '#EF4444' },
  { icon: '🧠', title: 'AI Extraction',          desc: 'Define extraction schemas in plain language. AI identifies fields, tables and entities with configurable confidence thresholds.', color: '#4F46E5' },
  { icon: '⚙️', title: 'Visual Transformation', desc: 'Drag-and-drop pipeline designer with filter, join, aggregate, pivot, deduplicate and 20+ transformation nodes.', color: '#3B82F6' },
  { icon: '✅', title: 'Data Quality',            desc: 'Automated validation with null, format, range, uniqueness and custom business rules. AI-generated rule suggestions.', color: '#10B981' },
  { icon: '⚖️', title: 'Reconciliation',         desc: 'Record-level, field-level and aggregate comparison between source and target. Downloadable exception reports.', color: '#F59E0B' },
  { icon: '🌐', title: 'Data Lineage',            desc: 'Full traceability from source document to target column. Dataset lineage, column lineage and impact analysis.', color: '#8B5CF6' },
];

const STATS = [
  { value: '10M+',  label: 'Documents processed' },
  { value: '99.2%', label: 'OCR accuracy' },
  { value: '< 2m',  label: 'Avg processing time' },
  { value: '500+',  label: 'Enterprise customers' },
];

const TRUST = ['SOC 2 Type II', 'GDPR Ready', 'ISO 27001', 'Enterprise SSO'];

// ── Pipeline visualization ─────────────────────────────────────

// 5 evenly-spaced x-positions (in a 0–100 viewBox, i.e. percentages) that the
// source cards sit above — kept in one place so the SVG converging paths
// always line up with the grid below regardless of viewport width.
const PVIZ_SRC_X = [10, 30, 50, 70, 90];

/** A small connector shown ONLY in the stacked mobile layout (hidden on desktop,
 * where the SVG fan / horizontal connectors already do this job). Reused between
 * every stacked node so the mobile flow reads as one continuous animated chain. */
function FlowConnector() {
  return (
    <div className="pviz-flow-connector" aria-hidden="true">
      <span className="pviz-flow-connector-line" />
      <ChevronDown size={12} />
    </div>
  );
}

function PipelineViz() {
  const sources = [
    { icon: '📄', label: 'PDF',      color: '#EF4444' },
    { icon: '🖼️', label: 'Images',  color: '#F59E0B' },
    { icon: '📊', label: 'Excel',    color: '#3B82F6' },
    { icon: '🗄️', label: 'Database', color: '#8B5CF6' },
    { icon: '🌐', label: 'APIs',     color: '#10B981' },
  ];

  const engineStages = [
    { icon: '🔎', label: 'Scanner' },
    { icon: '📝', label: 'OCR' },
    { icon: '🧠', label: 'AI Extraction' },
  ];

  const outputStages = [
    { icon: '🗂️', label: 'Dataset',    color: '#10B981' },
    { icon: '⚙️', label: 'Transform',  color: '#3B82F6' },
    { icon: '✅', label: 'Validate',   color: '#10B981' },
    { icon: '⚖️', label: 'Reconcile', color: '#F59E0B' },
    { icon: '🎯', label: 'Target',     color: '#10B981' },
  ];

  return (
    <div className="pviz-wrap">
      {/* Ambient glows */}
      <div className="pviz-glow pviz-glow-1" aria-hidden="true" />
      <div className="pviz-glow pviz-glow-2" aria-hidden="true" />

      {/* ── Row 1: Input sources — equal-width grid, perfectly aligned ── */}
      <div className="pviz-sources" role="list" aria-label="Data sources">
        {sources.map((src, i) => (
          <div key={src.label} role="listitem">
            <div
              className="pviz-source-card"
              style={{ '--nc': src.color, animationDelay: `${i * 0.08}s` }}
            >
              <div className="pviz-source-icon">{src.icon}</div>
              <span className="pviz-source-label">{src.label}</span>
              <div className="pviz-source-dot" />
            </div>
            {/* Mobile-only stacked connector between source cards */}
            {i < sources.length - 1 && <FlowConnector />}
          </div>
        ))}
      </div>

      {/* ── Converging animated dotted SVG paths → Intelligence Engine (desktop) ── */}
      <div className="pviz-conv" aria-hidden="true">
        <svg viewBox="0 0 100 42" preserveAspectRatio="none" className="pviz-conv-svg">
          {PVIZ_SRC_X.map((x, i) => {
            const d = `M ${x} 0 C ${x} 22, 50 20, 50 42`;
            return (
              <g key={i}>
                {/* Static dotted track */}
                <path d={d} className="pviz-flow-track" />
                {/* Colored line that flows along the same dotted path, per source */}
                <path
                  d={d}
                  className="pviz-flow-path"
                  style={{ '--dc': sources[i].color, animationDelay: `${i * 0.2}s` }}
                />
              </g>
            );
          })}
        </svg>
      </div>
      {/* Mobile-only connector: source stack → engine */}
      <FlowConnector />

      {/* ── Row 2: Intelligence engine — Scanner → OCR → AI Extraction ── */}
      <div className="pviz-engine-row">
        <div className="pviz-engine-card">
          <div className="pviz-engine-body">
            <div className="pviz-engine-stages">
              {engineStages.map((s, i) => (
                <div key={s.label} className="pviz-engine-step-wrap">
                  <div className="pviz-engine-step">
                    <div className="pviz-engine-step-icon">{s.icon}</div>
                    <span className="pviz-engine-step-label">{s.label}</span>
                  </div>
                  {i < engineStages.length - 1 && (
                    <div className="pviz-engine-sep" aria-hidden="true">
                      <span className="pviz-engine-sep-line" />
                      <ChevronRight size={13} className="pviz-engine-sep-icon" />
                    </div>
                  )}
                </div>
              ))}
            </div>
            <div className="pviz-engine-tag">✦ Intelligence Engine</div>
          </div>
        </div>
      </div>

      {/* ── Connector: engine → output pipeline (both layouts) ── */}
      <div className="pviz-vline-wrap" aria-hidden="true">
        <div className="pviz-vline" />
        <div className="pviz-vline-arrow" />
      </div>

      {/* ── Row 3: Output pipeline — equal size, equal spacing, aligned centers ── */}
      <div className="pviz-output-row">
        {outputStages.map((stage, i) => (
          <div key={stage.label} className="pviz-output-step-wrap">
            <div
              className="pviz-output-stage"
              style={{ '--sc': stage.color, animationDelay: `${1 + i * 0.1}s` }}
            >
              <div className="pviz-output-icon">{stage.icon}</div>
              <span className="pviz-output-label">{stage.label}</span>
              <div className="pviz-output-indicator" />
            </div>
            {i < outputStages.length - 1 && (
              <div className="pviz-h-connector" aria-hidden="true">
                <div className="pviz-h-line" />
                <ChevronRight size={12} className="pviz-h-arrow-icon" />
              </div>
            )}
          </div>
        ))}
      </div>

      {/* ── Floating data particles ── */}
      {[...Array(4)].map((_, i) => (
        <div key={i} className={`pviz-particle pviz-particle-${i + 1}`} aria-hidden="true" />
      ))}
    </div>
  );
}

// ── Pipeline steps (section) ───────────────────────────────────

function PipelineSteps() {
  return (
    <div className="ps-grid">
      {PIPELINE_STEPS.map((step, i) => (
        <div key={step.num} className="ps-step">
          <div className="ps-step-icon" style={{ background: step.bg, borderColor: `${step.color}40` }}>
            {step.icon}
          </div>
          <div className="ps-step-num" style={{ color: step.color }}>0{step.num}</div>
          <div className="ps-step-label">{step.label}</div>
          <div className="ps-step-desc">{step.desc}</div>
          {step.isAI && <div className="ps-step-ai-badge">✦ AI Powered</div>}
          {i < PIPELINE_STEPS.length - 1 && <div className="ps-step-arrow">→</div>}
        </div>
      ))}
    </div>
  );
}

// ── Main Home page ─────────────────────────────────────────────

export default function Home() {
  const { isAuthenticated } = useAuth();

  return (
    <div className="home">

      {/* ══ Navigation ══════════════════════════════════════════ */}
      <nav className="hnav">
        <div className="hnav-inner">
          <Link to="/" className="hnav-logo">
            <div className="hnav-mark">DI</div>
            <span className="hnav-name">DataIntelligence</span>
            <span className="hnav-platform">Platform</span>
          </Link>

          <ul className="hnav-links">
            {['Features', 'Solutions', 'Pricing', 'Docs'].map((l) => (
              <li key={l}><a className="hnav-link">{l}</a></li>
            ))}
          </ul>

          <div className="hnav-actions">
            {isAuthenticated ? (
              <Link to="/app/dashboard" className="hnav-btn-primary">
                Dashboard <ArrowRight size={13} />
              </Link>
            ) : (
              <>
                <Link to="/login"    className="hnav-btn-ghost">Sign in</Link>
                <a href="mailto:demo@dip.ai" className="hnav-btn-ghost">Request Demo</a>
                <Link to="/register" className="hnav-btn-primary">
                  Start Free Trial <ArrowRight size={13} />
                </Link>
              </>
            )}
          </div>
        </div>
      </nav>

      {/* ══ Hero ════════════════════════════════════════════════ */}
      <section className="hero">
        <div className="hero-grid-bg" aria-hidden="true" />
        <div className="hero-glow hero-glow-c" aria-hidden="true" />

        <div className="hero-content">
          <div className="hero-badge">
            <span className="hero-badge-dot" />
            Now with GPT-4o powered extraction
          </div>

          <h1 className="hero-h1">
            Transform Any Data Into<br />
            <span className="hero-grad">Trusted Intelligence</span>
          </h1>

          <p className="hero-sub">
            AI-powered document intelligence, data transformation, validation
            and reconciliation — one enterprise platform, zero data trust issues.
          </p>

          <div className="hero-ctas">
            <Link to="/register" className="hero-cta-primary">
              Start Free Trial
              <ArrowRight size={15} />
            </Link>
            <a href="mailto:demo@dip.ai" className="hero-cta-secondary">
              <Play size={13} className="hero-play-icon" fill="currentColor" />
              Request Demo
            </a>
            {isAuthenticated && (
              <Link to="/app/dashboard" className="hero-cta-secondary">
                Open Dashboard <ChevronRight size={14} />
              </Link>
            )}
          </div>

          <div className="hero-trust">
            {TRUST.map((t) => (
              <div key={t} className="hero-trust-item">
                <Check size={11} strokeWidth={2.5} />
                {t}
              </div>
            ))}
          </div>
        </div>

        {/* ── 3D Visualization ── */}
        <div className="hero-viz-wrap">
          <PipelineViz />
        </div>
      </section>

      {/* ══ Stats bar ══════════════════════════════════════════ */}
      <div className="stats-bar">
        <div className="stats-inner">
          {STATS.map((s, i) => (
            <div key={s.label} className="stats-item">
              <span className="stats-val">{s.value}</span>
              <span className="stats-label">{s.label}</span>
              {i < STATS.length - 1 && <div className="stats-sep" aria-hidden="true" />}
            </div>
          ))}
        </div>
      </div>

      {/* ══ Pipeline detail ════════════════════════════════════ */}
      <section className="section pipeline-section">
        <div className="section-inner">
          <div className="section-eyebrow">End-to-end pipeline</div>
          <h2 className="section-h2">From raw document to trusted data</h2>
          <p className="section-sub">
            Nine automated stages. Every step is monitored, auditable and configurable.
          </p>
          <PipelineSteps />
        </div>
      </section>

      {/* ══ Features ═══════════════════════════════════════════ */}
      <section className="section features-section">
        <div className="section-inner">
          <div className="section-eyebrow">Capabilities</div>
          <h2 className="section-h2">Everything in one platform</h2>
          <p className="section-sub">
            No more fragmented point tools. One integrated platform from ingest to target.
          </p>
          <div className="features-grid">
            {FEATURES.map((f) => (
              <div key={f.title} className="feature-card">
                <div
                  className="feature-card-icon"
                  style={{ background: `${f.color}18`, color: f.color }}
                >
                  {f.icon}
                </div>
                <h4 className="feature-card-title">{f.title}</h4>
                <p className="feature-card-desc">{f.desc}</p>
                <div className="feature-card-link" style={{ color: f.color }}>
                  Learn more <ChevronRight size={13} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ══ CTA section ════════════════════════════════════════ */}
      <section className="section cta-section">
        <div className="section-inner">
          <div className="cta-box">
            <div className="cta-glow" aria-hidden="true" />
            <div className="cta-content">
              <h2 className="cta-h2">Ready to transform your data?</h2>
              <p className="cta-sub">
                Start a free 14-day trial. Full access, no credit card required.
              </p>
              <div className="cta-actions">
                <Link to="/register" className="hero-cta-primary">
                  Start Free Trial <ArrowRight size={15} />
                </Link>
                <a href="mailto:demo@dip.ai" className="hero-cta-secondary" style={{ borderColor: 'rgba(255,255,255,0.2)', color: 'white' }}>
                  Talk to Sales
                </a>
              </div>
              <p className="cta-footnote">
                14-day trial · Full module access · No card needed
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ══ Footer ═════════════════════════════════════════════ */}
      <footer className="home-footer">
        <div className="footer-inner">
          <div className="footer-brand">
            <div className="hnav-mark" style={{ width: 26, height: 26, fontSize: 10 }}>DI</div>
            <span className="footer-copy">
              DataIntelligence Platform © {new Date().getFullYear()}
            </span>
          </div>
          <div className="footer-links">
            {['Privacy', 'Terms', 'Security', 'Status', 'Contact'].map((l) => (
              <a key={l} className="footer-link">{l}</a>
            ))}
          </div>
        </div>
      </footer>

    </div>
  );
}
