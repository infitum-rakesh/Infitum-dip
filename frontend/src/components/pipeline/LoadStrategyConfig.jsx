/**
 * LoadStrategyConfig — the config-panel body for a 'target' node's
 * Dataset (database) load behaviour: Full Load vs Delta Load, and for
 * Delta, real SCD Type 1 / Type 2 merge configuration (business key,
 * change detection, SCD2 effective-dating columns).
 *
 * All of this is real, not a UI-only mock: the fields written here map
 * directly onto backend/app/execution/pipeline_job_executor.py's target
 * node config and are executed for real by backend/app/loading/scd.py —
 * a genuine vendor-native upsert (Postgres ON CONFLICT / MySQL ON
 * DUPLICATE KEY UPDATE / SQL Server MERGE) for SCD1, and a real
 * transaction-scoped close-then-insert for SCD2. See that module's
 * docstring for the merge semantics.
 *
 * File targets don't use this component — see the plain Write Mode
 * dropdown still rendered inline in TransformationDesigner.jsx for
 * targetType === 'file' (file writes have no concept of upsert/SCD).
 */
import { useMemo, useState } from 'react';
import { AlertTriangle, Link2, Plus, X } from 'lucide-react';
import { mappingApi } from '@/services/api/mappingApi.js';
import { findNearestNodeOfType } from './nodeGraphUtils.js';
import './pipeline.css';

const FULL_STRATEGIES = [
  { value: 'create_if_not_exists', label: 'Create if not exists + Load', help: 'Creates the table if missing. If it already exists, rows are appended without dropping or truncating anything.' },
  { value: 'truncate_load', label: 'Truncate + Load', help: 'Empties the existing table (or creates it if missing), then loads the new rows.' },
  { value: 'drop_recreate', label: 'Drop + Recreate + Load', help: 'Destructive — drops the existing table and rebuilds it from this run\'s output columns. Use when the source structure has changed.' },
  { value: 'append', label: 'Append', help: 'Keeps existing rows and inserts the new dataset rows on top.' },
];

function ChipList({ values, onChange, placeholder, columnOptions }) {
  const add = (v) => {
    const val = (v ?? '').trim();
    if (!val || values.includes(val)) return;
    onChange([...values, val]);
  };
  const remove = (v) => onChange(values.filter((x) => x !== v));

  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 6 }}>
        {values.map((v) => (
          <span key={v} className="ls-chip">
            {v}
            <button type="button" onClick={() => remove(v)} aria-label={`Remove ${v}`}><X size={11} /></button>
          </span>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 6 }}>
        {columnOptions && columnOptions.length > 0 ? (
          <select
            className="pc-config-select"
            value=""
            onChange={(e) => { if (e.target.value) add(e.target.value); }}
          >
            <option value="">{placeholder}</option>
            {columnOptions.filter((c) => !values.includes(c)).map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        ) : (
          <input
            className="pc-config-input"
            placeholder={placeholder}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); add(e.target.value); e.target.value = ''; }
            }}
          />
        )}
      </div>
    </div>
  );
}

