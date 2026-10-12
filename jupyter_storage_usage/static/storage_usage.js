/* jupyter-storage-usage frontend
 *
 * Polls /api/storage/v1/usage every 300s. The server runs du every 600s
 * and caches the result; this widget only reads the cache (instant).
 * On first load (cache empty), shows "💾 loading…" until the first du completes.
 * Shows a status bar item with K8s-style units: "💾 15Gi / 20Gi"
 */

const STORAGE_POLL_MS = 300000;  // 5 min — matches server cache freshness
const FETCH_TIMEOUT_MS = 5000;   // 5s — cache read should be instant

function getBaseUrl() {
  // Use JupyterLab's PageConfig API to get the correct base URL.
  // This is the official way to discover the server's base path and
  // works regardless of which JupyterLab route the user is on.
  // Falls back to deriving from window.location for non-JupyterLab contexts.
  if (typeof PageConfig !== 'undefined' && PageConfig.getBaseUrl) {
    return PageConfig.getBaseUrl();
  }
  // Fallback: derive from the body data attribute that Jupyter Server sets
  var bodyUrl = document.body && document.body.getAttribute('data-base-url');
  if (bodyUrl) {
    return bodyUrl;
  }
  // Last resort: use pathname without known suffixes
  var path = window.location.pathname;
  path = path.replace(/\/lab(\/.*)?$/, '/');
  path = path.replace(/\/notebooks\/.*$/, '/');
  path = path.replace(/\/tree\/.*$/, '/');
  path = path.replace(/\/edit\/.*$/, '/');
  if (!path.endsWith('/')) { path = path + '/'; }
  return path;
}

function statusColor(status) {
  if (status === 'over_limit') return 'var(--jp-error-color1, #e84855)';
  if (status === 'warning') return 'var(--jp-warn-color1, #f0a000)';
  return 'var(--jp-content-font-color2, #888)';
}

function statusIcon(status) {
  if (status === 'over_limit') return '🚫';
  if (status === 'warning') return '⚠️';
  return '💾';
}

async function fetchStorageUsage() {
  try {
    const url = getBaseUrl() + 'api/storage/v1/usage';
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    const resp = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    if (!resp.ok) return null;
    const data = await resp.json();
    // Empty response {} means cache not ready yet
    if (!data || data.used_bytes === undefined) return null;
    return data;
  } catch (e) {
    return null;
  }
}

function createStatusBarItem() {
  const el = document.createElement('span');
  el.className = 'jupyter-storage-usage-item';
  el.style.cssText = `
    display: inline-flex; align-items: center; gap: 4px;
    padding: 0 8px; cursor: default;
    font-size: var(--jp-ui-font-size1, 13px);
  `;
  el.textContent = '💾 loading…';
  el.title = 'Storage usage (waiting for first scan...)';
  el.style.color = 'var(--jp-content-font-color3, #ccc)';
  return el;
}

function updateStatusBarItem(el, data) {
  if (!data) {
    // Cache not populated yet — du still running or not yet started
    el.textContent = '💾 loading…';
    el.title = 'Storage usage (waiting for first scan...)';
    el.style.color = 'var(--jp-content-font-color3, #ccc)';
    return;
  }

  // Use server-provided display strings (K8s-style units: 15Gi, 512Mi, etc.)
  const used = data.used_display || (data.used_bytes / 1073741824).toFixed(1) + 'Gi';
  const hard = data.hard_limit_display || (data.hard_limit_bytes / 1073741824).toFixed(1) + 'Gi';
  const warn = data.warn_limit_display || (data.warn_limit_bytes / 1073741824).toFixed(1) + 'Gi';

  el.textContent = `${statusIcon(data.status)} ${used} / ${hard}`;
  el.style.color = statusColor(data.status);

  const ageSec = Math.round((Date.now() / 1000) - data.timestamp);
  const ageStr = ageSec < 60 ? `${ageSec}s ago` :
                 ageSec < 3600 ? `${Math.round(ageSec/60)}m ago` :
                 `${Math.round(ageSec/3600)}h ago`;
  const ts = new Date(data.timestamp * 1000).toLocaleTimeString();
  el.title = `Storage: ${used} used of ${hard} limit\n` +
    `Warn at ${warn}\n` +
    `Last checked: ${ts} (${ageStr})\n` +
    `Status: ${data.status}`;
}

function init() {
  // Wait for JupyterLab status bar to be available
  const waitForStatusBar = setInterval(() => {
  const statusbar = document.querySelector('#jp-main-statusbar') || document.querySelector('.jp-StatusBar-Widget');
  if (!statusbar) return;

    clearInterval(waitForStatusBar);
    const item = createStatusBarItem();
    // Insert at the very beginning so it's always visible
    const leftGroup = statusbar.querySelector('.jp-StatusBar-Left') || statusbar;
    if (leftGroup === statusbar) {
      statusbar.insertBefore(item, statusbar.firstChild);
    } else {
      leftGroup.insertBefore(item, leftGroup.firstChild);
    }

    async function poll() {
      const data = await fetchStorageUsage();
      updateStatusBarItem(item, data);
    }

    poll();
    setInterval(poll, STORAGE_POLL_MS);
  }, 1000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
