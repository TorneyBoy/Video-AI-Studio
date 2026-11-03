// Network-free regression checks for the retry controls.  The browser UI is
// deliberately loaded with a tiny DOM shim so this test verifies the actual
// app.js retry path without adding a browser-test dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(id = '') {
    this.id = id;
    this.dataset = {};
    this.className = '';
    this.classList = {add() {}, remove() {}, toggle() {}};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.textContent = '';
    this.value = '';
    this.children = [];
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute() {}
  querySelector(selector) {
    if (selector === '[data-retry-job-id]') return this.children.find(item => item.dataset.retryJobId) || null;
    return null;
  }
  querySelectorAll() { return []; }
  scrollIntoView() {}
  set innerHTML(value) {
    this._innerHTML = value;
    const match = String(value).match(/data-retry-job-id="([^"]+)"/);
    this.children = match ? [new Element('retry')]: [];
    if (this.children[0]) {
      this.children[0].dataset.retryJobId = match[1];
      this.children[0].textContent = '重试此任务';
    }
  }
  get innerHTML() { return this._innerHTML || ''; }
}

const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
};
const calls = [];
let retryShouldFail = false;
let workspaceOffline = false;
let workspaceTemplates = [];
const response = (status, value) => ({
  status,
  ok: status >= 200 && status < 300,
  async text() { return value === null ? '' : JSON.stringify(value); },
});

const context = {
  console,
  setTimeout,
  clearTimeout,
  Intl,
  Date,
  URL,
  URLSearchParams,
  Math,
  JSON,
  Number,
  String,
  Object,
  Array,
  Set,
  FormData: class FormData {},
  localStorage: {
    values: new Map([['video-ai-studio.tenant-id', 'tenant-test']]),
    getItem(key) { return this.values.get(key) || null; },
    setItem(key, value) { this.values.set(key, String(value)); },
    removeItem(key) { this.values.delete(key); },
  },
  document: {
    hidden: false,
    querySelector(selector) {
      const id = selector.startsWith('#') ? selector.slice(1) : selector;
      return element(id);
    },
    querySelectorAll() { return []; },
    addEventListener() {},
  },
  window: {prompt() { return null;}},
  fetch: async (url, options = {}) => {
    const path = new URL(url, 'http://localhost').pathname + new URL(url, 'http://localhost').search;
    calls.push({path, method: String(options.method || 'GET').toUpperCase()});
    if (path === '/api/v1/session') return response(200, {auth_mode: 'development_header', tenant_id: null});
    if (path === '/api/v1/projects') return response(200, []);
    if (path === '/api/v1/jobs') return response(200, []);
    const task = {id:'job-1',project_id:'project-test',type:'render.draft',status:'failed',attempt_count:1,execution_generation:1,max_attempts:3,can_retry:true,can_cancel:false};
    if (path.startsWith('/api/v1/billing/overview')) return response(200,{scope:'project_lifetime',period_key:'2026-10',totals:[],tenant_policy:{currency:'USD'},project_policy:null});
    if (path.startsWith('/api/v1/billing/entries')) return response(200,{items:[],next_skip:0,has_more:false});
    if (path.startsWith('/api/v1/billing/jobs?')) return response(200,{items:[task],next_skip:1,has_more:false});
    if (path === '/api/v1/billing/jobs/job-1') return response(200,task);
    if (path === '/api/v1/billing/jobs/job-1/actions') return retryShouldFail ? response(503,{detail:{message:'暂时不可用'}}) : response(200,{found:true,current:{...task,status:'queued'}});
    if (path === '/api/v1/jobs/job-1') return response(200,{...task,status:'queued',payload:{}});
    if (path === '/api/v1/jobs/job-1/retry') {
      if (retryShouldFail) return response(503, {detail: {code: 'provider_unavailable', message: '暂时不可用'}});
      return response(200, {id: 'job-1', status: 'queued', retryable: true, attempt_count: 1, max_attempts: 3});
    }
    if (path.startsWith('/api/v1/assets?project_id=') || path.startsWith('/api/v1/templates?project_id=')) {
      if (workspaceOffline) throw new Error('network unavailable');
      return response(200, path.startsWith('/api/v1/templates?') ? workspaceTemplates : []);
    }
    if (path === '/api/v1/tenants') return response(201, {id: 'tenant-test'});
    return response(200, []);
  },
};
context.globalThis = context;
vm.createContext(context);
const featureScripts = JSON.parse(fs.readFileSync(require.resolve('../web/manifest.json'), 'utf8')).scripts;
const source = featureScripts.map(name => fs.readFileSync(require.resolve('../web/' + name), 'utf8')).join('\n') + '\n' + fs.readFileSync(require.resolve('../app.js'), 'utf8') + `
this.__retryUi = {
  retryJob, renderJobDetail, renderWorkspaceStatus, handleRenderAction, loadWorkspace, workspaceState, workspaceReady,
  setWorkspace(tenant, project, mark = true) {
    tenantId = tenant; selectedProjectId = project;
    if (mark) { workspaceState.tenantId = tenant; workspaceState.projectId = project; }
  },
};`;
vm.runInContext(source, context, {filename: 'app.js'});

