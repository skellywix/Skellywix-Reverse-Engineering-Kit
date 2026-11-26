import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { Theme } from '@astryxdesign/core/theme';
import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import './themes/skelly-graphite/skelly-graphite.css';
import './styles.css';
import App from './App';
import { skellyGraphiteTheme } from './themes/skelly-graphite/skellyGraphiteTheme';

const rootElement = document.getElementById('root');
if (rootElement === null) {
  throw new Error('Application root element is missing');
}

createRoot(rootElement).render(
  <StrictMode>
    <Theme theme={skellyGraphiteTheme} mode="dark">
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </Theme>
  </StrictMode>,
);
