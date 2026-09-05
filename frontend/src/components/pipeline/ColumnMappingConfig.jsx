/**
 * ColumnMappingConfig — the config-panel body for the 'column_mapping_step'
 * node type (Column Mapping <-> Pipeline Builder integration build).
 *
 * Mirrors AnomalyDetectionConfig.jsx's "reference a saved entity, execute
 * for real" shape, extended per the spec: a Version picker (only
 * PUBLISHED versions are selectable — enforced again server-side at
 * execution time, never trusted from this filtered list alone), Create
 * New Mapping / Edit Mapping launch actions, inline Validate, a
 * staleness ("MAPPING OUT OF DATE") check, and a source/target
 * consistency check against this node's resolved upstream Source
 * Dataset / downstream Target node.
 *
 * "Create New Mapping" opens the real, separate Column Mapping module
 * (ObjectColumnMapping.jsx) in a new tab, pre-populated from this node's
 * resolved source/target refs — never a duplicated mapping editor here.
 * Publishing there posts a `column-mapping-published` message back to
 * this tab (window.opener), which is how the attach becomes automatic
 * without risking this pipeline's own unsaved canvas state by navigating
 * away in-place.
 */
import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, ArrowUpCircle, ExternalLink, Plus, RefreshCw, ShieldCheck } from 'lucide-react';
import { mappingApi } from '@/services/api/mappingApi.js';
import { pipelineMappingApi } from '@/services/api/pipelineMappingApi.js';
import { findNearestNodeOfType } from './nodeGraphUtils.js';
import './pipeline.css';

