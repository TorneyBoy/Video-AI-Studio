// Compile and execute first-party page/state code with an offline wx transport.
// This does not claim a WeChat developer-tools compile or device acceptance.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {execFileSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const root = path.resolve(__dirname, '..');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'video-ai-mini-studio-'));
const watchdog = setTimeout(() => {console.error('Native contract did not finish'); process.exit(1);}, 15000);
let page;
const values = new Map();
global.wx = {getStorageSync: key => values.get(key), setStorageSync: (key, value) => values.set(key, structuredClone(value)),
  removeStorageSync: key => values.delete(key), getStorageInfoSync: () => ({keys: [...values.keys()]})};
global.Page = definition => {page = definition;};

(async () => {
  execFileSync(process.execPath, [path.join(root, 'miniprogram/node_modules/typescript/bin/tsc'),
    '--project', path.join(root, 'miniprogram/tsconfig.json'), '--noEmit', 'false', '--module', 'Node16',
    '--moduleResolution', 'Node16', '--rootDir', path.join(root, 'miniprogram/src'), '--outDir', output], {stdio: 'pipe'});
  fs.cpSync(path.join(root, 'miniprogram/src/vendor'), path.join(output, 'vendor'), {recursive: true});
  const apiPath = path.join(output, 'shared/api.js');
  const personalSession = {profile: {id: 'person', tenant_id: 'tenant'}, activeProject: () => project, async restore() {return this.profile;},
    async request(...args) {calls.push(args); return handler(...args);}};
  require.cache[apiPath] = {id: apiPath, filename: apiPath, loaded: true,
    exports: {personalSession, message: error => error.message}};
  const state = require(path.join(output, 'shared/studio-state.js'));
  const {ClientError} = require(path.join(output, 'shared/contracts.js'));
  let project = 'project'; let handler; const calls = [];
  const body = {base_revision_id: 'v1', operations: [{op: 'set_lock', selection_id: 'clip', locked: true}]};
  const record = state.operation('edit', 'stable-edit', body, '锁定', {documentId: 'doc'});
  handler = async () => {throw new ClientError('submission_unknown', 'Disconnected', 0, true);};
  await assert.rejects(state.sendStudioOperation(record));
  assert.equal(state.pendingStudioOperations().length, 1);
  const sent = calls.at(-1);
  await assert.rejects(state.sendStudioOperation(state.operation('edit', 'another', body, 'Other', {documentId: 'doc'})),
    error => error.code === 'operation_unconfirmed');
  handler = async (...args) => {assert.deepEqual(args, sent); return {revision_id: 'v2'};};
  assert.equal((await state.sendStudioOperation(record, true)).revision_id, 'v2');
  assert.equal(state.pendingStudioOperations().length, 0);
  handler = async () => {throw new ClientError('edit_conflict', 'Conflict', 409);};
  await assert.rejects(state.sendStudioOperation(record));
  assert.equal(state.pendingStudioOperations().length, 0);
  project = 'another-project';
  await assert.rejects(state.sendStudioOperation(record), error => error.code === 'workspace_changed');
  project = 'project';
  const originalSet = wx.setStorageSync;
  wx.setStorageSync = () => {throw new Error('Quota');};
  const before = calls.length;
  await assert.rejects(state.sendStudioOperation(record), error => error.code === 'operation_cache_unavailable');
  assert.equal(calls.length, before);
  wx.setStorageSync = originalSet;

  require(path.join(output, 'pages/studio/index.js'));
  page.data = structuredClone(page.data); page.setData = fields => Object.assign(page.data, fields);
  state.writeStudioCache({planId: 'old-plan'});
  const job = id => ({id, project_id: 'project', status: 'failed', type: 'auto_edit.plan', payload: {},
    failure: {message: 'Fixture'}, attempt_count: 3, max_attempts: 3, created_at: '2026-10-18T00:00:00Z'});
  handler = async endpoint => {
    if (endpoint.startsWith('/templates?')) return [{id: 'template', name: 'Fixture', content: {}}];
    if (endpoint.startsWith('/jobs?')) return [job('recent-plan')];
    if (endpoint === '/jobs/old-plan') return job('old-plan');
    throw new Error('Unexpected ' + endpoint);
  };
  await page.onShow();
  assert.equal(page.data.plan.id, 'old-plan', 'opening the page must preserve an older explicitly selected plan');
  assert.equal(page.data.plans[page.data.planIndex].id, 'old-plan');
  const revision = {document_id: 'doc', revision_id: 'v1', origin_job_id: 'old-plan', number: 1,
    template: {duration: 8, slots: [], metadata: {}}, plan: {selected: [], warnings: []}, source_asset_ids: [],
    editor: {duration: 8, fps: 30, resolution: {width: 1080, height: 1920}, tracks: [{id: 'main', type: 'video', muted: false,
      clips: [{id: 'clip', asset_id: 'source', source_start: 0, source_end: 4, range: {start: 0, end: 4}, volume: 1, opacity: 1, text: null}]}]}};
  const readyPlan = {...job('old-plan'), status: 'succeeded'};
  handler = async endpoint => endpoint === '/jobs/old-plan/editing' ? revision : [];
  await page.showPlan(readyPlan, true);
  page.setData({editTarget: '3'}); page.rememberConfig();
  handler = async () => {throw new ClientError('edit_conflict', 'Other page saved a new version', 409);};
  await page.saveOperations([{op: 'configure', target_duration: 3, aspect_ratio: '9:16'}], '调整');
  assert.equal(page.data.draftRebased, false);
  assert.equal(page.data.canRender, false);
  assert.equal(state.editorDraft('doc').body.base_revision_id, 'v1');
  const nextRevision = {...revision, revision_id: 'v2', number: 2};
  handler = async endpoint => endpoint === '/jobs/old-plan' ? readyPlan : endpoint === '/jobs/old-plan/editing' ? nextRevision : [];
  await page.refreshRevision();
  assert.equal(page.data.revision.revision_id, 'v2');
  assert.equal(page.data.editTarget, '3');
  assert.equal(page.data.draftRebased, true);
  assert.equal(state.editorDraft('doc').body.base_revision_id, 'v1');
  state.removeEditorDraft('doc'); page.setData({draftLabel: ''}); await page.showPlan(readyPlan, true);
  page.choosePrecise({currentTarget: {dataset: {id: 'clip'}}});
  page.preciseInput({currentTarget: {dataset: {field: 'sourceStart'}}, detail: {value: '1.01'}});
  page.preciseInput({currentTarget: {dataset: {field: 'sourceEnd'}}, detail: {value: '3.99'}});
  assert.equal(page.data.preciseKind, 'trim');
  const beforeConfig = calls.length;
  await page.saveOperations([{op: 'configure', target_duration: 1, aspect_ratio: '1:1'}], 'Config');
  assert.equal(calls.length, beforeConfig, 'a different action cannot silently erase the trim draft');
  handler = async () => {throw new ClientError('edit_conflict', 'Concurrent trim', 409);};
  await page.preciseAction({currentTarget: {dataset: {kind: 'trim'}}});
  assert.equal(state.editorDraft('doc').precise.sourceStart, '1.01');
  assert.deepEqual(state.editorDraft('doc').body.operations[0], {op: 'trim_clip', clip_id: 'clip', source_start: 1.01, source_end: 3.99});
  assert.equal(page.data.hasRejectedDraft, true);
  state.removeEditorDraft('doc'); page.setData({draftLabel: ''});
  const cue = {id: 'cue', text: '原文', range: {start: 0, end: 2}, style: {font_size: 48, font_color: 'white'}, placement: {x: 54, y: 1632},
    subtitle: {origin: {text: '原文'}, anchor: {parts: [{clip_id: 'clip'}]}}};
  let subtitleRevision = {...nextRevision, timeline_hash: 'hash-v2', editor: {...nextRevision.editor, tracks: [...nextRevision.editor.tracks,
    {id: 'subtitles', type: 'subtitle', clips: [cue]}]}};
  handler = async endpoint => endpoint === '/jobs/old-plan/editing' ? subtitleRevision : [];
  await page.showPlan(readyPlan, true); page.editSubtitle({currentTarget: {dataset: {id: 'cue'}}});
  page.subtitleInput({currentTarget: {dataset: {field: 'text'}}, detail: {value: '人工修订\n第二行'}});
  handler = async () => {throw new ClientError('edit_conflict', 'Concurrent subtitle change', 409);};
  await page.saveSubtitle();
  assert.equal(state.editorDraft('doc').subtitle.text, '人工修订\n第二行');
  assert.equal(state.editorDraft('doc').body.operations[0].text, '人工修订\n第二行');
  subtitleRevision = {...subtitleRevision, revision_id: 'v3', timeline_hash: 'hash-v3', editor: {...subtitleRevision.editor,
    tracks: subtitleRevision.editor.tracks.map(track => track.id === 'subtitles' ? {...track, clips: []} : track)}};
  handler = async endpoint => endpoint === '/jobs/old-plan' ? readyPlan : endpoint === '/jobs/old-plan/editing' ? subtitleRevision : [];
  await page.refreshRevision();
  assert.equal(page.data.subtitleMissing, true);
  assert.equal(page.data.subtitleForm.text, '人工修订\n第二行');
  page.copySubtitleAsNew();
  assert.equal(page.data.subtitleForm.cueId, null);
  assert.equal(page.data.subtitleForm.anchorMode, 'absolute');
  assert.equal(state.editorDraft('doc').body, undefined);
  subtitleRevision = {...subtitleRevision, revision_id: 'v4', timeline_hash: 'hash-v4', editor: {...subtitleRevision.editor,
    tracks: subtitleRevision.editor.tracks.map(track => track.id === 'subtitles' ? {...track, clips: [{...cue, id: 'new-cue', text: '人工修订\n第二行', subtitle: {}}]} : track)}};
  handler = async endpoint => endpoint === '/editing/doc/revisions' || endpoint === '/jobs/old-plan/editing' ? subtitleRevision : [];
  await page.saveSubtitle();
  assert.equal(page.data.subtitleCues[0].text, '人工修订\n第二行');
  assert.equal(page.data.subtitleEditing, false);
  const written = [];
  wx.env = {USER_DATA_PATH: 'wxfile://offline-test'};
  wx.getFileSystemManager = () => ({writeFile(options) {written.push(options); options.success();}});
  const content = '1\n00:00:00,000 --> 00:00:02,000\n人工修订\n第二行\n\n';
  handler = async () => ({revision_id: 'v4', timeline_hash: 'hash-v4', filename: 'subtitles-v4.srt', content,
    sha256: createHash('sha256').update(content).digest('hex')});
  await page.exportSubtitles({currentTarget: {dataset: {format: 'srt'}}});
  assert.equal(written.length, 1); assert.equal(written[0].data, content); assert.equal(written[0].encoding, 'utf8');
  assert.equal(page.data.subtitleExportPath, 'wxfile://offline-test/subtitles-v4.srt');
  handler = async () => ({revision_id: 'wrong', timeline_hash: 'wrong', filename: 'wrong.srt', content, sha256: 'wrong'});
  await page.exportSubtitles({currentTarget: {dataset: {format: 'srt'}}});
  assert.equal(written.length, 1);
  assert.ok(!JSON.stringify([...values.values()]).includes('wxfile://'), 'export paths are not operation/draft metadata');
  state.removeEditorDraft('doc'); page.setData({draftLabel: '', layerEditing: false});
  const audioLayer = {id: 'audio-clip', asset_id: 'audio', range: {start: 0, end: 3}, source_start: 1, source_end: 4, volume: 0.4, opacity: 1,
    composition: {audio_fade: {in_seconds: 0.5, out_seconds: 1}, audio_role: 'music'}};
  let layerRevision = {...subtitleRevision, revision_id: 'layers-v1', editor: {...revision.editor,
    sources: [{id: 'audio', name: 'Offline audio fixture', kind: 'audio', has_audio: true}],
    tracks: [...revision.editor.tracks, {id: 'music', type: 'audio', muted: false, clips: [audioLayer]}],
    advanced_support: {supported: false, reason: '音轨待验收', required_features: ['audio_tracks'], missing_features: ['audio_tracks']}}};
  handler = async endpoint => endpoint === '/jobs/old-plan/editing' ? layerRevision : [];
  await page.showPlan(readyPlan, true);
  assert.equal(page.data.canRender, false);
  page.openLayer('audio', 'audio-clip', 'music');
  page.layerInput({currentTarget: {dataset: {field: 'fadeIn'}}, detail: {value: '0.75'}});
  assert.equal(state.editorDraft('doc').layer.fadeIn, '0.75');
  const beforeLayer = calls.length;
  await page.saveOperations([{op: 'configure', target_duration: 3, aspect_ratio: '1:1'}], 'Other form');
  assert.equal(calls.length, beforeLayer);
  handler = async () => {throw new ClientError('edit_conflict', 'Concurrent layer deletion', 409);};
  await page.saveLayer();
  assert.equal(state.editorDraft('doc').body.operations[0].fade_in, 0.75);
  layerRevision = {...layerRevision, revision_id: 'layers-v2', editor: {...layerRevision.editor,
    tracks: layerRevision.editor.tracks.map(track => track.id === 'music' ? {...track, clips: []} : track)}};
  handler = async endpoint => endpoint === '/jobs/old-plan' ? readyPlan : endpoint === '/jobs/old-plan/editing' ? layerRevision : [];
  await page.refreshRevision();
  assert.equal(page.data.layerMissing, true); assert.equal(page.data.layerForm.fadeIn, '0.75');
  page.copyLayerAsNew();
  assert.equal(page.data.layerForm.clipId, null); assert.equal(state.editorDraft('doc').body, undefined);
  assert.equal(page.data.layerForm.fadeIn, '0.75');
  let resolve;
  handler = () => new Promise(done => {resolve = done;});
  const late = page.load();
  personalSession.profile = null; page.onHide();
  resolve([]); await late;
  assert.equal(page.data.loading, true, 'a hidden page must ignore an old load completion');
  await page.onShow();
  assert.equal(page.data.signedIn, false);
  assert.equal(page.data.revision, null);
  assert.equal(page.data.plan, null);
  page.onUnload();
  assert.ok(!JSON.stringify([...values.values()]).includes('access_token'));
  console.log('native studio state/lifecycle: PASS (offline runtime; no WeChat/device/media execution)');
})().catch(error => {console.error(error.stdout?.toString() || error); process.exitCode = 1;}).finally(() => {
  clearTimeout(watchdog);
  if (!path.resolve(output).startsWith(path.resolve(os.tmpdir()) + path.sep + 'video-ai-mini-studio-')) throw new Error('Unsafe temporary path');
  fs.rmSync(output, {recursive: true, force: true});
});
