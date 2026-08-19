import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { scheduleApi } from '@/services/api/scheduleApi.js';

const VIEWS = [{ key: 'day', label: 'Day' }, { key: 'week', label: 'Week' }, { key: 'month', label: 'Month' }];
const STATUS_DOT = { ACTIVE: 'sched-dot-active', RUNNING: 'sched-dot-running', FAILED: 'sched-dot-failed', PAUSED: 'sched-dot-paused', COMPLETED: 'sched-dot-completed', DISABLED: 'sched-dot-paused' };

function sameDay(a, b) { return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
function startOfWeek(d) { const r = new Date(d); r.setDate(r.getDate() - r.getDay()); r.setHours(0, 0, 0, 0); return r; }
function addDays(d, n) { const r = new Date(d); r.setDate(r.getDate() + n); return r; }

/**
 * ScheduleCalendar — shows each schedule's next scheduled run (and its last
 * run, for context) on the calendar. Only the single next occurrence the
 * backend computes is plotted — projecting further future occurrences would
 * mean re-deriving cron/frequency math on the frontend, duplicating what
 * schedule_service.compute_next_run() already owns.
 */
export function ScheduleCalendar({ onSelectSchedule }) {
  const [view, setView] = useState('month');
  const [anchor, setAnchor] = useState(new Date());
  const [schedules, setSchedules] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    scheduleApi.list({ page_size: 200 }).then((r) => setSchedules(r.items)).finally(() => setLoading(false));
  }, []);

  const events = useMemo(() => {
    const list = [];
    schedules.forEach((s) => {
      if (s.next_run_at) list.push({ id: `${s.id}-next`, schedule: s, date: new Date(s.next_run_at), kind: 'next' });
      if (s.last_run_at) list.push({ id: `${s.id}-last`, schedule: s, date: new Date(s.last_run_at), kind: 'last' });
    });
    return list;
  }, [schedules]);

  const eventsOn = (day) => events.filter((e) => sameDay(e.date, day));

  const navigate = (dir) => {
    setAnchor((cur) => {
      if (view === 'day') return addDays(cur, dir);
      if (view === 'week') return addDays(cur, dir * 7);
      const r = new Date(cur);
      r.setMonth(r.getMonth() + dir);
      return r;
    });
  };

  const title = view === 'day'
    ? anchor.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
    : view === 'week'
      ? `${startOfWeek(anchor).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} – ${addDays(startOfWeek(anchor), 6).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}`
      : anchor.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });

  return (
    <div className="sched-calendar">
      <div className="sched-calendar-toolbar">
        <div className="sched-pill-group">
          {VIEWS.map((v) => (
            <button key={v.key} className={`sched-pill ${view === v.key ? 'active' : ''}`} onClick={() => setView(v.key)}>{v.label}</button>
          ))}
        </div>
        <div className="sched-calendar-nav">
          <button className="sched-icon-btn" onClick={() => navigate(-1)}><ChevronLeft size={16} /></button>
          <span className="sched-calendar-title">{title}</span>
          <button className="sched-icon-btn" onClick={() => navigate(1)}><ChevronRight size={16} /></button>
          <button className="sched-icon-btn" title="Today" onClick={() => setAnchor(new Date())}>•</button>
        </div>
      </div>

      {loading ? (
        <div className="text-muted text-sm" style={{ padding: 20 }}>Loading calendar…</div>
      ) : view === 'month' ? (
        <MonthView anchor={anchor} eventsOn={eventsOn} onSelectSchedule={onSelectSchedule} />
      ) : view === 'week' ? (
        <WeekView anchor={anchor} eventsOn={eventsOn} onSelectSchedule={onSelectSchedule} />
      ) : (
        <DayView anchor={anchor} eventsOn={eventsOn} onSelectSchedule={onSelectSchedule} />
      )}
    </div>
  );
}

function EventPill({ event, onSelectSchedule }) {
  return (
    <button
      className={`sched-event-pill ${STATUS_DOT[event.schedule.status] ?? ''}`}
      title={`${event.schedule.name} — ${event.kind === 'next' ? 'Next Run' : 'Last Run'}`}
      onClick={(e) => { e.stopPropagation(); onSelectSchedule(event.schedule); }}
    >
      <span className="sched-event-dot" />
      {event.date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: event.schedule.timezone })} {event.schedule.name}
    </button>
  );
}

function MonthView({ anchor, eventsOn, onSelectSchedule }) {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const gridStart = startOfWeek(first);
  const days = Array.from({ length: 42 }, (_, i) => addDays(gridStart, i));
  const today = new Date();

  return (
    <div className="sched-month-grid">
      {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((d) => <div key={d} className="sched-month-dow">{d}</div>)}
      {days.map((day) => {
        const inMonth = day.getMonth() === anchor.getMonth();
        const dayEvents = eventsOn(day);
        return (
          <div key={day.toISOString()} className={`sched-month-cell ${inMonth ? '' : 'muted'} ${sameDay(day, today) ? 'today' : ''}`}>
            <span className="sched-month-daynum">{day.getDate()}</span>
            <div className="sched-month-events">
              {dayEvents.slice(0, 3).map((e) => <EventPill key={e.id} event={e} onSelectSchedule={onSelectSchedule} />)}
              {dayEvents.length > 3 && <span className="text-muted" style={{ fontSize: 10 }}>+{dayEvents.length - 3} more</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function WeekView({ anchor, eventsOn, onSelectSchedule }) {
  const start = startOfWeek(anchor);
  const days = Array.from({ length: 7 }, (_, i) => addDays(start, i));
  const today = new Date();

  return (
    <div className="sched-week-grid">
      {days.map((day) => (
        <div key={day.toISOString()} className={`sched-week-col ${sameDay(day, today) ? 'today' : ''}`}>
          <div className="sched-week-hdr">{day.toLocaleDateString('en-US', { weekday: 'short', day: 'numeric' })}</div>
          <div className="sched-month-events">
            {eventsOn(day).map((e) => <EventPill key={e.id} event={e} onSelectSchedule={onSelectSchedule} />)}
          </div>
        </div>
      ))}
    </div>
  );
}

function DayView({ anchor, eventsOn, onSelectSchedule }) {
  const dayEvents = eventsOn(anchor).sort((a, b) => a.date - b.date);
  return (
    <div className="sched-day-list">
      {dayEvents.length === 0 ? (
        <div className="text-muted text-sm" style={{ padding: 20 }}>No scheduled executions on this day.</div>
      ) : (
        dayEvents.map((e) => (
          <button key={e.id} className="sched-day-row" onClick={() => onSelectSchedule(e.schedule)}>
            <span className={`sched-event-dot ${STATUS_DOT[e.schedule.status] ?? ''}`} />
            <span className="sched-day-time">{e.date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: e.schedule.timezone })}</span>
            <span className="sched-day-name">{e.schedule.name}</span>
            <span className="text-muted text-sm">{e.kind === 'next' ? 'Next Run' : 'Last Run'} · {e.schedule.pipeline_name}</span>
          </button>
        ))
      )}
    </div>
  );
}
