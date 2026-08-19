/**
 * PipelineToolbar — action bar at the top of the canvas.
 *
 * Props:
 *   canUndo, canRedo       : booleans
 *   onUndo, onRedo         : () => void
 *   onRun, onSave          : () => void
 *   onFitView              : () => void
 *   onZoomIn, onZoomOut    : () => void
 *   zoom                   : number (0.25–2)
 *   status                 : 'idle'|'running'|'success'|'error'
 *   pipelineName           : string
 *   onNameChange           : (name) => void
 *   onClearCanvas          : () => void
 *   dirty                  : boolean | undefined — when provided, shows an
 *                             "Unsaved changes" indicator and disables Save
 *                             while false. Omit to leave Save always active
 *                             (existing Pipeline Builder behaviour).
 *   onHistory              : () => void — when provided, shows a History button.
 */
import { Undo2, Redo2, Play, Save, ZoomIn, ZoomOut, Maximize2, Trash2, History } from 'lucide-react';
import { Button } from '@/components/common/Button.jsx';
import { StatusBadge } from '@/components/common/StatusBadge.jsx';
import './pipeline.css';

const STATUS_VARIANT = { idle: 'gray', running: 'info', success: 'success', error: 'danger' };
const STATUS_LABEL   = { idle: 'Not run', running: 'Running…', success: 'Completed', error: 'Failed' };

export function PipelineToolbar({
  canUndo = false, canRedo = false,
  onUndo, onRedo,
  onRun, onSave,
  onFitView,
  onZoomIn, onZoomOut,
  zoom = 1,
  status = 'idle',
  pipelineName = 'Untitled Pipeline',
  onNameChange,
  onClearCanvas,
  dirty,
  onHistory,
  versionLabel,
}) {
  return (
    <div className="pc-toolbar">
      {/* Pipeline name (inline-editable) */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <input
          className="pc-config-input"
          style={{ width: 200, padding: '4px 8px', fontSize: 13, fontWeight: 600 }}
          value={pipelineName}
          onChange={(e) => onNameChange?.(e.target.value)}
          placeholder="Pipeline name…"
        />
        {versionLabel && <span className="pc-version-pill">{versionLabel}</span>}
        {dirty === true && (
          <span className="pc-dirty-pill" title="You have unsaved changes">● Unsaved changes</span>
        )}
      </div>

      <div className="pc-toolbar-sep" />

      {/* Undo / Redo */}
      <Button variant="ghost" size="sm" onClick={onUndo} disabled={!canUndo} title="Undo (Ctrl+Z)">
        <Undo2 size={14} />
      </Button>
      <Button variant="ghost" size="sm" onClick={onRedo} disabled={!canRedo} title="Redo (Ctrl+Y)">
        <Redo2 size={14} />
      </Button>

      <div className="pc-toolbar-sep" />

      {/* Save — disabled while there's nothing to save, when the parent tracks dirty state */}
      <Button variant="secondary" size="sm" icon={<Save size={13} />} onClick={onSave} disabled={dirty === false}>
        Save
      </Button>

      {/* Version History */}
      {onHistory && (
        <Button variant="ghost" size="sm" icon={<History size={13} />} onClick={onHistory}>History</Button>
      )}

      {/* Run — only shown where the parent actually wires up execution.
          Transform Workspace no longer runs pipelines from the canvas;
          running always happens from Pipeline Builder (see PipelineBuilder.jsx). */}
      {onRun && (
        <>
          <Button
            variant={status === 'running' ? 'warning' : 'primary'}
            size="sm"
            icon={<Play size={13} fill="currentColor" />}
            loading={status === 'running'}
            onClick={onRun}
          >
            {status === 'running' ? 'Running…' : 'Run Pipeline'}
          </Button>

          {/* Status */}
          <StatusBadge variant={STATUS_VARIANT[status] ?? 'gray'} dot>
            {STATUS_LABEL[status] ?? status}
          </StatusBadge>
        </>
      )}

      <div className="pc-toolbar-sep" />

      {/* Clear */}
      <Button variant="ghost" size="sm" onClick={onClearCanvas} title="Clear canvas">
        <Trash2 size={13} />
      </Button>

      {/* Zoom controls */}
      <div className="pc-toolbar-zoom">
        <Button variant="ghost" size="sm" onClick={onZoomOut} title="Zoom out"><ZoomOut size={13} /></Button>
        <span className="pc-toolbar-label">{Math.round(zoom * 100)}%</span>
        <Button variant="ghost" size="sm" onClick={onZoomIn} title="Zoom in"><ZoomIn size={13} /></Button>
        <Button variant="ghost" size="sm" onClick={onFitView} title="Fit to view"><Maximize2 size={13} /></Button>
      </div>
    </div>
  );
}
