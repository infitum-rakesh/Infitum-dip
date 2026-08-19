import { useContext } from 'react';
import { AuthContext } from '@/context/AuthContext.jsx';

/** Convenience hook — access auth state and actions anywhere */
export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
