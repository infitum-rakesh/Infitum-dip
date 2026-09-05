import { useEffect, useRef, useState } from 'react';
import { Bell, CheckCheck, Loader2 } from 'lucide-react';
import { notificationApi } from '@/services/api/notificationApi.js';
import './NotificationDropdown.css';

const POLL_INTERVAL_MS = 60_000;

function relativeTime(iso) {
  const diffMs = Date.now() - new Date(iso).getTime();
  const diffMin = Math.round(diffMs / 60_000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  return new Date(iso).toLocaleDateString();
}

export function NotificationDropdown() {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const rootRef = useRef(null);

  async function refresh() {
    try {
      const data = await notificationApi.list({ page_size: 20 });
      setItems(data.items);
      setUnreadCount(data.unread_count);
      setError(null);
    } catch {
      setError('Could not load notifications.');
    }
  }

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    function onClickOutside(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, []);

  async function toggleOpen() {
    const next = !open;
    setOpen(next);
    if (next) {
      setLoading(true);
      await refresh();
      setLoading(false);
    }
  }

  async function handleItemClick(item) {
    if (item.read_at) return;
    setItems((prev) => prev.map((n) => (n.id === item.id ? { ...n, read_at: new Date().toISOString() } : n)));
    setUnreadCount((c) => Math.max(0, c - 1));
    try {
      await notificationApi.markRead(item.id);
    } catch {
      refresh(); // reconcile with the server if the optimistic update didn't actually land
    }
  }

  async function handleMarkAllRead() {
    if (unreadCount === 0) return;
    const now = new Date().toISOString();
    setItems((prev) => prev.map((n) => (n.read_at ? n : { ...n, read_at: now })));
    setUnreadCount(0);
    try {
      await notificationApi.markAllRead();
    } catch {
      refresh();
    }
  }

  return (
    <div className="notif-dropdown-root" ref={rootRef}>
      <button className="header-icon-btn" aria-label="Notifications" onClick={toggleOpen}>
        <Bell size={16} />
        {unreadCount > 0 && <span className="header-notif-dot" />}
      </button>

      {open && (
        <div className="notif-panel">
          <div className="notif-panel-header">
            <span>Notifications</span>
            {unreadCount > 0 && (
              <button className="notif-mark-all-btn" onClick={handleMarkAllRead}>
                <CheckCheck size={13} /> Mark all read
              </button>
            )}
          </div>

          <div className="notif-panel-body">
            {loading && (
              <div className="notif-panel-loading">
                <Loader2 size={16} className="notif-spin" /> Loading…
              </div>
            )}
            {!loading && error && <div className="notif-panel-empty">{error}</div>}
            {!loading && !error && items.length === 0 && (
              <div className="notif-panel-empty">No notifications yet.</div>
            )}
            {!loading &&
              !error &&
              items.map((item) => (
                <button
                  key={item.id}
                  className={`notif-item${item.read_at ? '' : ' notif-item-unread'}`}
                  onClick={() => handleItemClick(item)}
                >
                  <span className="notif-item-dot" />
                  <div className="notif-item-content">
                    <span className="notif-item-title">{item.title}</span>
                    <span className="notif-item-body">{item.body}</span>
                    <span className="notif-item-time">{relativeTime(item.created_at)}</span>
                  </div>
                </button>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
