/**
 * AnomalyScheduleCard — AD12: real scheduling for one saved AnomalyJob,
 * reusing the platform's existing Schedule/ScheduleRun infrastructure
 * (scheduleApi.js, the same schedules table Pipeline schedules use) rather
 * than inventing a parallel scheduling system. A Schedule row with
 * anomaly_job_id set to this job dispatches to a REAL execution via
 * backend/app/services/schedule_service.py's execute_schedule() ->
 * anomaly_service.run_job() (see that function's AD12 docstring) — not a
 * simulation, unlike every pipeline-linked schedule (no second execution
 * engine exists for those yet).
 *
 * One schedule per anomaly job (kept simple, mirrors the "single
 * schedule_frequency" shape AD1's AnomalyJob model already anticipated,
 * even though that field itself was never wired — this component is the
 * real replacement).
 */
import { useCallback, useEffect, useState } from 'react';
import { Calendar, Pause, Play, RotateCw } from 'lucide-react';
import { Button, StatusBadge } from '@/components/common/index.js';
import { scheduleApi } from '@/services/api/scheduleApi.js';
import { formatDateTime } from '@/utils/formatters.js';

const FREQUENCIES = [
  { value: 'HOURLY', label: 'Hourly' },
  { value: 'DAILY', label: 'Daily' },
  { value: 'WEEKLY', label: 'Weekly' },
  { value: 'MONTHLY', label: 'Monthly' },
  { value: 'CUSTOM_CRON', label: 'Custom Cron' },
];
const TIMEZONES = ['UTC', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Europe/London', 'Asia/Kolkata', 'Asia/Singapore'];
const STATUS_VARIANT = { ACTIVE: 'success', PAUSED: 'gray', RUNNING: 'info', FAILED: 'danger', COMPLETED: 'success', DISABLED: 'gray' };
const RUN_STATUS_VARIANT = { RUNNING: 'info', SUCCESS: 'success', WARNING: 'warning', FAILED: 'danger' };

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

export default function AnomalyScheduleCard({ jobId, jobName }) {
  const [schedule, setSchedule] = useState(undefined); // undefined = loading, null = none exists
  const [runs, setRuns] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ frequency: 'DAILY', start_date: todayISO(), start_time: '02:00', timezone: 'UTC', cron_expression: '' });
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(() => {
    scheduleApi.list({ anomaly_job_id: jobId, page_size: 1 })
      .then((r) => setSchedule(r.items[0] ?? null))
      .catch((e) => setError(apiErrorMessage(e, 'Could not load schedule.')));
  }, [jobId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (schedule) scheduleApi.getRuns(schedule.id, { page_size: 5 }).then((r) => setRuns(r.items)).catch(() => setRuns([]));
  }, [schedule]);

  const handleCreate = async () => {
    setSaving(true); setError('');
    try {
      const created = await scheduleApi.create({
        name: `${jobName} — Schedule`, pipeline_id: '', pipeline_name: '', anomaly_job_id: jobId,
        frequency: form.frequency, start_date: form.start_date, start_time: form.start_time,
        timezone: form.timezone, cron_expression: form.frequency === 'CUSTOM_CRON' ? form.cron_expression : null,
      });
      setSchedule(created);
      setShowForm(false);
    } catch (err) { setError(apiErrorMessage(err, 'Could not create schedule.')); }
    finally { setSaving(false); }
  };

  const handleUpdate = async () => {
    setSaving(true); setError('');
    try {
      const updated = await scheduleApi.update(schedule.id, {
        frequency: form.frequency, start_date: form.start_date, start_time: form.start_time,
        timezone: form.timezone, cron_expression: form.frequency === 'CUSTOM_CRON' ? form.cron_expression : null,
      });
      setSchedule(updated);
      setShowForm(false);
    } catch (err) { setError(apiErrorMessage(err, 'Could not update schedule.')); }
    finally { setSaving(false); }
  };

  const handleRunNow = async () => {
    setRunning(true); setError('');
    try {
      const updated = await scheduleApi.runNow(schedule.id);
      setSchedule(updated);
      scheduleApi.getRuns(schedule.id, { page_size: 5 }).then((r) => setRuns(r.items));
    } catch (err) { setError(apiErrorMessage(err, 'Run failed.')); }
    finally { setRunning(false); }
  };

  const togglePause = async () => {
    setError('');
    try {
      const updated = schedule.status === 'PAUSED' ? await scheduleApi.resume(schedule.id) : await scheduleApi.pause(schedule.id);
      setSchedule(updated);
    } catch (err) { setError(apiErrorMessage(err, 'Could not update schedule status.')); }
  };

  const openEditForm = () => {
    setForm({
      frequency: schedule.frequency, start_date: schedule.start_at.slice(0, 10),
      start_time: new Date(schedule.start_at).toISOString().slice(11, 16),
      timezone: schedule.timezone, cron_expression: schedule.cron_expression ?? '',
    });
    setShowForm(true);
  };

  if (schedule === undefined) return null;

  return (
    <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 'var(--radius-xl)', padding: 16, marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <div className="anm-setup-section-title" style={{ marginBottom: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Calendar size={14} /> Schedule
        </div>
        {schedule && !showForm && (
          <div style={{ display: 'flex', gap: 8 }}>
            <Button variant="ghost" size="sm" onClick={openEditForm}>Edit</Button>
            <Button variant="ghost" size="sm" icon={schedule.status === 'PAUSED' ? <Play size={12} /> : <Pause size={12} />} onClick={togglePause}>
              {schedule.status === 'PAUSED' ? 'Resume' : 'Pause'}
            </Button>
            <Button variant="secondary" size="sm" icon={<RotateCw size={12} />} loading={running} onClick={handleRunNow}>Run Now</Button>
          </div>
        )}
      </div>

      {error && <div style={{ fontSize: 12, color: 'var(--color-danger-dark)', marginBottom: 8 }}>{error}</div>}

      {!schedule && !showForm && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: 12.5, color: 'var(--text-muted)' }}>Not scheduled — this job only runs when triggered manually or from a pipeline.</span>
          <Button variant="secondary" size="sm" onClick={() => setShowForm(true)}>Create Schedule</Button>
        </div>
      )}

      {schedule && !showForm && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, fontSize: 12.5, flexWrap: 'wrap' }}>
            <StatusBadge variant={STATUS_VARIANT[schedule.status] ?? 'gray'} dot>{schedule.status}</StatusBadge>
            <span><strong>{schedule.frequency_display}</strong></span>
            <span style={{ color: 'var(--text-muted)' }}>Next run: {schedule.next_run_at ? formatDateTime(schedule.next_run_at) : '—'}</span>
            <span style={{ color: 'var(--text-muted)' }}>Last run: {schedule.last_run_at ? `${formatDateTime(schedule.last_run_at)} (${schedule.last_run_status})` : '—'}</span>
          </div>
          {runs.length > 0 && (
            <table className="data-table" style={{ width: '100%', marginTop: 10 }}>
              <thead><tr><th>Run</th><th>Status</th><th>Started</th><th>Anomalies</th><th>Message</th></tr></thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td>run_{r.id}</td>
                    <td><StatusBadge variant={RUN_STATUS_VARIANT[r.status] ?? 'gray'}>{r.status}</StatusBadge></td>
                    <td>{formatDateTime(r.started_at)}</td>
                    <td>{r.records_failed}</td>
                    <td style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.error_message || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      {showForm && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="anm-field-row">
            <div className="anm-field">
              <label className="anm-label">Frequency</label>
              <select className="anm-select anm-select-full" value={form.frequency} onChange={(e) => setForm((f) => ({ ...f, frequency: e.target.value }))}>
                {FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </select>
            </div>
            <div className="anm-field">
              <label className="anm-label">Timezone</label>
              <select className="anm-select anm-select-full" value={form.timezone} onChange={(e) => setForm((f) => ({ ...f, timezone: e.target.value }))}>
                {TIMEZONES.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
              </select>
            </div>
          </div>
          <div className="anm-field-row">
            <div className="anm-field">
              <label className="anm-label">Start Date</label>
              <input type="date" className="anm-input anm-input-full" value={form.start_date} onChange={(e) => setForm((f) => ({ ...f, start_date: e.target.value }))} />
            </div>
            <div className="anm-field">
              <label className="anm-label">Start Time</label>
              <input type="time" className="anm-input anm-input-full" value={form.start_time} onChange={(e) => setForm((f) => ({ ...f, start_time: e.target.value }))} />
            </div>
            {form.frequency === 'CUSTOM_CRON' && (
              <div className="anm-field">
                <label className="anm-label">Cron Expression</label>
                <input className="anm-input anm-input-full" value={form.cron_expression} onChange={(e) => setForm((f) => ({ ...f, cron_expression: e.target.value }))} placeholder="*/15 * * * *" />
              </div>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button variant="primary" size="sm" loading={saving} disabled={form.frequency === 'CUSTOM_CRON' && !form.cron_expression} onClick={schedule ? handleUpdate : handleCreate}>
              {schedule ? 'Save Schedule' : 'Create Schedule'}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setShowForm(false)}>Cancel</Button>
          </div>
        </div>
      )}
    </div>
  );
}
