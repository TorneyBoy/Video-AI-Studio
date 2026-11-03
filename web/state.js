// Shared classic-script scope; loaded in web/manifest.json order.
const API_BASE = '/api/v1';
const toast = document.querySelector('#toast');
let toastTimer;
function showToast(message){ if (!toast) return; toast.textContent = message; toast.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove('show'), 2600); }
const TENANT_KEY = 'video-ai-studio.tenant-id';
const PROJECT_KEY = 'video-ai-studio.project-id';
const WORKSPACE_OWNER_KEY = 'video-ai-studio.workspace-owner';
let tenantId = localStorage.getItem(TENANT_KEY);
let authMode = null;
let authPrincipalId = '';
let identityPromise = null;
let sessionRefreshPromise = null;
let selectedProjectId = localStorage.getItem(PROJECT_KEY);
let projects = [];
let loadedJobs = [];
let selectedJobId = null;
let jobsRefreshTimer;
let workspaceRenderTimer;
let jobsLoadPromise = null;
let jobsLoadKey = null;
let workspacePolls = new Map();
const ACTIVE_JOB_STATUSES = new Set(['queued', 'running', 'processing']);
const TERMINAL_JOB_STATUSES = new Set(['succeeded', 'completed', 'done', 'failed', 'cancelled']);
const WORKSPACE_KEY = 'video-ai-studio.workspace';
const workspaceState = {
  loadSequence: 0,
  openPlanJobId: null,
  assets: [],
  assetCache: {},
  assetQuery: '',
  assetTag: '',
  assetKind: '',
  templates: [],
  selectedAssetIds: new Set(),
  selectedTemplateId: null,
  // Keep the last successfully hydrated workspace identity.  A transient
  // refresh failure must not replace the current project's usable state with
  // an empty fallback, while a real project switch must never reuse it.
  tenantId: null,
  projectId: null,
  planJob: null,
  timelineId: null,
  previewJob: null,
  exportJob: null,
};
const BUILTIN_TEMPLATES = [
  {id: 'vertical-reel', name: '竖屏短视频', description: '适合社交媒体', art: 'reel', content: {schema_version: '1.0', id: 'vertical-reel', version: '1', fps: 30, duration: 15, slots: [{name: 'hero', type: 'video', required: true, constraints: {allowed_asset_types: ['video'], max_items: 1}}, {name: 'support', type: 'video', required: false, constraints: {allowed_asset_types: ['video'], max_items: 2}}], metadata: {slot_policies: {}}}},
  {id: 'brand-lookbook', name: '品牌图文', description: '适合产品发布', art: 'brand', content: {schema_version: '1.0', id: 'brand-lookbook', version: '1', fps: 30, duration: 12, slots: [{name: 'hero', type: 'video', required: true, constraints: {allowed_asset_types: ['video', 'image'], max_items: 1}}, {name: 'support', type: 'video', required: false, constraints: {allowed_asset_types: ['video', 'image'], max_items: 2}}], metadata: {slot_policies: {}}}},
  {id: 'podcast-cover', name: '播客封面', description: '适合音频内容', art: 'podcast', content: {schema_version: '1.0', id: 'podcast-cover', version: '1', fps: 30, duration: 15, slots: [{name: 'hero', type: 'video', required: true, constraints: {allowed_asset_types: ['video', 'image'], max_items: 1}}, {name: 'support', type: 'audio', required: false, constraints: {allowed_asset_types: ['audio'], max_items: 1}}], metadata: {slot_policies: {}}}},
];
