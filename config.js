// Deployment setting. Render uses this service name by default; replace the
// URL after creating the backend if the generated hostname differs.
window.FOODSOURCE_API = window.FOODSOURCE_API || (
  location.hostname.endsWith('.github.io')
    ? 'https://foodsource-api.onrender.com'
    : 'http://127.0.0.1:8000'
);
