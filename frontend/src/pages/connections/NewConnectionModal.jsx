import {
  Database, AppWindow, HardDrive, Plug, Cloud, Sparkles, ScanText,
} from 'lucide-react';
import { Modal } from '@/components/common/index.js';

const CATEGORY_ICON = {
  DATABASE: Database, APPLICATION: AppWindow, STORAGE: HardDrive, API: Plug, CLOUD: Cloud,
  AI_PROVIDER: Sparkles, OCR_PROVIDER: ScanText,
};

/**
 * NewConnectionModal — step 1 of creating a connection: pick a category
 * then a type. Reads categories/schemas from the backend's type catalogue
 * (GET /connections/types) — never hardcodes the type list.
 */
export function NewConnectionModal({ open, onClose, catalogue, onSelect }) {
  if (!catalogue) return null;

  return (
    <Modal open={open} onClose={onClose} title="New Connection" size="lg">
      <p className="text-muted text-sm" style={{ marginBottom: 16 }}>Choose the type of connection you want to configure.</p>
      <div className="conn-type-groups">
        {catalogue.categories.map((cat) => {
          const Icon = CATEGORY_ICON[cat.key] ?? Database;
          return (
            <div key={cat.key} className="conn-type-group">
              <div className="conn-type-group-hdr"><Icon size={14} /> {cat.label}</div>
              <div className="conn-type-grid">
                {cat.types.map((typeKey) => {
                  const schema = catalogue.schemas[typeKey];
                  if (!schema) return null;
                  return (
                    <button key={typeKey} className="conn-type-card" onClick={() => onSelect(cat.key, typeKey)}>
                      {schema.label}
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </Modal>
  );
}
