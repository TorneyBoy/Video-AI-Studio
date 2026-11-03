// Offline transport-failure and concurrent-editor contracts for the actual UI.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {createHash} = require('node:crypto');
const storage = () => ({values: new Map(), get length() {return this.values.size;},
  key(i) {return [...this.values.keys()][i] ?? null;}, getItem(k) {return this.values.get(k) ?? null;},
  setItem(k, v) {this.values.set(k, String(v));}, removeItem(k) {this.values.delete(k);}});
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {hidden: false, value: '', dataset: {}, style: {}, textContent: '', innerHTML: '',
    disabled: false, listeners: {}, addEventListener(name, fn) {this.listeners[name] = fn;},
    querySelectorAll() {return [];}, pause() {}, removeAttribute() {}});
  return elements.get(id);
}
const first = {document_id: 'doc', project_id: 'project', origin_job_id: 'plan', revision_id: 'v1', number: 1,
  template: {duration: 8, slots: [], metadata: {resolution: {width: 1080, height: 1920}}},
  plan: {selected: [], warnings: []}, source_asset_ids: []};
const calls = []; const notices = []; const downloads = []; let handler; let head = first;
const context = {console, Date, Math, JSON, Set, Map, Number, String, Object, Array, encodeURIComponent,
  authPrincipalId: 'person', tenantId: 'tenant', selectedProjectId: 'project', loadedJobs: [],
  workspaceState: {planJob: {id: 'plan', status: 'succeeded'}, selectedAssetIds: new Set()},
  ACTIVE_JOB_STATUSES: new Set(['queued', 'running']), localStorage: storage(), sessionStorage: storage(),
  document: {querySelector: element, body: {appendChild() {}}, createElement() {return {click() {downloads.push(this.download);}, remove() {}};}},
  URL: {createObjectURL: () => 'blob:offline-export', revokeObjectURL() {}}, Blob: class Blob {}, setTimeout: callback => callback(),
  parseJsonText(text) {try {return JSON.parse(text);} catch {return null;}},
  esc: String, normalizedStatus: value => String(value || '').toLowerCase(), updateWorkspaceControls() {},
  stopWorkspacePolls() {}, friendlyApiError: error => error.message, showToast: text => notices.push(text),
  sha256: input => createHash('sha256').update(input).digest('hex'),
  async api(path, opts = {}) {calls.push({path, ...opts}); return handler(path, opts);},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(require.resolve('../web/editor.js'), 'utf8') + '\n' + fs.readFileSync(require.resolve('../web/clip-editor.js'), 'utf8') + '\n' + fs.readFileSync(require.resolve('../web/subtitle-editor.js'), 'utf8') + '\n' + fs.readFileSync(require.resolve('../web/layer-editor.js'), 'utf8') + `
this.ui = {planEditor, pendingOperations, applyEditorOperations, submitTrackedOperation,
  recoverTrackedOperation, loadPlanEditor, rememberEditorDraft, editorBlocked, renderBelongsToEditor,
  loadEditorCandidates, clearEditorDraft, clearPlanEditor, renderPlanEditor, selectPreciseClip, submitClipAction,
  openSubtitle, saveSubtitleForm, exportSubtitleText, openLayerEditor, saveLayerForm};`, context);
const ui = context.ui;
function current(revision = head) {ui.planEditor.context = 'person:tenant:project'; ui.planEditor.revision = revision;}
handler = async path => {if (path === '/jobs/plan/editing') return head; throw new Error('Unexpected ' + path);};

