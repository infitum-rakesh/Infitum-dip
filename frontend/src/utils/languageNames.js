/**
 * Multilingual Document Processing — ISO 639-1 code -> display name, for
 * badges/labels only. NOT the source of truth (that's the backend's
 * ocr/language_config.py LANGUAGE_CONFIG) — this is a small, intentionally
 * duplicated display-only lookup so the frontend doesn't need a round trip
 * just to render "Kannada" instead of "kn". Keep this list in sync with
 * LANGUAGE_CONFIG's keys if a new language is ever added there.
 */
export const LANGUAGE_NAMES = {
  en: 'English',
  hi: 'Hindi',
  kn: 'Kannada',
  ta: 'Tamil',
  te: 'Telugu',
  ml: 'Malayalam',
  mr: 'Marathi',
  bn: 'Bengali',
  gu: 'Gujarati',
  pa: 'Punjabi',
  or: 'Odia',
  as: 'Assamese',
  ur: 'Urdu',
};

export function languageName(code) {
  if (!code) return 'Unknown';
  return LANGUAGE_NAMES[code] ?? code.toUpperCase();
}
