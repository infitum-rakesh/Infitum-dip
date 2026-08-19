/** Field-level validators — return error string or null */

export function required(value, label = 'This field') {
  return value?.toString().trim() ? null : `${label} is required.`;
}

export function minLength(value, min, label = 'Field') {
  return value?.length >= min ? null : `${label} must be at least ${min} characters.`;
}

export function maxLength(value, max, label = 'Field') {
  return value?.length <= max ? null : `${label} must be at most ${max} characters.`;
}

export function email(value) {
  const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  return re.test(value ?? '') ? null : 'Enter a valid email address.';
}

export function password(value) {
  if (!value || value.length < 8) return 'Password must be at least 8 characters.';
  return null;
}

export function passwordsMatch(p1, p2) {
  return p1 === p2 ? null : 'Passwords do not match.';
}

/** Run a map of validators: { fieldName: validatorFn } → error map */
export function validate(fields, rules) {
  const errors = {};
  for (const [field, validatorFn] of Object.entries(rules)) {
    const err = validatorFn(fields[field]);
    if (err) errors[field] = err;
  }
  return errors; // empty = valid
}
