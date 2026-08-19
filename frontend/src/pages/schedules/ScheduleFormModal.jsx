import { useEffect, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Button, Modal } from '@/components/common/index.js';
import { pipelineApi } from '@/services/api/pipelineApi.js';
import { scheduleApi } from '@/services/api/scheduleApi.js';

const FREQUENCIES = [
  { value: 'ONCE', label: 'Once' },
  { value: 'HOURLY', label: 'Hourly' },
  { value: 'DAILY', label: 'Daily' },
  { value: 'WEEKLY', label: 'Weekly' },
  { value: 'MONTHLY', label: 'Monthly' },
  { value: 'CUSTOM_CRON', label: 'Custom Cron' },
];
const ENVIRONMENTS = ['PRODUCTION', 'STAGING', 'DEVELOPMENT', 'TEST'];
const TIMEZONES = [
  'UTC', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'America/Sao_Paulo',
  'Europe/London', 'Europe/Berlin', 'Asia/Kolkata', 'Asia/Dubai', 'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney',
];

const EMPTY_FORM = {
  name: '', pipeline_id: '', pipeline_name: '', environment: 'DEVELOPMENT', timezone: 'UTC',
  frequency: 'DAILY', start_date: '', start_time: '02:00', cron_expression: '',
  retry_count: 0, retry_interval_seconds: 60, timeout_seconds: 3600, concurrency: 1,
  notify_on_failure: true, notify_on_success: false, notification_emails: '',
};

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Converts a UTC ISO timestamp to {date, time} wall-clock strings in the
 * given IANA timezone — used to re-populate the form when editing a
 * schedule, since start_at is always stored/returned in UTC. */
function toLocalParts(iso, timeZone) {
  const d = new Date(iso);
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const parts = Object.fromEntries(fmt.formatToParts(d).map((p) => [p.type, p.value]));
  return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
}

/** ScheduleFormModal — create or edit. Reuses the existing pipeline catalogue
 * (pipelineApi.js) for the Pipeline picker — never invents a second pipeline
 * registry. */