(async () => {
  await context.__retryUi.workspaceReady;
  context.__retryUi.setWorkspace('tenant-test','project-test');
  const retried = await context.__retryUi.retryJob('job-1');
  assert.equal(retried.status, 'queued');
  assert.deepEqual(calls.filter(call => call.path.endsWith('/actions')), [{path: '/api/v1/billing/jobs/job-1/actions', method: 'POST'}]);

  retryShouldFail = true;
  const detailJob = {id: 'job-1', type: 'render.draft', status: 'failed', retryable: true, attempt_count: 1, max_attempts: 3, failure: {code: 'provider_unavailable', message: '暂时不可用', retryable: true}, steps: [], artifacts: []};
  context.__retryUi.renderJobDetail(detailJob);
  const button = element('jobDetail').querySelector('[data-retry-job-id]');
  assert.ok(button, 'failed job should expose a retry button');
  await button.listeners.click({currentTarget: button});
  assert.equal(button.disabled, false, 'retry control must re-enable after API failure');
  assert.equal(button.textContent, '重试此任务');

  const recovered = {...detailJob, status: 'succeeded', failure: null, failure_code: null,
    payload: {provider_failure: {code: 'old_failure', message: '旧尝试失败'},
      budget: {decision: 'amount_unknown', reconciliation_required: true, allowed: true,
        currency: 'CNY', reserved_micros: 500000}}};
  context.__retryUi.renderJobDetail(recovered);
  assert.ok(!element('jobDetail').innerHTML.includes('旧尝试失败'), 'recovered job must not show an old failure as current');
  assert.ok(!element('jobDetail').innerHTML.includes('old_failure'));
  assert.ok(element('jobDetail').innerHTML.includes('费用待对账 · 预占保留'));
  assert.ok(element('jobDetail').innerHTML.includes('CNY 0.50'));
  recovered.payload.budget.allowed = false;
  context.__retryUi.renderJobDetail(recovered);
  assert.ok(element('jobDetail').innerHTML.includes('费用待对账，暂缓交付'));
  assert.ok(!element('jobDetail').innerHTML.includes('已超预算'), 'unknown cost is not evidence of an exceeded budget');

  context.__retryUi.workspaceState.planJob = {status: 'succeeded', payload: {plan: {selected: [{asset_id: 'asset-1'}]}}};
  context.__retryUi.workspaceState.previewJob = detailJob;
  context.__retryUi.renderWorkspaceStatus(detailJob, 'draft');
  const preview = element('previewBtn');
  assert.equal(preview.disabled, false, 'retryable preview should be clickable');
  await context.__retryUi.handleRenderAction('draft');
  assert.equal(preview.disabled, false, 'preview retry control must re-enable after API failure');
  assert.equal(preview.textContent, '重试预览');

  // Refreshing an already hydrated workspace during a transient network
  // outage must retain the usable local view.  A project switch still clears
  // the marker, so the same fallback cannot leak across projects.
  context.__retryUi.setWorkspace('tenant-test', 'project-a');
  const state = context.__retryUi.workspaceState;
  const priorAssets = [{id: 'asset-a', kind: 'video', uri: 'https://cdn.test/a.mp4', metadata: {upload: {status: 'ready'}}}];
  const priorTemplates = [{id: 'template-a', name: 'A', version: '1', content: {slots: []}}];
  state.assets = priorAssets;
  state.templates = priorTemplates;
  state.planJob = {id: 'plan-a', type: 'auto_edit.plan', status: 'succeeded', payload: {plan: {selected: [{asset_id: 'asset-a'}]}}};
  state.previewJob = {id: 'render-a', type: 'render.draft', project_id: 'project-a', status: 'failed', retryable: true, attempt_count: 1, max_attempts: 3};
  state.exportJob = null;
  workspaceOffline = true;
  await context.__retryUi.loadWorkspace();
  assert.deepEqual(state.assets, priorAssets, 'transient asset refresh failure must keep current assets');
  assert.deepEqual(state.templates, priorTemplates, 'transient template refresh failure must keep current templates');
  assert.equal(state.planJob.id, 'plan-a', 'transient refresh failure must keep the current plan');
  assert.equal(state.previewJob.id, 'render-a', 'transient refresh failure must keep the current render status');
  context.__retryUi.setWorkspace('tenant-test', 'project-b', false);
  await context.__retryUi.loadWorkspace();
  assert.equal(state.assets.length, 0, 'a project switch must not reuse the previous assets');
  assert.equal(state.planJob, null, 'a project switch must not reuse the previous plan');
  assert.equal(state.previewJob, null, 'a project switch must not reuse the previous render');
  workspaceOffline = false;
  context.__retryUi.setWorkspace('tenant-test', 'project-c');
  workspaceTemplates = [{id: 'first', content: {slots: []}}, {id: 'chosen', content: {slots: []}}];
  state.selectedTemplateId = 'chosen';
  context.localStorage.setItem('video-ai-studio.workspace', JSON.stringify({templateId: 'first'}));
  await context.__retryUi.loadWorkspace();
  assert.equal(state.selectedTemplateId, 'chosen', 'refresh must preserve a valid current selection over an older saved choice');
  workspaceTemplates = [{id: 'first', content: {slots: []}}];
  await context.__retryUi.loadWorkspace();
  assert.equal(state.selectedTemplateId, 'first', 'removed templates must fall back to a currently available choice');
})().catch(error => { console.error(error); process.exitCode = 1; });
