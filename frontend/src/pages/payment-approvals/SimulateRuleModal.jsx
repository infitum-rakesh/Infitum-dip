/**
 * SimulateRuleModal — dry-runs the real rule engine (rule_engine_service.
 * evaluate(), via POST /approvals/rules/simulate) against a hypothetical
 * invoice. Changes nothing; the backend logs a SIMULATION_RUN audit
 * event but no workflow is created. Useful for an admin to answer "which
 * rule would fire for a $12,000 USD invoice from this supplier?" before
 * real invoices start flowing through it.
 */
import { useState } from 'react';
import { PlayCircle } from 'lucide-react';
import { Modal, Button, StatusBadge } from '@/components/common/index.js';
import { paymentApprovalApi } from '@/services/api/paymentApprovalApi.js';
import './payment-approvals.css';

const CONDITION_KEYS = ['SUPPLIER', 'BUSINESS_UNIT', 'DEPARTMENT', 'COST_CENTER', 'COMPANY', 'PAYMENT_METHOD', 'SOURCE_SYSTEM'];

export function SimulateRuleModal({ open, onClose }) {
  const [amount, setAmount] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [invoiceType, setInvoiceType] = useState('');
  const [conditions, setConditions] = useState({});
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);

  const handleClose = () => { setResult(null); setError(''); onClose(); };

  const run = async () => {
    setRunning(true);
    setError('');
    setResult(null);
    try {
      const res = await paymentApprovalApi.simulateRule({
        amount: amount === '' ? null : Number(amount),
        currency: currency.trim() || null,
        invoice_type: invoiceType.trim() || null,
        conditions: Object.fromEntries(Object.entries(conditions).filter(([, v]) => v.trim())),
      });
      setResult(res);
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Simulation failed.');
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Simulate Rule Evaluation"
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={handleClose}>Close</Button>
          <Button variant="primary" icon={<PlayCircle size={14} />} loading={running} onClick={run}>Run Simulation</Button>
        </>
      }
    >
      <div className="par-grid-3">
        <div className="par-field">
          <label className="par-label">Amount</label>
          <input className="par-input" type="number" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="e.g. 12000" />
        </div>
        <div className="par-field">
          <label className="par-label">Currency</label>
          <input className="par-input" value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} maxLength={8} />
        </div>
        <div className="par-field">
          <label className="par-label">Invoice Type</label>
          <input className="par-input" value={invoiceType} onChange={(e) => setInvoiceType(e.target.value)} placeholder="optional" />
        </div>
      </div>

      <div className="par-section-title">Conditions (optional)</div>
      <div className="par-grid-2">
        {CONDITION_KEYS.map((k) => (
          <div key={k} className="par-field">
            <label className="par-label">{k}</label>
            <input className="par-input" value={conditions[k] || ''} onChange={(e) => setConditions((c) => ({ ...c, [k]: e.target.value }))} />
          </div>
        ))}
      </div>

      {error && <div className="par-form-errors">{error}</div>}

      {result && (
        <div className="par-sim-result">
          <div className="par-section-title">Result</div>
          {result.conflict ? (
            <div className="par-sim-conflict">
              <StatusBadge variant="danger" dot>RULE_CONFLICT</StatusBadge>
              <span> — {result.conflicting_rule_ids.length} equally-specific rules tie: #{result.conflicting_rule_ids.join(', #')}</span>
            </div>
          ) : result.winner ? (
            <div className="par-sim-winner">
              <StatusBadge variant="success" dot>MATCHED</StatusBadge>
              <span> — <strong>{result.winner.rule_name}</strong> v{result.winner.version} (priority {result.winner.priority}, specificity {result.winner.specificity})</span>
            </div>
          ) : (
            <div className="par-sim-nomatch">
              <StatusBadge variant="gray" dot>NO MATCH</StatusBadge>
              <span> — no rule matched this hypothetical invoice ({result.candidates_considered} candidate(s) considered).</span>
            </div>
          )}

          {result.evaluations.length > 0 && (
            <table className="par-sim-table">
              <thead>
                <tr><th>Rule</th><th>Matched</th><th>Reason</th></tr>
              </thead>
              <tbody>
                {result.evaluations.map((e) => (
                  <tr key={`${e.rule_id}-${e.version}`}>
                    <td>{e.rule_name} v{e.version}</td>
                    <td>{e.matched ? <StatusBadge variant="success" dot>Yes</StatusBadge> : <StatusBadge variant="gray" dot>No</StatusBadge>}</td>
                    <td>{e.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </Modal>
  );
}
