import { Menu, PanelLeftClose, PanelLeftOpen, Search } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth.js';
import { Breadcrumb } from './Breadcrumb.jsx';
import { NotificationDropdown } from './NotificationDropdown.jsx';
import './Header.css';

export function Header({ collapsed, onToggle, onMobileNavToggle }) {
  const { user } = useAuth();

  return (
    <header className="app-header">
      {/* Mobile/tablet only (CSS-driven, ≤1024px) — opens the Sidebar as
          an off-canvas drawer. The desktop collapse toggle below is the
          inverse-hidden sibling at that same breakpoint. */}
      <button className="header-hamburger-btn" onClick={onMobileNavToggle} aria-label="Open navigation menu">
        <Menu size={20} />
      </button>

      <button className="header-toggle-btn" onClick={onToggle} aria-label="Toggle sidebar">
        {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
      </button>

      <Breadcrumb />

      <div className="header-search">
        <Search size={13} className="header-search-icon" />
        <input type="search" className="header-search-input" placeholder="Search datasets, pipelines, documents…" />
        <kbd className="header-search-kbd">⌘K</kbd>
      </div>

      <div className="header-right">
        <NotificationDropdown />

        <div className="header-user">
          <div className="header-avatar">
            {user?.full_name?.charAt(0)?.toUpperCase() ?? 'U'}
          </div>
          <div className="header-user-info">
            <span className="header-user-name">{user?.full_name}</span>
            <span className="header-user-role">{user?.role?.replace(/_/g, ' ')}</span>
          </div>
        </div>
      </div>
    </header>
  );
}
