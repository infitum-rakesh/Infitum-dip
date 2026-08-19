/**
 * DocumentIntelligence — pipeline coordinator.
 * Owns all pipeline state; renders the active stage and the stepper.
 * Stage components call onComplete(result) to advance; onBack() to retreat.
 *
 * Pipeline:
 *   Stage 0: ImageScanner  → produces scanData
 *   Stage 1: OCR           → produces ocrData
 *   Stage 2: AIExtraction  → produces extractionData
 *   Stage 3: StructuredDataset → final output
 */
import { useReducer } from 'react';
import { useNavigate } from 'react-router-dom';
import { Check, AlertCircle } from 'lucide-react';
import { ImageScanner }     from './ImageScanner.jsx';
import { OCR }              from './OCR.jsx';
import { AIExtraction }     from './AIExtraction.jsx';
import { StructuredDataset } from './StructuredDataset.jsx';
import './document-intelligence.css';

// ── Stage definitions ──────────────────────────────────────────

const STAGES = [
  { id: 'scanner',   label: 'Image Scanner',      icon: '🔬' },
  { id: 'ocr',       label: 'OCR',                icon: '📝' },
  { id: 'ai-extract',label: 'AI Extraction',      icon: '🧠' },
  { id: 'dataset',   label: 'Structured Dataset', icon: '🗂️' },
];

// ── Reducer ────────────────────────────────────────────────────

const INIT = {
  currentStage: 0,
  scanData:       null,
  ocrData:        null,
  extractionData: null,
  datasetResult:  null,
};

function reducer(state, action) {
  switch (action.type) {
    case 'COMPLETE_SCAN':
      return { ...state, scanData: action.data, currentStage: 1 };
    case 'COMPLETE_OCR':
      return { ...state, ocrData: action.data, currentStage: 2 };
    case 'COMPLETE_EXTRACTION':
      return { ...state, extractionData: action.data, currentStage: 3 };
    case 'COMPLETE_DATASET':
      return { ...state, datasetResult: action.data };
    case 'GO_TO_STAGE':
      // Only allow navigating to completed stages
      if (action.stage < state.currentStage) return { ...state, currentStage: action.stage };
      return state;
    default:
      return state;
  }
}

// ── Stepper ────────────────────────────────────────────────────

function PipelineStepper({ stages, currentStage, onStepClick }) {
  return (
    <div className="di-stepper">
      {stages.map((stage, i) => {
        const isDone   = i < currentStage;
        const isActive = i === currentStage;
        const status   = isDone ? 'done' : isActive ? 'active' : 'pending';

        return (
          <div key={stage.id} style={{ display: 'flex', alignItems: 'center', flex: i < stages.length - 1 ? '1' : 'none' }}>
            <div
              className={`di-step ${status}`}
              onClick={() => isDone && onStepClick(i)}
              style={{ cursor: isDone ? 'pointer' : 'default' }}
              title={isDone ? `Go back to ${stage.label}` : undefined}
            >
              <div className="di-step-circle">
                {isDone ? <Check size={15} strokeWidth={2.5} /> : <span>{i + 1}</span>}
              </div>
              <span className="di-step-label">{stage.label}</span>
            </div>

            {i < stages.length - 1 && (
              <div
                className={`di-connector ${isDone ? 'done' : isActive ? 'active' : ''}`}
                aria-hidden="true"
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────

export default function DocumentIntelligence() {
  const [state, dispatch] = useReducer(reducer, INIT);
  const navigate = useNavigate();

  const { currentStage, scanData, ocrData, extractionData } = state;

  return (
    <div>
      {/* Page header */}
      <div style={{ marginBottom: 6 }}>
        <h1 style={{ fontSize: 'var(--fs-3xl)', fontWeight: 'var(--fw-bold)', color: 'var(--text-primary)', lineHeight: 1.2 }}>
          Document Intelligence
        </h1>
        <p style={{ fontSize: 'var(--fs-md)', color: 'var(--text-muted)', marginTop: 3 }}>
          Turn documents and images into trusted structured data — Image Scanner → OCR → AI Extraction → Dataset
        </p>
      </div>

      {/* Pipeline stepper */}
      <PipelineStepper
        stages={STAGES}
        currentStage={currentStage}
        onStepClick={(i) => dispatch({ type: 'GO_TO_STAGE', stage: i })}
      />

      {/* Active stage */}
      {currentStage === 0 && (
        <ImageScanner
          onComplete={(data) => dispatch({ type: 'COMPLETE_SCAN', data })}
        />
      )}

      {currentStage === 1 && scanData && (
        <OCR
          scanData={scanData}
          onBack={() => dispatch({ type: 'GO_TO_STAGE', stage: 0 })}
          onComplete={(data) => dispatch({ type: 'COMPLETE_OCR', data })}
        />
      )}

      {currentStage === 2 && ocrData && (
        <AIExtraction
          ocrData={ocrData}
          onBack={() => dispatch({ type: 'GO_TO_STAGE', stage: 1 })}
          onComplete={(data) => dispatch({ type: 'COMPLETE_EXTRACTION', data })}
        />
      )}

      {currentStage === 3 && extractionData && (
        <StructuredDataset
          extractionData={extractionData}
          onBack={() => dispatch({ type: 'GO_TO_STAGE', stage: 2 })}
          onContinue={(initialPipeline) => {
            // initialPipeline already has the extracted data uploaded as a
            // real file and wired into a Source node — see
            // StructuredDataset.jsx's handleContinue(). Handing it to
            // Transform Workspace via router state (not just a bare
            // navigate()) is what stops the extracted data being silently
            // dropped when this page used to just navigate('/app/transform').
            dispatch({ type: 'COMPLETE_DATASET', data: initialPipeline });
            navigate('/app/transform', { state: { initialPipeline } });
          }}
        />
      )}

      {/* Guard: should not happen, but prevents blank screen */}
      {currentStage > 0 && !scanData && currentStage !== 0 && (
        <div style={{ padding: 32, textAlign: 'center', color: 'var(--color-danger)' }}>
          <AlertCircle size={28} style={{ margin: '0 auto 8px' }} />
          <p>Pipeline state was lost. Please <button onClick={() => dispatch({ type: 'GO_TO_STAGE', stage: 0 })} style={{ color: 'var(--color-primary)', background: 'none', border: 'none', cursor: 'pointer', fontWeight: 600 }}>start again</button>.</p>
        </div>
      )}
    </div>
  );
}
