import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './app/App.jsx';

// Global styles — import order matters
import './styles/variables.css';
import './styles/global.css';
import './styles/responsive.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
