// Shared classic-script scope; loaded in web/manifest.json order.
function parseJsonText(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch (_) { return null; }
}

function apiErrorMessage(payload, fallback = '请求失败') {
  const detail = payload && typeof payload === 'object' && Object.prototype.hasOwnProperty.call(payload, 'detail') ? payload.detail : payload;
  if (Array.isArray(detail)) {
    const messages = detail.map(item => {
      if (typeof item === 'string') return item;
      if (!item || typeof item !== 'object') return '';
      const location = Array.isArray(item.loc) ? item.loc.filter(Boolean).join('.') : '';
      const message = item.msg || item.message || item.code || '';
      return location && message ? `${location}: ${message}` : message;
    }).filter(Boolean);
    if (messages.length) return messages.join('；');
  }
  if (detail && typeof detail === 'object') {
    const message = detail.message || detail.error || detail.msg || detail.code;
    if (message) return String(message);
  }
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (payload && typeof payload === 'object') {
    const message = payload.message || payload.error || payload.msg || payload.code;
    if (message) return String(message);
  }
  return String(payload || fallback).trim() || fallback;
}

async function responseError(response) {
  const text = await response.text().catch(() => '');
  const payload = parseJsonText(text);
  const error = new Error(apiErrorMessage(payload || text, `HTTP ${response.status}`));
  error.status = response.status;
  error.code = payload?.detail?.code || payload?.code || (typeof payload?.detail === 'string' ? payload.detail : undefined);
  error.details = payload?.detail || payload;
  error.userMessage = apiErrorMessage(payload || text, `HTTP ${response.status}`);
  return error;
}

async function api(path, options = {}) {
  const {omitTenantIdentity, sessionRetried, ...requestOptions} = options;
  const headers = {...(options.body instanceof FormData ? {} : {'Content-Type': 'application/json'}), ...(options.headers || {})};
  if (!omitTenantIdentity && authMode === 'development_header' && tenantId) headers['X-Tenant-ID'] = tenantId;
  const method = String(options.method || 'GET').toUpperCase();
  const response = await fetch(`${API_BASE}${path}`, {...requestOptions, cache: method === 'GET' ? 'no-store' : options.cache, headers});
  if (response.status === 401 && authMode === 'session' && !sessionRetried && !path.startsWith('/auth/')) {
    await refreshBrowserSession();
    // A 401 was rejected before business execution. Keep the same body and
    // idempotency key; never retry network errors/unknown POST outcomes.
    return api(path, {...options, sessionRetried: true});
  }
  if (!response.ok) throw await responseError(response);
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  return parseJsonText(text) ?? text;
}

async function refreshBrowserSession() {
  const refresh = async () => {
    const current = await fetch(`${API_BASE}/session`, {credentials: 'same-origin', cache: 'no-store'});
    if (current.ok) return; // Another tab may already have refreshed the cookie.
    const response = await fetch(`${API_BASE}/auth/refresh`, {method: 'POST',
      credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({client: 'web'})});
    if (!response.ok) {
      if (window.location) window.location.assign('/login');
      throw new Error('登录已失效，请重新登录');
    }
  };
  sessionRefreshPromise ||= (typeof navigator !== 'undefined' && navigator.locks
    ? navigator.locks.request('video-ai-session-refresh', refresh) : refresh())
    .finally(() => { sessionRefreshPromise = null; });
  return sessionRefreshPromise;
}

window.addEventListener?.('storage', event => {
  if (event.key === WORKSPACE_OWNER_KEY && authMode === 'session') window.location?.reload();
});

async function identityContext() {
  try { return await api('/session', {omitTenantIdentity: true}); }
  catch (error) {
    if (error.status !== 401) throw error;
    const capabilities = await api('/auth/capabilities', {omitTenantIdentity: true});
    if (!capabilities.account_login) throw error;
    await refreshBrowserSession();
    return api('/session', {omitTenantIdentity: true});
  }
}

async function ensureTenant() {
  if (!authMode) {
    identityPromise ||= identityContext().then(context => {
      if (!['development_header', 'trusted_proxy', 'session'].includes(context?.auth_mode)) throw new Error('身份服务返回无效');
      authMode = context.auth_mode;
      authPrincipalId = context.principal_id || '';
      if (authMode !== 'development_header') {
        if (!context.tenant_id) throw new Error('请先登录工作台');
        if (localStorage.getItem(WORKSPACE_OWNER_KEY) !== context.tenant_id) {
          selectedProjectId = null;
          localStorage.removeItem(PROJECT_KEY);
          localStorage.removeItem(WORKSPACE_KEY);
        }
        tenantId = context.tenant_id;
        localStorage.removeItem(TENANT_KEY);
        localStorage.setItem(WORKSPACE_OWNER_KEY, tenantId);
      }
    }).finally(() => { identityPromise = null; });
    await identityPromise;
  }
  if (tenantId) return tenantId;
  if (authMode !== 'development_header') throw new Error('请先登录工作台');
  try {
    const created = await api('/tenants', {method: 'POST', body: JSON.stringify({slug: `workspace-${Math.random().toString(36).slice(2, 9)}`, name: '内容工作区'})});
    tenantId = created.id;
    localStorage.setItem(TENANT_KEY, tenantId);
  } catch (_) { /* API offline; keep preview usable */ }
  return tenantId;
}
