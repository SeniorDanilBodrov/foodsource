// Public FastAPI endpoint for the GitHub Pages deployment. Local development
// uses port 8001 while an older local API process still occupies port 8000.
window.FOODSOURCE_API = window.FOODSOURCE_API || (
  location.hostname.endsWith('.github.io')
    ? 'https://foodsource-api.onrender.com'
    : 'http://127.0.0.1:8001'
);
