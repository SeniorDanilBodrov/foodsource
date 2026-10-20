// Deployment setting. Render uses this service name by default; replace the
// URL after creating the backend if the generated hostname differs.
// GitHub Pages is intentionally usable without a backend: it loads the
// verified seed catalog locally in the browser. Once the FastAPI service is
// deployed, replace the empty string with its public URL (for example the
// Render hostname) and commit the file again.
window.FOODSOURCE_API = window.FOODSOURCE_API || (
  location.hostname.endsWith('.github.io')
    ? ''
    : 'http://127.0.0.1:8000'
);
