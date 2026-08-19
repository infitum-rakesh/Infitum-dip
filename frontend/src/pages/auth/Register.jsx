import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Eye, EyeOff, AlertCircle, CheckCircle2 } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth.js';
import { Button } from '@/components/common/Button.jsx';
import { validate, email as validateEmail, required, minLength, passwordsMatch } from '@/utils/validators.js';
import './auth.css';

export default function Register() {
  const { register, isLoading } = useAuth();
  const navigate = useNavigate();

  const [form, setForm]       = useState({ full_name: '', email: '', password: '', confirm_password: '', company_name: '', country: '' });
  const [errors, setErrors]   = useState({});
  const [apiError, setApiErr] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [agreed, setAgreed]   = useState(false);

  const set = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    const errs = validate(form, {
      full_name:    (v) => required(v, 'Full name'),
      email:        (v) => validateEmail(v),
      password:     (v) => minLength(v, 8, 'Password'),
      confirm_password: (v) => passwordsMatch(form.password, v),
      company_name: (v) => required(v, 'Company name'),
    });
    setErrors(errs);
    if (Object.keys(errs).length) return;
    if (!agreed) { setApiErr('Please accept the Terms of Service.'); return; }

    setApiErr('');
    const result = await register({
      full_name:    form.full_name,
      email:        form.email,
      password:     form.password,
      company_name: form.company_name,
      country:      form.country || undefined,
    });
    if (result.ok) navigate('/app/dashboard', { replace: true });
    else setApiErr(result.error);
  };

  return (
    <div className="auth-form-wrap animate-fade-in-up">
      <a href="/" className="auth-logo-row auth-logo-link" aria-label="Back to home">
        <div className="auth-logo-mark">DI</div>
        <span className="auth-logo-name">DataIntelligence</span>
      </a>

      <h1 className="auth-heading">Start free trial</h1>
      <p className="auth-sub-heading">14 days free — no credit card required</p>

      {apiError && (
        <div className="auth-alert auth-alert-error" role="alert">
          <AlertCircle size={14} /> {apiError}
        </div>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <div className="form-row-2">
          <div className="form-field">
            <label className="form-label" htmlFor="full_name">Full name <span className="req">*</span></label>
            <input id="full_name" type="text" className={`form-input ${errors.full_name ? 'input-error' : ''}`} value={form.full_name} onChange={set('full_name')} placeholder="Jane Smith" autoComplete="name" autoFocus />
            {errors.full_name && <p className="field-error"><AlertCircle size={11}/>{errors.full_name}</p>}
          </div>

          <div className="form-field">
            <label className="form-label" htmlFor="company">Company <span className="req">*</span></label>
            <input id="company" type="text" className={`form-input ${errors.company_name ? 'input-error' : ''}`} value={form.company_name} onChange={set('company_name')} placeholder="Acme Corp" autoComplete="organization" />
            {errors.company_name && <p className="field-error"><AlertCircle size={11}/>{errors.company_name}</p>}
          </div>
        </div>

        <div className="form-field">
          <label className="form-label" htmlFor="email">Work email <span className="req">*</span></label>
          <input id="email" type="email" className={`form-input ${errors.email ? 'input-error' : ''}`} value={form.email} onChange={set('email')} placeholder="you@company.com" autoComplete="email" />
          {errors.email && <p className="field-error"><AlertCircle size={11}/>{errors.email}</p>}
        </div>

        <div className="form-field">
          <label className="form-label" htmlFor="country">Country</label>
          <input id="country" type="text" className="form-input" value={form.country} onChange={set('country')} placeholder="United States" />
        </div>

        <div className="form-field">
          <label className="form-label" htmlFor="password">Password <span className="req">*</span></label>
          <div className="form-input-wrap">
            <input id="password" type={showPwd ? 'text' : 'password'} className={`form-input ${errors.password ? 'input-error' : ''}`} value={form.password} onChange={set('password')} placeholder="Min. 8 characters" autoComplete="new-password" />
            <button type="button" className="pwd-toggle" onClick={() => setShowPwd((s) => !s)} tabIndex={-1}>
              {showPwd ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </div>
          {errors.password && <p className="field-error"><AlertCircle size={11}/>{errors.password}</p>}
        </div>

        <div className="form-field">
          <label className="form-label" htmlFor="confirm_password">Confirm password <span className="req">*</span></label>
          <input id="confirm_password" type={showPwd ? 'text' : 'password'} className={`form-input ${errors.confirm_password ? 'input-error' : ''}`} value={form.confirm_password} onChange={set('confirm_password')} placeholder="Repeat password" autoComplete="new-password" />
          {errors.confirm_password && <p className="field-error"><AlertCircle size={11}/>{errors.confirm_password}</p>}
        </div>

        <div className="terms-row">
          <input type="checkbox" id="terms" className="terms-check" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />
          <label htmlFor="terms">
            I agree to the <a href="#" onClick={(e) => e.preventDefault()}>Terms of Service</a> and{' '}
            <a href="#" onClick={(e) => e.preventDefault()}>Privacy Policy</a>
          </label>
        </div>

        <Button type="submit" variant="primary" size="lg" full loading={isLoading}>
          Create account &amp; start trial
        </Button>
      </form>

      <p className="auth-footer-link">
        Already have an account? <Link to="/login">Sign in</Link>
      </p>
    </div>
  );
}
