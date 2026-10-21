// Public FastAPI endpoint for the GitHub Pages deployment. Local development
// continues to use the API on port 8000.
window.FOODSOURCE_API = window.FOODSOURCE_API || (
  location.hostname.endsWith('.github.io')
    ? 'https://foodsource-api.onrender.com'
    : 'http://127.0.0.1:8000'
);