(async () => {
  await ui.loadPlanEditor(context.workspaceState.planJob);
  const lock = [{op: 'set_lock', selection_id: 'clip', locked: true}];
  handler = async () => {throw Object.assign(new Error('Other editor changed this plan'), {status: 409});};
  await ui.applyEditorOperations(lock, '锁定');
  assert.equal(ui.pendingOperations().length, 0, 'a known rejection is not an unknown submission');
  assert.equal(ui.planEditor.draft.body.base_revision_id, 'v1');
  assert.equal(ui.planEditor.rebased, false);
  assert.ok(ui.editorBlocked(), 'a rejected draft must block rendering');
  const before = calls.length;
  await ui.applyEditorOperations([{op: 'replan'}], '重算');
  assert.equal(calls.length, before, 'a different write cannot silently replace a rejected draft');
  head = {...first, revision_id: 'v2', number: 2};
  handler = async path => {assert.equal(path, '/jobs/plan/editing'); return head;};
  await ui.loadPlanEditor(context.workspaceState.planJob, true);
  assert.equal(ui.planEditor.rebased, true);
  assert.equal(ui.planEditor.draft.body.base_revision_id, 'v1', 'refresh must not mutate/replay the draft');
  assert.equal(ui.planEditor.revision.revision_id, 'v2');
  assert.equal(calls.filter(row => row.method === 'POST' && row.path.endsWith('/revisions')).length, 1);
  ui.clearEditorDraft();

  handler = async () => {throw new Error('Disconnected after sending');};
  await ui.applyEditorOperations(lock, '锁定');
  const pending = ui.pendingOperations()[0];
  assert.equal(pending.body.base_revision_id, 'v2');
  const unknownCall = calls.at(-1);
  assert.equal(ui.pendingOperations().length, 1);
  await ui.applyEditorOperations([{op: 'undo'}], '撤销');
  assert.equal(calls.at(-1), unknownCall, 'unknown outcomes fence subsequent writes');
  handler = async path => {assert.ok(path.includes('/requests/')); return {found: false, revision: null};};
  await ui.recoverTrackedOperation();
  assert.equal(ui.pendingOperations().length, 1, 'a missing receipt is not proof that retry is a new intent');
  head = {...head, revision_id: 'v3', number: 3};
  handler = async (path, opts) => {
    if (path === '/editing/doc/revisions') {
      assert.equal(opts.headers['Idempotency-Key'], unknownCall.headers['Idempotency-Key']);
      assert.equal(opts.body, unknownCall.body); return head;
    }
    if (path === '/jobs/plan/editing') return head;
    throw new Error('Unexpected ' + path);
  };
  await ui.recoverTrackedOperation(true);
  assert.equal(ui.pendingOperations().length, 0);
  assert.equal(ui.planEditor.revision.revision_id, 'v3');
  assert.equal(ui.renderBelongsToEditor({payload: {timeline_revision_id: 'v2'}}), false);
  assert.equal(ui.renderBelongsToEditor({payload: {timeline_revision_id: 'v3'}}), true);

  // An acknowledged write still fences more writes until its new head is loaded.
  let finishSync; let syncStarted;
  const syncing = new Promise(resolve => {syncStarted = resolve;});
  handler = async path => {
    if (path === '/editing/doc/revisions') return {...head, revision_id: 'v4', number: 4};
    syncStarted(); return new Promise(resolve => {finishSync = resolve;});
  };
  const saving = ui.applyEditorOperations(lock, '锁定'); await syncing;
  assert.equal(ui.planEditor.busy, true);
  const duringSync = calls.length;
  await ui.applyEditorOperations([{op: 'undo'}], '撤销');
  assert.equal(calls.length, duringSync);
  head = {...head, revision_id: 'v4', number: 4}; finishSync(head); await saving;
  assert.equal(ui.planEditor.busy, false);

  // Precise inputs use the same rejected-draft contract and never overwrite
  // an unsaved trim with a later config-field event.
  head = {...head, editor: {duration: 8, fps: 30, resolution: {width: 1080, height: 1920}, tracks: [
    {id: 'video-main', type: 'video', muted: false, clips: [{id: 'clip', asset_id: 'source',
      source_start: 0, source_end: 4, range: {start: 0, end: 4}, volume: 1, opacity: 1}]}]}};
  current(head); ui.renderPlanEditor(); ui.selectPreciseClip('clip');
  element('#preciseSourceStart').value = '1.01'; element('#preciseSourceStart').listeners.input();
  element('#preciseSourceEnd').value = '3.99'; element('#preciseSourceEnd').listeners.input();
  assert.equal(ui.planEditor.draft.precise.kind, 'trim');
  assert.equal(element('#preciseVolume').disabled, true, 'a different parameter group cannot erase a trim draft');
  element('#editorTargetDuration').value = '1'; element('#editorTargetDuration').listeners.input();
  assert.equal(ui.planEditor.draft.precise.sourceStart, '1.01');
  handler = async () => {throw Object.assign(new Error('Concurrent trim'), {status: 409});};
  await ui.submitClipAction('trim');
  assert.equal(ui.planEditor.draft.precise.sourceEnd, '3.99');
  const command = JSON.parse(calls.at(-1).body).operations[0];
  assert.deepEqual(command, {op: 'trim_clip', clip_id: 'clip', source_start: 1.01, source_end: 3.99});
  assert.equal(ui.planEditor.draft.body.base_revision_id, 'v4');
  assert.equal(ui.planEditor.rebased, false);
  ui.clearEditorDraft();

  // A subtitle deletion in another editor must retain rejected text, with an
  // explicit conversion to a new cue rather than an automatic overwrite.
  const cue = {id: 'cue', text: '原文', range: {start: 0, end: 2}, style: {font_size: 48, font_color: 'white'},
    placement: {x: 54, y: 1632}, subtitle: {origin: {text: '原文'}, anchor: {parts: [{clip_id: 'clip'}]}}};
  head.editor.tracks.push({id: 'subtitles', type: 'subtitle', clips: [cue]});
  current(head); ui.renderPlanEditor(); ui.openSubtitle('cue');
  const historyButtons = [{dataset: {restoreRevision: 'v4'}, disabled: false}, {dataset: {restoreRevision: 'older'}, disabled: true}];
  element('#editorHistory').querySelectorAll = () => historyButtons;
  ui.renderPlanEditor(); assert.equal(historyButtons[0].disabled, true); assert.equal(historyButtons[1].disabled, false);
  assert.equal(element('#subtitleStart').disabled, true);
  element('#subtitleText').value = '人工修订 <b>字</b>\n第二行'; element('#subtitleText').listeners.input();
  assert.equal(element('#preciseSourceStart').disabled, true);
  handler = async () => {throw Object.assign(new Error('Concurrent subtitle edit'), {status: 409});};
  await ui.saveSubtitleForm();
  assert.equal(ui.planEditor.draft.subtitle.text, '人工修订 <b>字</b>\n第二行');
  assert.equal(ui.planEditor.draft.body.operations[0].text, '人工修订 <b>字</b>\n第二行');
  head = {...head, revision_id: 'v5', number: 5, editor: {...head.editor, tracks: head.editor.tracks.map(track => track.id === 'subtitles' ? {...track, clips: []} : track)}};
  handler = async () => head; await ui.loadPlanEditor(context.workspaceState.planJob, true);
  assert.equal(element('#copySubtitleAsNewBtn').hidden, false);
  assert.equal(element('#subtitleText').value, '人工修订 <b>字</b>\n第二行');
  element('#copySubtitleAsNewBtn').listeners.click();
  assert.equal(ui.planEditor.draft.subtitle.cueId, null);
  assert.equal(ui.planEditor.draft.body, undefined);
  assert.equal(ui.planEditor.draft.subtitle.anchorMode, 'absolute');
  ui.clearEditorDraft();
  head = {...head, timeline_hash: 'hash-v5'}; current(head);
  const content = 'WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nUTF-8字幕\n\n';
  handler = async () => ({revision_id: 'v5', timeline_hash: 'hash-v5', filename: 'subtitles-v5.vtt', content,
    sha256: createHash('sha256').update(content).digest('hex')});
  await ui.exportSubtitleText('vtt'); assert.deepEqual(downloads, ['subtitles-v5.vtt']);
  handler = async () => ({revision_id: 'older', timeline_hash: 'hash-v5', filename: 'wrong.vtt', content, sha256: 'wrong'});
  await ui.exportSubtitleText('vtt'); assert.equal(downloads.length, 1);

  // A query response arriving after project navigation must not populate the new workspace.
  let resolveLoad;
  handler = () => new Promise(resolve => {resolveLoad = resolve;});
  const late = ui.loadPlanEditor(context.workspaceState.planJob, true);
  context.selectedProjectId = 'another-project'; ui.clearPlanEditor();
  resolveLoad(head); await late;
  assert.equal(ui.planEditor.revision, null);
  context.selectedProjectId = 'project'; current();
  let resolveCandidate;
  handler = () => new Promise(resolve => {resolveCandidate = resolve;});
  const candidateLoad = ui.loadEditorCandidates('clip');
  ui.planEditor.candidateSelection = null;
  resolveCandidate({items: [], total: 0}); await candidateLoad;
  assert.equal(ui.planEditor.candidateSelection, null, 'closing candidates fences a late query');
  ui.clearEditorDraft();
  const layer = {id: 'audio-clip', asset_id: 'audio', range: {start: 0, end: 3}, source_start: 1, source_end: 4,
    volume: 0.4, opacity: 1, composition: {audio_fade: {in_seconds: 0.5, out_seconds: 1}, audio_role: 'music'}};
  head = {...head, revision_id: 'layer-v1', editor: {duration: 8, fps: 30, resolution: {width: 1080, height: 1920},
    sources: [{id: 'audio', name: 'Offline audio fixture', kind: 'audio', has_audio: true}], tracks: [{id: 'music', type: 'audio', muted: false, clips: [layer]}],
    advanced_support: {supported: false, reason: '独立音轨待验收'}}};
  current(); ui.renderPlanEditor(); ui.openLayerEditor('audio', 'audio-clip', 'music');
  element('#layerFadeIn').value = '0.75'; element('#layerFadeIn').listeners.input();
  assert.equal(ui.planEditor.draft.layer.fadeIn, '0.75');
  assert.equal(element('#editorTargetDuration').disabled, true);
  const beforeLayerConflict = calls.length;
  await ui.applyEditorOperations([{op: 'configure', target_duration: 3, aspect_ratio: '1:1'}], 'Other form');
  assert.equal(calls.length, beforeLayerConflict, 'a different form cannot erase layer inputs');
  handler = async () => {throw Object.assign(new Error('Concurrent layer delete'), {status: 409});};
  await ui.saveLayerForm();
  assert.equal(ui.planEditor.draft.body.operations[0].fade_in, 0.75);
  head = {...head, revision_id: 'layer-v2', editor: {...head.editor, tracks: [{id: 'music', type: 'audio', clips: []}]}};
  handler = async path => path === '/jobs/plan/editing' ? head : [];
  await ui.loadPlanEditor(context.workspaceState.planJob, true);
  assert.equal(element('#layerFadeIn').value, '0.75');
  assert.equal(element('#saveLayerBtn').disabled, true);
  assert.equal(element('#copyLayerAsNewBtn').hidden, false);
  element('#copyLayerAsNewBtn').listeners.click();
  assert.equal(ui.planEditor.draft.layer.clipId, null);
  assert.equal(ui.planEditor.draft.body, undefined);
  assert.equal(ui.planEditor.draft.layer.fadeIn, '0.75');
  assert.equal(element('#advancedRenderNotice').textContent, '独立音轨待验收');
  ui.clearEditorDraft();
  const originalSet = context.localStorage.setItem;
  context.localStorage.setItem = () => {throw new Error('Quota');};
  const writesBefore = calls.length;
  await assert.rejects(ui.submitTrackedOperation({kind: 'edit', documentId: 'doc', key: 'unsaved', body: {}}));
  assert.equal(calls.length, writesBefore, 'no submission before operation metadata is durable');
  context.localStorage.setItem = originalSet;
  assert.ok(![...context.localStorage.values.values()].some(text => text.includes('https://') || text.includes('access_token')));
  console.log('editor concurrency/recovery contracts: PASS (offline; no cloud/media execution)');
})().catch(error => {console.error(error); process.exitCode = 1;});
