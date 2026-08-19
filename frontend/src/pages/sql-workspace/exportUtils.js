/**
 * exportUtils — real client-side export of a SQL Workspace result grid.
 * Operates on whatever rows are ALREADY in the browser (bounded by the
 * query's row limit, never more than 1000 — see ROW_LIMITS in
 * SqlWorkspace.jsx) — genuine CSV/JSON/XLSX generation, not a stub.
 * Streaming/background export for result sets larger than the browser-
 * safe bound is a separate, not-yet-built increment (see task tracker) —
 * deliberately not faked here with a UI that doesn't actually stream.
 */
import * as XLSX from 'xlsx';

function csvCell(value) {
  if (value === null || value === undefined) return '';
  const s = String(value);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(columns, rows) {
  const lines = [columns.map(csvCell).join(','), ...rows.map((r) => r.map(csvCell).join(','))];
  return lines.join('\r\n');
}

export function toJson(columns, rows) {
  return JSON.stringify(rows.map((r) => Object.fromEntries(columns.map((c, i) => [c, r[i]]))), null, 2);
}

function txtCell(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[\t\r\n]/g, ' ');
}

// Tab-delimited plain text — the same "Results to Text" convention most
// SQL clients (SSMS, DBeaver, ...) use: one header row, columns padded
// with a single tab rather than a fixed-width column layout.
export function toTxt(columns, rows) {
  const lines = [columns.map(txtCell).join('\t'), ...rows.map((r) => r.map(txtCell).join('\t'))];
  return lines.join('\r\n');
}

export function downloadTextFile(filename, content, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function downloadXlsx(filename, columns, rows) {
  const ws = XLSX.utils.aoa_to_sheet([columns, ...rows.map((r) => r.map((v) => (v === null || v === undefined ? '' : v)))]);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Result');
  XLSX.writeFile(wb, filename); // SheetJS's own real browser download — no separate Blob wiring needed
}

export function exportResult(format, baseName, columns, rows) {
  const stamp = baseName.replace(/[^a-z0-9_-]+/gi, '_');
  if (format === 'csv') downloadTextFile(`${stamp}.csv`, toCsv(columns, rows), 'text/csv;charset=utf-8');
  else if (format === 'json') downloadTextFile(`${stamp}.json`, toJson(columns, rows), 'application/json;charset=utf-8');
  else if (format === 'xlsx') downloadXlsx(`${stamp}.xlsx`, columns, rows);
  else if (format === 'txt') downloadTextFile(`${stamp}.txt`, toTxt(columns, rows), 'text/plain;charset=utf-8');
}