export function LoadStrategyConfig({ cfg, set, sourceColumns = [], nodeId, nodes = [], edges = [] }) {
  const loadType = cfg.loadType ?? 'full';
  const deltaStrategy = cfg.deltaStrategy ?? 'scd1';
  const businessKeys = cfg.businessKeys ?? [];
  const changeDetection = cfg.changeDetection ?? 'auto';
  const changedColumns = cfg.changedColumns ?? [];
  const nonKeyColumns = sourceColumns.filter((c) => !businessKeys.includes(c));

  // CM7 — "Use business keys from upstream Column Mapping": a ONE-TIME
  // copy of whichever target columns that mapping's own editor flagged
  // is_key into this node's businessKeys, at click time (mirrored again
  // at canvas Save, since businessKeys is plain node config like any
  // other field). Never a live reference — _run_target only ever reads
  // cfg["businessKeys"] with no knowledge mappings exist at all (see
  // execution/pipeline_job_executor.py), so editing the mapping's key
  // flags later, or even Upgrading this node to a newer mapping version,
  // does NOT retroactively change what SCD1/SCD2 key on here. Re-click
  // the button after an Upgrade if the new version's keys differ.
  const nodesById = useMemo(() => Object.fromEntries(nodes.map((n) => [n.id, n])), [nodes]);
  const upstreamMapping = useMemo(
    () => (nodeId ? findNearestNodeOfType(nodeId, 'column_mapping_step', edges, nodesById, 'upstream') : null),
    [nodeId, edges, nodesById],
  );
  const [loadingKeys, setLoadingKeys] = useState(false);
  const [keysError, setKeysError] = useState(null);

  const useKeysFromMapping = async () => {
    const mappingId = upstreamMapping?.config?.mappingId;
    const mappingVersion = upstreamMapping?.config?.mappingVersion;
    if (!mappingId || !mappingVersion) return;
    setLoadingKeys(true);
    setKeysError(null);
    try {
      const version = await mappingApi.getVersion(mappingId, mappingVersion);
      const keyColumns = (version.mappings || []).filter((m) => m.is_key).map((m) => m.target_column);
      if (keyColumns.length === 0) {
        setKeysError('That mapping has no column flagged as a business key yet — flag at least one row\'s "is_key" in the Column Mapping editor first.');
        return;
      }
      set('businessKeys', keyColumns);
    } catch {
      setKeysError('Could not load that mapping\'s key columns — try again, or enter them manually below.');
    } finally {
      setLoadingKeys(false);
    }
  };

  return (
    <div className="ls-panel">
      <div className="pc-config-row">
        <label className="pc-config-label">Load Type</label>
        <div className="ls-radio-group">
          {[['full', 'Full Load'], ['delta', 'Delta Load']].map(([v, label]) => (
            <label key={v} className="ls-radio">
              <input type="radio" name="loadType" checked={loadType === v} onChange={() => set('loadType', v)} />
              {label}
            </label>
          ))}
        </div>
      </div>

      {loadType === 'full' && (
        <div className="pc-config-row">
          <label className="pc-config-label">Load Strategy</label>
          <div className="ls-radio-group ls-radio-group-col">
            {FULL_STRATEGIES.map((s) => (
              <label key={s.value} className="ls-radio ls-radio-block">
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <input
                    type="radio"
                    name="fullLoadStrategy"
                    checked={(cfg.fullLoadStrategy ?? 'create_if_not_exists') === s.value}
                    onChange={() => set('fullLoadStrategy', s.value)}
                  />
                  {s.label}
                </div>
                <span className="ls-radio-help">{s.help}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {loadType === 'delta' && (
        <>
          <div className="pc-config-row">
            <label className="pc-config-label">Delta Strategy</label>
            <div className="ls-radio-group">
              {[['scd1', 'SCD Type 1'], ['scd2', 'SCD Type 2']].map(([v, label]) => (
                <label key={v} className="ls-radio">
                  <input type="radio" name="deltaStrategy" checked={deltaStrategy === v} onChange={() => set('deltaStrategy', v)} />
                  {label}
                </label>
              ))}
            </div>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
              {deltaStrategy === 'scd1'
                ? 'Overwrites the matching row in place — no history is kept.'
                : 'Keeps every version of a row — the old version is closed out and a new current version is inserted.'}
            </span>
          </div>

          <div className="pc-config-row">
            <label className="pc-config-label">
              Business Key <span style={{ color: 'var(--color-danger)' }}>*</span>
            </label>
            {upstreamMapping && (
              <button
                type="button" className="sdc-secondary-btn" disabled={loadingKeys}
                style={{ marginBottom: 6 }} onClick={useKeysFromMapping}
              >
                <Link2 size={12} /> {loadingKeys ? 'Loading…' : 'Use business keys from Column Mapping'}
              </button>
            )}
            {keysError && (
              <span className="sdc-inline-warning" style={{ marginBottom: 6 }}><AlertTriangle size={11} /> {keysError}</span>
            )}
            <ChipList
              values={businessKeys}
              onChange={(v) => set('businessKeys', v)}
              placeholder={sourceColumns.length ? '+ Add key column' : 'Type a column name, press Enter'}
              columnOptions={sourceColumns}
            />
            {businessKeys.length === 0 && (
              <span style={{ fontSize: 11, color: 'var(--color-danger)', marginTop: 3 }}>
                Required — the pipeline cannot run without at least one business key column.
              </span>
            )}
            {upstreamMapping && (
              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
                A one-time copy from the upstream Column Mapping node's is_key-flagged columns — editing that mapping later, or upgrading this node's mapping version, never changes these keys automatically.
              </span>
            )}
          </div>

          <div className="pc-config-row">
            <label className="pc-config-label">Changed Columns</label>
            <div className="ls-radio-group">
              {[['auto', 'Auto Detect'], ['manual', 'Manual Selection']].map(([v, label]) => (
                <label key={v} className="ls-radio">
                  <input type="radio" name="changeDetection" checked={changeDetection === v} onChange={() => set('changeDetection', v)} />
                  {label}
                </label>
              ))}
            </div>
            {changeDetection === 'manual' && (
              <div style={{ marginTop: 8 }}>
                <ChipList
                  values={changedColumns}
                  onChange={(v) => set('changedColumns', v)}
                  placeholder={nonKeyColumns.length ? '+ Add column to compare' : 'Type a column name, press Enter'}
                  columnOptions={nonKeyColumns}
                />
              </div>
            )}
            <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>
              A deterministic hash over these columns decides whether a row actually changed — unchanged rows are never written. Key columns are never eligible.
            </span>
          </div>

          {deltaStrategy === 'scd2' && (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">Effective Start Column</label>
                <input
                  className="pc-config-input"
                  value={cfg.scd2StartColumn ?? ''}
                  placeholder="effective_start_date"
                  onChange={(e) => set('scd2StartColumn', e.target.value)}
                />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Effective End Column</label>
                <input
                  className="pc-config-input"
                  value={cfg.scd2EndColumn ?? ''}
                  placeholder="effective_end_date"
                  onChange={(e) => set('scd2EndColumn', e.target.value)}
                />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Current Flag Column</label>
                <input
                  className="pc-config-input"
                  value={cfg.scd2CurrentColumn ?? ''}
                  placeholder="is_current"
                  onChange={(e) => set('scd2CurrentColumn', e.target.value)}
                />
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
