import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import { Sidebar } from '@/components/navigation/Sidebar.jsx';
import { Header }  from '@/components/navigation/Header.jsx';
import { ImpersonationBanner } from '@/components/seller/ImpersonationBanner.jsx';
import './MainLayout.css';

export function MainLayout() {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="app-shell">
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <div className="app-main">
        <ImpersonationBanner />
        <Header collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
        <main className="app-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