export function ScheduleFormModal({ open, onClose, schedule, onSaved }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [pipelines, setPipelines] = useState([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => { pipelineApi.list().then(setPipelines).catch(() => {}); }, []);

  useEffect(() => {
    if (!open) return;
    if (schedule) {
      const { date, time } = toLocalParts(schedule.start_at, schedule.timezone);
      setForm({
        name: schedule.name, pipeline_id: schedule.pipeline_id, pipeline_name: schedule.pipeline_name,
        environment: schedule.environment, timezone: schedule.timezone, frequency: schedule.frequency,
        start_date: date, start_time: time,
        cron_expression: schedule.cron_expression ?? '',
        retry_count: schedule.retry_count, retry_interval_seconds: schedule.retry_interval_seconds,
        timeout_seconds: schedule.timeout_seconds, concurrency: schedule.concurrency,
        notify_on_failure: schedule.notify_on_failure, notify_on_success: schedule.notify_on_success,
        notification_emails: schedule.notification_emails ?? '',
      });
    } else {
      setForm({ ...EMPTY_FORM, start_date: todayISO() });
    }
    setError(null);
    setAdvancedOpen(false);
  }, [open, schedule]);

  const set = (key) => (e) => {
    const val = e?.target ? (e.target.type === 'checkbox' ? e.target.checked : e.target.value) : e;
    setForm((f) => ({ ...f, [key]: val }));
  };

  const setPipeline = (id) => {
    const p = pipelines.find((x) => x.id === id);
    setForm((f) => ({ ...f, pipeline_id: id, pipeline_name: p?.name ?? '' }));
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const body = {
        name: form.name, pipeline_id: form.pipeline_id, pipeline_name: form.pipeline_name,
        environment: form.environment, timezone: form.timezone, frequency: form.frequency,
        start_date: form.start_date, start_time: form.start_time,
        cron_expression: form.frequency === 'CUSTOM_CRON' ? form.cron_expression : null,
        retry_count: Number(form.retry_count), retry_interval_seconds: Number(form.retry_interval_seconds),
        timeout_seconds: Number(form.timeout_seconds), concurrency: Number(form.concurrency),
        notify_on_failure: form.notify_on_failure, notify_on_success: form.notify_on_success,
        notification_emails: form.notification_emails || null,
      };
      if (schedule) await scheduleApi.update(schedule.id, body);
      else await scheduleApi.create(body);
      onSaved();
      onClose();
    } catch (err) {
      setError(err.message || 'Failed to save schedule.');
    } finally {
      setSaving(false);
    }
  };

  const valid = form.name && form.pipeline_id && form.start_date && form.start_time
    && (form.frequency !== 'CUSTOM_CRON' || form.cron_expression);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={schedule ? `Edit ${schedule.name}` : 'Create Schedule'}
      size="lg"
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button><Button variant="primary" loading={saving} disabled={!valid} onClick={handleSave}>Save Schedule</Button></>}
    >
      {error && <div className="sched-form-error">{error}</div>}

      <div className="sched-field-grid">
        <div className="sched-field">
          <label className="sched-field-label">Schedule Name<span className="sched-required">*</span></label>
          <input className="sched-input" value={form.name} onChange={set('name')} placeholder="e.g. Daily Customer Validation" />
        </div>
        <div className="sched-field">
          <label className="sched-field-label">Pipeline<span className="sched-required">*</span></label>
          <select className="sched-input" value={form.pipeline_id} onChange={(e) => setPipeline(e.target.value)}>
            <option value="">Select a pipeline…</option>
            {pipelines.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>

        <div className="sched-field">
          <label className="sched-field-label">Environment</label>
          <select className="sched-input" value={form.environment} onChange={set('environment')}>
            {ENVIRONMENTS.map((e) => <option key={e} value={e}>{e.charAt(0) + e.slice(1).toLowerCase()}</option>)}
          </select>
        </div>
        <div className="sched-field">
          <label className="sched-field-label">Timezone</label>
          <select className="sched-input" value={form.timezone} onChange={set('timezone')}>
            {TIMEZONES.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
          </select>
        </div>

        <div className="sched-field">
          <label className="sched-field-label">Start Date<span className="sched-required">*</span></label>
          <input type="date" className="sched-input" value={form.start_date} onChange={set('start_date')} />
        </div>
        <div className="sched-field">
          <label className="sched-field-label">Start Time<span className="sched-required">*</span></label>
          <input type="time" className="sched-input" value={form.start_time} onChange={set('start_time')} />
        </div>

        <div className="sched-field">
          <label className="sched-field-label">Frequency</label>
          <select className="sched-input" value={form.frequency} onChange={set('frequency')}>
            {FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
        {form.frequency === 'CUSTOM_CRON' && (
          <div className="sched-field">
            <label className="sched-field-label">Cron Expression<span className="sched-required">*</span></label>
            <input className="sched-input" value={form.cron_expression} onChange={set('cron_expression')} placeholder="*/15 * * * *" />
          </div>
        )}
      </div>

      <div className="sched-form-divider" />

      <button type="button" className="sched-advanced-toggle" onClick={() => setAdvancedOpen((o) => !o)}>
        {advancedOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Advanced Options
      </button>

      {advancedOpen && (
        <div className="sched-field-grid" style={{ marginTop: 12 }}>
          <div className="sched-field">
            <label className="sched-field-label">Retry Count</label>
            <input type="number" min="0" className="sched-input" value={form.retry_count} onChange={set('retry_count')} />
          </div>
          <div className="sched-field">
            <label className="sched-field-label">Retry Interval (seconds)</label>
            <input type="number" min="0" className="sched-input" value={form.retry_interval_seconds} onChange={set('retry_interval_seconds')} />
          </div>
          <div className="sched-field">
            <label className="sched-field-label">Timeout (seconds)</label>
            <input type="number" min="1" className="sched-input" value={form.timeout_seconds} onChange={set('timeout_seconds')} />
          </div>
          <div className="sched-field">
            <label className="sched-field-label">Concurrency</label>
            <input type="number" min="1" className="sched-input" value={form.concurrency} onChange={set('concurrency')} />
          </div>
          <label className="sched-field sched-field-checkbox">
            <input type="checkbox" checked={form.notify_on_failure} onChange={set('notify_on_failure')} />
            <span>Failure Notification</span>
          </label>
          <label className="sched-field sched-field-checkbox">
            <input type="checkbox" checked={form.notify_on_success} onChange={set('notify_on_success')} />
            <span>Success Notification</span>
          </label>
          <div className="sched-field" style={{ gridColumn: '1 / -1' }}>
            <label className="sched-field-label">Notification Emails</label>
            <input className="sched-input" value={form.notification_emails} onChange={set('notification_emails')} placeholder="alerts@company.com, team@company.com" />
          </div>
        </div>
      )}
    </Modal>
  );
}
