import { Link } from 'react-router-dom';
import { Home } from 'lucide-react';

export default function NotFound() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: '100vh', gap: 16, padding: 24, textAlign: 'center' }}>
      <div style={{ fontSize: 72, fontWeight: 800, color: 'var(--color-primary)', lineHeight: 1 }}>404</div>
      <h1 style={{ fontSize: 24, fontWeight: 700, color: 'var(--text-primary)' }}>Page not found</h1>
      <p style={{ color: 'var(--text-muted)', maxWidth: 360 }}>The page you're looking for doesn't exist or you don't have permission to view it.</p>
      <Link to="/app/dashboard" style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '9px 20px', background: 'var(--color-primary)', color: '#fff', borderRadius: 8, fontSize: 14, fontWeight: 500 }}>
        <Home size={15} /> Back to Dashboard
      </Link>
    </div>
  );
}
