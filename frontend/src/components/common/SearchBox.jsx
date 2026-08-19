import { Search } from 'lucide-react';

/** Reusable search input with debounce */
export function SearchBox({ value, onChange, placeholder = 'Search…', className = '' }) {
  return (
    <div className={`search-box ${className}`}>
      <Search size={14} className="search-box-icon" />
      <input
        type="search"
        className="search-box-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
    </div>
  );
}
