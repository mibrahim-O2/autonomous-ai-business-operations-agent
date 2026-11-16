// Static default: the read-only demonstration used on GitHub Pages.
// When the Python backend serves the dashboard it answers /config.js itself
// with {"mode": "live"}, so this file is only reached on a static host.
window.BIZOPS_CONFIG = { mode: "demo", snapshot: "demo/snapshot.json" };