export function ColumnMappingConfig({ cfg, set, pipelineId, nodeId, nodes = [], edges = [] }) {
  const [mappings, setMappings] = useState([]);
  const [loadingMappings, setLoadingMappings] = useState(true);
  const [versions, setVersions] = useState([]);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [validateResult, setValidateResult] = useState(null);
  const [validating, setValidating] = useState(false);
  const [staleness, setStaleness] = useState(null);
  const [checkingStale, setCheckingStale] = useState(false);
  const [consistency, setConsistency] = useState(null);
  const [attaching, setAttaching] = useState(false);
  const [upgrading, setUpgrading] = useState(false);
  const [upgradeNotice, setUpgradeNotice] = useState('');

  const nodesById = useMemo(() => Object.fromEntries(nodes.map((n) => [n.id, n])), [nodes]);
  const upstreamSource = useMemo(() => (nodeId ? findNearestNodeOfType(nodeId, 'source', edges, nodesById, 'upstream') : null), [nodeId, edges, nodesById]);
  const downstreamTarget = useMemo(() => (nodeId ? findNearestNodeOfType(nodeId, 'target', edges, nodesById, 'downstream') : null), [nodeId, edges, nodesById]);

  useEffect(() => {
    mappingApi.list({ status: 'PUBLISHED' }).then((r) => setMappings(r.items)).catch(() => setMappings([])).finally(() => setLoadingMappings(false));
  }, []);

  useEffect(() => {
    if (!cfg.mappingId) { setVersions([]); return; }
    setLoadingVersions(true);
    mappingApi.listVersions(cfg.mappingId, { status: 'PUBLISHED' }).then((r) => setVersions(r.items)).catch(() => setVersions([])).finally(() => setLoadingVersions(false));
  }, [cfg.mappingId]);

  // Auto-attach round trip from "Create New Mapping" — see module docstring.
  useEffect(() => {
    function onMessage(event) {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== 'column-mapping-published') return;
      set('mappingId', event.data.mappingId);
      set('mappingVersion', event.data.mappingVersion);
      setValidateResult(null); setStaleness(null); setConsistency(null);
    }
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [set]);

  const selectedMapping = mappings.find((m) => m.id === cfg.mappingId);

  const handlePickMapping = (mappingId) => {
    set('mappingId', mappingId || null);
    set('mappingVersion', null);
    setValidateResult(null); setStaleness(null); setConsistency(null);
  };

  const handlePickVersion = async (versionNumber) => {
    set('mappingVersion', versionNumber || null);
    setValidateResult(null); setStaleness(null); setConsistency(null);
    if (!versionNumber || !cfg.mappingId) return;
    // Best-effort — registers usage + runs the real server-side
    // consistency check. If the pipeline hasn't been saved yet (no id),
    // this is simply skipped; the config still applies on Apply, and the
    // consistency check re-runs the next time this panel is opened after
    // the pipeline (and thus its id) exists.
    if (!pipelineId) return;
    setAttaching(true);
    try {
      const result = await pipelineMappingApi.attach(pipelineId, {
        node_id: nodeId, mapping_id: cfg.mappingId, mapping_version: versionNumber,
        pipeline_source_connection_id: upstreamSource?.config?.connectionId ?? undefined,
        pipeline_source_schema: upstreamSource?.config?.schema ?? undefined,
        pipeline_source_object: upstreamSource?.config?.object ?? undefined,
        pipeline_target_connection_id: downstreamTarget?.config?.connectionId ?? undefined,
        pipeline_target_object: downstreamTarget?.config?.datasetName ?? undefined,
      });
      setConsistency(result);
    } catch {
      // Non-fatal — attach is a convenience registration + check, not a gate.
    } finally { setAttaching(false); }
  };

  const runValidate = async () => {
    if (!cfg.mappingId || !cfg.mappingVersion) return;
    setValidating(true);
    try { setValidateResult(await mappingApi.validateVersion(cfg.mappingId, cfg.mappingVersion)); }
    catch { setValidateResult(null); }
    finally { setValidating(false); }
  };

  const runStaleness = async () => {
    if (!cfg.mappingId || !cfg.mappingVersion) return;
    setCheckingStale(true);
    try { setStaleness(await mappingApi.getStaleness(cfg.mappingId, cfg.mappingVersion)); }
    catch { setStaleness(null); }
    finally { setCheckingStale(false); }
  };

  const openCreateNew = () => {
    const params = new URLSearchParams({ returnMode: 'pipelineNode' });
    if (upstreamSource?.config?.connectionId) {
      params.set('sourceConnectionId', upstreamSource.config.connectionId);
      if (upstreamSource.config.schema) params.set('sourceSchema', upstreamSource.config.schema);
      if (upstreamSource.config.object) params.set('sourceObject', upstreamSource.config.object);
    }
    if (downstreamTarget?.config?.connectionId) {
      params.set('targetConnectionId', downstreamTarget.config.connectionId);
      if (downstreamTarget.config.datasetName) params.set('targetObject', downstreamTarget.config.datasetName);
    }
    window.open(`/app/mapping?${params.toString()}`, '_blank');
  };

  const openEdit = () => { if (cfg.mappingId) window.open(`/app/mapping/${cfg.mappingId}`, '_blank'); };

  // CM11 — visible whenever this node's pinned version isn't the
  // mapping's current published version. Calls the real
  // /pipelines/{id}/mapping/upgrade endpoint, which writes a brand new
  // PipelineVersion server-side via pipeline_service.save_version()
  // (history is never mutated) — NOT the normal canvas Apply/Save flow.
  // Updates this node's own draft field afterward so the panel reflects
  // it immediately; other unsaved canvas edits are untouched, but since
  // the backend's current pipeline version has now moved on, reload the
  // pipeline before saving further canvas changes to avoid clobbering
  // this upgrade with stale in-memory canvas state.
  const canUpgrade = pipelineId && nodeId && selectedMapping?.current_published_version
    && cfg.mappingVersion && selectedMapping.current_published_version !== cfg.mappingVersion;

  const runUpgrade = async () => {
    if (!canUpgrade) return;
    setUpgrading(true); setUpgradeNotice('');
    try {
      const result = await pipelineMappingApi.upgrade(pipelineId, { node_id: nodeId, to_version: selectedMapping.current_published_version });
      set('mappingVersion', result.to_version);
      setValidateResult(null); setStaleness(null); setConsistency(null);
      setUpgradeNotice(
        `Upgraded to v${result.to_version} — saved as pipeline version ${result.new_pipeline_version}. `
        + `Reload this pipeline before saving any other canvas changes so you're editing on top of that new version.`
      );
    } catch {
      setUpgradeNotice('Upgrade failed — try again.');
    } finally { setUpgrading(false); }
  };

  return (
    <>
      <div className="pc-config-row">
        <label className="pc-config-label">Column Mapping <span style={{ fontWeight: 400, color: 'var(--text-muted)' }}>(only PUBLISHED mappings shown)</span></label>
        <select className="pc-config-select" value={cfg.mappingId ?? ''} onChange={(e) => handlePickMapping(e.target.value ? Number(e.target.value) : null)}>
          <option value="">{loadingMappings ? 'Loading…' : 'Select a mapping…'}</option>
          {mappings.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
        </select>
        {!loadingMappings && mappings.length === 0 && (
          <span className="sdc-inline-warning"><AlertTriangle size={11} /> No published mappings yet — create one below.</span>
        )}
      </div>

      {cfg.mappingId && (
        <div className="pc-config-row">
          <label className="pc-config-label">Version</label>
          <select className="pc-config-select" value={cfg.mappingVersion ?? ''} onChange={(e) => handlePickVersion(e.target.value ? Number(e.target.value) : null)}>
            <option value="">{loadingVersions ? 'Loading…' : 'Select a version…'}</option>
            {versions.map((v) => (
              <option key={v.version_number} value={v.version_number}>
                v{v.version_number}{v.version_number === selectedMapping?.current_published_version ? ' (current published)' : ''}
              </option>
            ))}
          </select>
          {attaching && <span className="ocm-muted" style={{ fontSize: 11 }}>Checking compatibility…</span>}
        </div>
      )}

      <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
        <button type="button" className="sdc-secondary-btn" onClick={openCreateNew}><Plus size={12} /> Create New Mapping</button>
        <button type="button" className="sdc-secondary-btn" disabled={!cfg.mappingId} onClick={openEdit}><ExternalLink size={12} /> Edit Mapping</button>
      </div>

      {cfg.mappingId && cfg.mappingVersion && (
        <div style={{ display: 'flex', gap: 6, marginBottom: 10, flexWrap: 'wrap' }}>
          <button type="button" className="sdc-secondary-btn" disabled={validating} onClick={runValidate}>
            <ShieldCheck size={12} /> {validating ? 'Validating…' : 'Validate'}
          </button>
          <button type="button" className="sdc-secondary-btn" disabled={checkingStale} onClick={runStaleness}>
            <RefreshCw size={12} /> {checkingStale ? 'Checking…' : 'Refresh Metadata'}
          </button>
          {canUpgrade && (
            <button type="button" className="sdc-secondary-btn" disabled={upgrading} onClick={runUpgrade}>
              <ArrowUpCircle size={12} /> {upgrading ? 'Upgrading…' : `Upgrade to v${selectedMapping.current_published_version}`}
            </button>
          )}
        </div>
      )}

      {upgradeNotice && (
        <div className="pc-config-notice">
          <ArrowUpCircle size={13} />
          <span>{upgradeNotice}</span>
        </div>
      )}

      {consistency && !consistency.consistent && (
        <div className="pc-config-notice" style={{ borderColor: 'var(--color-danger)' }}>
          <AlertTriangle size={13} />
          <span>
            Selected mapping is not compatible with this pipeline's source/target:
            <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
              {consistency.issues.map((iss, i) => <li key={i}>{iss.message}</li>)}
            </ul>
            Pick a different mapping, or update the upstream/downstream node to match.
          </span>
        </div>
      )}

      {staleness?.stale && (
        <div className="pc-config-notice">
          <AlertTriangle size={13} />
          <span>
            <strong>MAPPING OUT OF DATE</strong> — the source/target object has changed since this version was created:
            <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
              {staleness.issues.map((iss, i) => <li key={i}>{iss}</li>)}
            </ul>
            This never updates the pinned version automatically — review the drift and Upgrade explicitly if needed.
          </span>
        </div>
      )}

      {validateResult && (
        <div className={`pc-config-notice`} style={{ borderColor: validateResult.overall_status === 'CONFLICT' ? 'var(--color-danger)' : undefined }}>
          <ShieldCheck size={13} />
          <span>
            <strong>{validateResult.overall_status}</strong> — {validateResult.issues.filter((i) => i.severity !== 'INFO').length} issue(s) against the live schema right now.
          </span>
        </div>
      )}

      <div className="pc-config-notice">
        <AlertTriangle size={13} />
        <span>
          Resolves this exact (mapping, version) pair at execution time and applies its real rename/transform/lookup/constant/default rules to the in-flight data — never a cosmetic rename at the end. Only a PUBLISHED version can run in production (re-checked server-side regardless of what's shown here).
        </span>
      </div>
    </>
  );
}
