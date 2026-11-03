// Shared classic-script scope. Only operation metadata is persisted; media
// URLs, credentials and server source snapshots never enter this cache.
const planEditor = {context: '', revision: null, loading: null, busy: false, notice: '', draft: null,
  rebased: false, candidateSelection: null, candidates: [], candidateTotal: 0, history: [], preciseClipId: null,
  subtitleSelection: null, originals: [], originalTotal: 0, layerSelection: null};
const EDIT_OP_PREFIX = 'video-ai-studio.operation.';
const EDIT_DRAFT_PREFIX = 'video-ai-studio.edit-draft.';
const editorTabId = (() => {
  try {
    const key = 'video-ai-studio.editor-tab';
    if (!sessionStorage.getItem(key)) sessionStorage.setItem(key, `${Date.now()}-${Math.random()}`);
    return sessionStorage.getItem(key);
  } catch (_) {return `${Date.now()}-${Math.random()}`;}
})();
function editorContext() {return `${authPrincipalId}:${tenantId || ''}:${selectedProjectId || ''}`;}
function editorMatches() {return Boolean(planEditor.revision && planEditor.context === editorContext() && planEditor.revision.origin_job_id === workspaceState.planJob?.id);}
function storedRecords(prefix) {
  const rows = [];
  for (let index = 0; index < (localStorage.length || 0); index++) {
    const key = localStorage.key(index);
    if (!key?.startsWith(prefix)) continue;
    const row = parseJsonText(localStorage.getItem(key));
    if (row && typeof row === 'object') rows.push({storageKey: key, ...row});
  }
  return rows;
}
function pendingOperations() {return storedRecords(EDIT_OP_PREFIX + editorContext() + '.').filter(row => ['edit', 'plan', 'render'].includes(row.kind) && typeof row.key === 'string' && row.body && row.context === editorContext()).sort((a, b) => a.createdAt - b.createdAt);}
function pendingEndpoint(row) {
  if (row.kind === 'plan') return '/jobs/auto-edit';
  if (row.kind === 'edit') return `/editing/${encodeURIComponent(row.documentId)}/revisions`;
  return row.documentId ? `/editing/${encodeURIComponent(row.documentId)}/revisions/${encodeURIComponent(row.revisionId)}/render`
    : `/jobs/${encodeURIComponent(row.jobId)}/${row.body.quality === 'draft' ? 'preview' : 'export'}`;
}
function renderPendingOperations() {
  const panel = document.querySelector('#pendingOperationPanel'); if (!panel) return;
  const pending = pendingOperations(); panel.hidden = !pending.length;
  const message = document.querySelector('#pendingOperationMessage');
  if (message) message.textContent = pending.length ? `尚需核对${pending.length}次提交：${pending[0].label}。核对或继续原请求会保留原操作标识。` : '';
  for (const id of ['checkPendingOperationBtn', 'repeatPendingOperationBtn']) {const button = document.querySelector('#' + id); if (button) button.disabled = planEditor.busy;}
}
function draftPrefix(revision) {return `${EDIT_DRAFT_PREFIX}${editorContext()}.${revision.document_id}.`;}
function rememberEditorDraft(draft) {
  if (!editorMatches()) return;
  planEditor.draft = draft; planEditor.rebased = false;
  try {localStorage.setItem(draftPrefix(planEditor.revision) + editorTabId, JSON.stringify({...draft, createdAt: Date.now()}));}
  catch (_) {planEditor.notice = '草稿暂时只能保留在本页，请检查浏览器存储。';}
}
function clearEditorDraft() {
  if (editorMatches()) localStorage.removeItem(draftPrefix(planEditor.revision) + editorTabId);
  planEditor.draft = null; planEditor.rebased = false;
}
function restoreEditorDraft(revision) {
  const rows = storedRecords(draftPrefix(revision)).sort((a, b) => b.createdAt - a.createdAt);
  const row = rows.find(item => item.storageKey.endsWith('.' + editorTabId));
  if (row) {planEditor.draft = row; planEditor.rebased = false;}
}
function clearPlanEditor() {
  planEditor.context = ''; planEditor.revision = null; planEditor.draft = null; planEditor.rebased = false;
  planEditor.candidateSelection = null; planEditor.candidates = []; planEditor.history = []; planEditor.notice = '';
  planEditor.preciseClipId = null;
  planEditor.subtitleSelection = null; planEditor.originals = []; planEditor.originalTotal = 0;
  planEditor.layerSelection = null;
  const panel = document.querySelector('#planEditor'); if (panel) panel.hidden = true;
  const result = document.querySelector('#planResult'); if (result) result.hidden = false;
  renderPendingOperations();
}
function editorBlocked() {return Boolean(pendingOperations().length || (editorMatches() && (planEditor.busy || planEditor.draft || (planEditor.revision.plan.missing_required_slots || []).length)));}
function renderBelongsToEditor(job) {return !editorMatches() || job?.payload?.timeline_revision_id === planEditor.revision.revision_id;}
function syncEditorRenders() {
  if (!editorMatches()) return;
  const jobs = loadedJobs.filter(renderBelongsToEditor).slice().sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
  workspaceState.previewJob = jobs.find(row => row.type === 'render.draft') || null;
  workspaceState.exportJob = jobs.find(row => ['render.standard', 'render.high', 'render.studio'].includes(row.type)) || null;
}
function renderPlanEditor() {
  const panel = document.querySelector('#planEditor'); if (!panel) return;
  const active = editorMatches(); panel.hidden = !active;
  const result = document.querySelector('#planResult'); if (result) result.hidden = active;
  renderPendingOperations(); if (!active) return;
  const revision = planEditor.revision; const blocked = planEditor.busy || pendingOperations().length > 0;
  document.querySelector('#editorVersion').textContent = `编辑版本 v${revision.number} · ${revision.plan.selected.length} 个镜头`;
  document.querySelector('#editorNotice').textContent = planEditor.notice || (revision.plan.warnings || []).join('；');
  const draftPanel = document.querySelector('#editorDraftPanel'); draftPanel.hidden = !planEditor.draft;
  document.querySelector('#editorDraftLabel').textContent = planEditor.draft?.label || '';
  document.querySelector('#reapplyEditorDraftBtn').disabled = blocked || !planEditor.draft?.body || !planEditor.rebased;
  const shots = document.querySelector('#editorShots');
  const slotNames = Object.fromEntries((revision.template.slots || []).map(slot => [slot.name, slot.description || slot.name]));
  shots.innerHTML = revision.plan.selected.map((row, index) => `<article class="editor-shot${row.locked ? ' locked' : ''}"><strong>${index + 1}. ${esc(slotNames[row.slot] || row.slot)} · ${esc(row.name)}</strong><small>源 ${row.source_start.toFixed(3)}–${row.source_end.toFixed(3)} 秒 · 成片 ${row.timeline_start.toFixed(3)}–${row.timeline_end.toFixed(3)} 秒</small><small>${Object.keys(row.rationale || {}).length ? '已有特征的加权得分 ' + Number(row.score).toFixed(3) : '未提供质量评分，按素材与镜头顺序匹配'}</small><div class="editor-toolbar"><button type="button" class="small-action" data-editor-lock="${esc(row.selection_id)}" ${blocked ? 'disabled' : ''}>${row.locked ? '解锁' : '锁定'}</button><button type="button" class="small-action" data-editor-replace="${esc(row.selection_id)}" ${blocked || row.locked ? 'disabled' : ''}>替换</button><button type="button" class="small-action" data-editor-replan="${esc(row.selection_id)}" ${blocked || row.locked ? 'disabled' : ''}>仅重算此镜头</button></div></article>`).join('');
  shots.querySelectorAll('[data-editor-lock]').forEach(button => button.addEventListener('click', () => {
    const row = revision.plan.selected.find(item => item.selection_id === button.dataset.editorLock);
    applyEditorOperations([{op: 'set_lock', selection_id: row.selection_id, locked: !row.locked}], row.locked ? '解锁镜头' : '锁定镜头');
  }));
  shots.querySelectorAll('[data-editor-replace]').forEach(button => button.addEventListener('click', () => loadEditorCandidates(button.dataset.editorReplace)));
  shots.querySelectorAll('[data-editor-replan]').forEach(button => button.addEventListener('click', () => applyEditorOperations([{op: 'replan', selection_ids: [button.dataset.editorReplan]}], '局部重算镜头')));
  const values = planEditor.draft?.values;
  document.querySelector('#editorTargetDuration').value = values ? values.duration : revision.template.duration ?? '';
  const resolution = revision.template.metadata?.resolution || {};
  document.querySelector('#editorAspectRatio').value = values?.aspect || (resolution.width === resolution.height ? '1:1' : resolution.width > resolution.height ? '16:9' : '9:16');
  for (const id of ['editorTargetDuration', 'editorAspectRatio']) document.querySelector('#' + id).disabled = blocked || Boolean(planEditor.draft?.precise || planEditor.draft?.subtitle || planEditor.draft?.layer || planEditor.draft?.body);
  document.querySelector('#undoEditorBtn').disabled = blocked || !revision.undo_revision_id;
  for (const id of ['replanUnlockedBtn', 'addEditorSourcesBtn']) document.querySelector('#' + id).disabled = blocked;
  const candidates = document.querySelector('#editorCandidatesPanel'); candidates.hidden = !planEditor.candidateSelection;
  document.querySelector('#editorHistory')?.querySelectorAll('[data-restore-revision]').forEach(button => {
    button.disabled = blocked || Boolean(planEditor.draft) || button.dataset.restoreRevision === revision.revision_id;
  });
  if (typeof renderClipEditor === 'function') renderClipEditor();
  if (typeof renderSubtitleEditor === 'function') renderSubtitleEditor();
  if (typeof renderLayerEditor === 'function') renderLayerEditor();
  updateWorkspaceControls();
}
async function loadPlanEditor(job, force = false) {
  renderPendingOperations();
  if (!job?.id || normalizedStatus(job.status) !== 'succeeded') {if (planEditor.revision?.origin_job_id !== job?.id) clearPlanEditor(); return;}
  const context = editorContext(); const same = planEditor.context === context && planEditor.revision?.origin_job_id === job.id;
  if (same && !force && (planEditor.draft || planEditor.busy || pendingOperations().length)) return;
  const key = context + ':' + job.id;
  if (planEditor.loading?.key === key) return planEditor.loading.promise;
  const promise = (async () => {
    try {
      const revision = await api(`/jobs/${encodeURIComponent(job.id)}/editing`, {method: 'POST'});
      if (context !== editorContext() || workspaceState.planJob?.id !== job.id) return;
      if (!revision?.revision_id || revision.project_id !== selectedProjectId) throw new Error('编辑版本尚未就绪');
      const oldId = planEditor.revision?.revision_id;
      const priorRenders = {draft: workspaceState.previewJob ? `${workspaceState.previewJob.id}:${workspaceState.previewJob.status}` : '',
        high: workspaceState.exportJob ? `${workspaceState.exportJob.id}:${workspaceState.exportJob.status}` : ''};
      planEditor.context = context; planEditor.revision = revision;
      if (!same) {planEditor.draft = null; restoreEditorDraft(revision);}
      else if (force && planEditor.draft) planEditor.rebased = true;
      if (oldId !== revision.revision_id) {
        stopWorkspacePolls();
        planEditor.candidateSelection = null; planEditor.candidates = []; planEditor.history = [];
        planEditor.originals = []; planEditor.originalTotal = 0;
        const originals = document.querySelector('#subtitleOriginalList'); if (originals) originals.innerHTML = '';
        const history = document.querySelector('#editorHistory'); if (history) history.innerHTML = '';
        syncEditorRenders();
        const video = document.querySelector('#previewVideo'); if (video) {video.pause?.(); video.removeAttribute?.('src'); video.hidden = true;}
        const artifact = document.querySelector('#remoteArtifact'); if (artifact) {artifact.hidden = true; artifact.innerHTML = '';}
        const status = document.querySelector('#renderStatus'); if (status) status.textContent = `当前导出版本 v${revision.number}`;
      }
      syncEditorRenders();
      renderPlanEditor();
      if (oldId !== revision.revision_id && document.querySelector('#editorHistoryDetails')?.open) await loadEditorHistory();
      if (oldId !== revision.revision_id && document.querySelector('#subtitleOriginalDetails')?.open && typeof loadSubtitleOriginals === 'function') await loadSubtitleOriginals();
      for (const [job, quality] of [[workspaceState.previewJob, 'draft'], [workspaceState.exportJob, 'high']]) {
        if (!job) continue;
        if (oldId === revision.revision_id && priorRenders[quality] === `${job.id}:${job.status}`) continue;
        if (ACTIVE_JOB_STATUSES.has(normalizedStatus(job.status))) pollWorkspaceRender(job.id, quality);
        else if (normalizedStatus(job.status) === 'succeeded') await loadWorkspaceArtifact(job, quality);
      }
    } catch (error) {
      if (context !== editorContext() || workspaceState.planJob?.id !== job.id) return;
      if (same) {planEditor.notice = `版本同步未完成：${friendlyApiError(error)}`; renderPlanEditor();}
      else showToast(`编辑版本暂未加载：${friendlyApiError(error)}`);
    } finally {if (planEditor.loading?.key === key) planEditor.loading = null;}
  })();
  planEditor.loading = {key, promise}; return promise;
}
async function handleTrackedResult(record, result) {
  if (record.context !== editorContext()) return;
  if (record.kind === 'plan') {
    clearPlanEditor(); workspaceState.openPlanJobId = result.id; workspaceState.planJob = result; workspaceState.timelineId = null;
    renderPlanResult(result); await loadJobs(); await loadWorkspace(); setFlowStep('plan');
  } else if (record.kind === 'edit') {
    if (!editorMatches() || record.documentId !== planEditor.revision.document_id) return;
    clearEditorDraft(); planEditor.notice = '修改已保存为新版本。';
    if (record.body.operations?.some(row => row.op === 'save_subtitle' && row.cue_id === null)) planEditor.subtitleSelection = null;
    if (record.body.operations?.some(row => ['save_audio_clip', 'save_overlay_clip'].includes(row.op) && row.clip_id === null)) planEditor.layerSelection = null;
    await loadPlanEditor(workspaceState.planJob, true);
  } else {
    const job = result.job || result;
    if ((record.documentId && planEditor.revision?.document_id !== record.documentId) || workspaceState.planJob?.id !== record.jobId) return;
    if (record.revisionId && planEditor.revision?.revision_id !== record.revisionId) return;
    if (record.body.quality === 'draft') workspaceState.previewJob = job; else workspaceState.exportJob = job;
    renderWorkspaceStatus(job, record.body.quality); setFlowStep('render'); await loadJobs(); pollWorkspaceRender(job.id, record.body.quality);
  }
}
async function submitTrackedOperation(record, repeat = false) {
  if (planEditor.busy) throw new Error('请等待本次操作完成');
  if (!repeat && (typeof pendingBatchRun==='function' && pendingBatchRun() || typeof pendingBatchDelivery==='function' && pendingBatchDelivery())) throw new Error('请先在批次生成页面核对原提交');
  if (!repeat && pendingOperations().length) throw new Error('请先核对上次提交');
  record = {...record, context: record.context || editorContext(), createdAt: record.createdAt || Date.now()};
  record.storageKey ||= EDIT_OP_PREFIX + record.context + '.' + record.key;
  if (record.context !== editorContext()) throw new Error('账号或项目已变化，请回到原项目继续');
  try {localStorage.setItem(record.storageKey, JSON.stringify(record));}
  catch (_) {throw new Error('无法保存操作记录，请检查浏览器存储后再提交');}
  planEditor.busy = true; renderPlanEditor(); renderPendingOperations();
  try {
    let result;
    try {
      result = await api(pendingEndpoint(record), {method: 'POST', headers: {'Idempotency-Key': record.key}, body: JSON.stringify(record.body)});
    } catch (error) {
      if (!repeat && error.status >= 400 && error.status < 500) {
        localStorage.removeItem(record.storageKey);
        if (record.kind === 'edit' && editorMatches() && record.documentId === planEditor.revision.document_id)
          rememberEditorDraft({label: `尚未保存：${record.label}。${friendlyApiError(error)}`, body: record.body,
            values: planEditor.draft?.values, precise: planEditor.draft?.precise, subtitle: planEditor.draft?.subtitle, layer: planEditor.draft?.layer});
      }
      if (record.context === editorContext()) planEditor.notice = error.status >= 400 && error.status < 500 ? friendlyApiError(error) : '尚未确认提交结果，请先核对原提交。';
      throw error;
    }
    localStorage.removeItem(record.storageKey);
    try {await handleTrackedResult(record, result);}
    catch (_) {if (record.context === editorContext()) planEditor.notice = '提交已确认，页面同步未完成，请刷新当前版本。';}
    return result;
  } finally {planEditor.busy = false; renderPlanEditor(); renderPendingOperations();}
}
async function recoverTrackedOperation(repeat = false) {
  const record = pendingOperations()[0]; if (!record || planEditor.busy) return;
  try {
    if (repeat) {await submitTrackedOperation(record, true); return;}
    planEditor.busy = true; renderPendingOperations();
    const context = editorContext();
    const receipt = record.kind === 'edit'
      ? await api(`/editing/${encodeURIComponent(record.documentId)}/requests/${encodeURIComponent(record.key)}`)
      : await api(`/jobs?project_id=${encodeURIComponent(selectedProjectId)}&idempotency_key=${encodeURIComponent(record.key)}&limit=1`);
    if (context !== editorContext()) return;
    const result = record.kind === 'edit' ? receipt.found && receipt.revision : receipt[0];
    if (!result) {showToast('暂未查到结果；可点击“继续原提交”，保留原请求与操作标识。'); return;}
    localStorage.removeItem(record.storageKey);
    await handleTrackedResult(record, result); showToast('原提交已确认。');
  } catch (error) {showToast(friendlyApiError(error));}
  finally {planEditor.busy = false; renderPlanEditor(); renderPendingOperations();}
}
async function applyEditorOperations(operations, label, sourceIds = [], reapply = false) {
  if (!editorMatches()) return;
  if (planEditor.draft && !reapply) {
    const draft = planEditor.draft;
    const config = draft.values && !draft.precise && !draft.subtitle && !draft.layer && !draft.body && operations.every(row => row.op === 'configure');
    const precise = draft.precise && !draft.body && operations.every(row => row.clip_id === draft.precise.clipId);
    const subtitle = draft.subtitle && !draft.body && operations.every(row => row.op === 'save_subtitle' && row.cue_id === draft.subtitle.cueId);
    const layer = draft.layer && !draft.body && operations.every(row => ['save_audio_clip', 'save_overlay_clip'].includes(row.op) && row.clip_id === draft.layer.clipId);
    if (!config && !precise && !subtitle && !layer) return showToast('请先保存或放弃当前未保存修改。');
  }
  const revision = planEditor.revision;
  const body = {base_revision_id: revision.revision_id, operations, source_asset_ids: [...new Set(sourceIds)].sort(), note: ''};
  try {await submitTrackedOperation({kind: 'edit', documentId: revision.document_id, key: 'edit:' + globalThis.sha256(JSON.stringify(body)), body, label});}
  catch (error) {showToast(friendlyApiError(error));}
}
async function loadEditorCandidates(selectionId, append = false) {
  if (!editorMatches() || planEditor.busy) return;
  const revision = planEditor.revision; const context = editorContext();
  if (!append) {
    planEditor.candidateSelection = selectionId; planEditor.candidates = [];
    document.querySelector('#editorCandidates').textContent = '正在读取候选镜头…';
    renderPlanEditor();
  }
  try {
    const skip = append ? planEditor.candidates.length : 0;
    const page = await api(`/editing/${encodeURIComponent(revision.document_id)}/revisions/${encodeURIComponent(revision.revision_id)}/candidates?selection_id=${encodeURIComponent(selectionId)}&skip=${skip}&limit=50`);
    if (context !== editorContext() || planEditor.revision?.revision_id !== revision.revision_id || planEditor.candidateSelection !== selectionId) return;
    planEditor.candidateSelection = selectionId; planEditor.candidates = append ? [...planEditor.candidates, ...page.items] : page.items; planEditor.candidateTotal = page.total;
    const target = document.querySelector('#editorCandidates');
    target.innerHTML = planEditor.candidates.map(row => `<article class="candidate-row"><span><strong>${esc(row.name)}</strong><small>${row.source_start.toFixed(3)}–${row.source_end.toFixed(3)} 秒</small></span><button type="button" class="small-action" data-adopt-candidate="${esc(row.candidate_id)}">采用</button></article>`).join('') || '<p>没有符合条件的替代镜头。可先将其他已分析素材加入候选。</p>';
    target.querySelectorAll('[data-adopt-candidate]').forEach(button => button.addEventListener('click', () => applyEditorOperations([{op: 'replace', selection_id: selectionId, candidate_id: button.dataset.adoptCandidate}], '替换镜头')));
    document.querySelector('#moreEditorCandidatesBtn').hidden = planEditor.candidates.length >= page.total; renderPlanEditor();
  } catch (error) {showToast(friendlyApiError(error));}
}
async function loadEditorHistory(append = false) {
  if (!editorMatches()) return;
  const revision = planEditor.revision; const context = editorContext();
  try {
    const rows = await api(`/editing/${encodeURIComponent(revision.document_id)}/revisions?skip=${append ? planEditor.history.length : 0}&limit=20`);
    if (context !== editorContext() || planEditor.revision?.revision_id !== revision.revision_id) return;
    planEditor.history = append ? [...planEditor.history, ...rows] : rows;
    const target = document.querySelector('#editorHistory');
    target.innerHTML = planEditor.history.map(row => `<article class="candidate-row"><span>v${row.number} · ${esc(relativeDate(row.created_at))}${row.revision_id === revision.revision_id ? ' · 当前版' : ''}</span><button type="button" class="small-action" data-restore-revision="${esc(row.revision_id)}" ${row.revision_id === revision.revision_id || planEditor.busy || pendingOperations().length ? 'disabled' : ''}>恢复为新版本</button></article>`).join('');
    target.querySelectorAll('[data-restore-revision]').forEach(button => button.addEventListener('click', () => applyEditorOperations([{op: 'restore', revision_id: button.dataset.restoreRevision}], '恢复历史版本')));
    document.querySelector('#moreEditorHistoryBtn').hidden = rows.length < 20;
  } catch (error) {showToast(friendlyApiError(error));}
}
function configValues() {return {duration: String(document.querySelector('#editorTargetDuration')?.value || ''), aspect: document.querySelector('#editorAspectRatio')?.value || '9:16'};}
function parsedDuration(value) {if (!String(value).trim()) return null; const duration = Number(value); if (!Number.isFinite(duration) || duration < 0.1 || duration > 600) throw new Error('时长需为0.1至600秒'); return duration;}
async function openPlanFromJob(job) {
  if (job.project_id && job.project_id !== selectedProjectId) return;
  clearPlanEditor(); workspaceState.openPlanJobId = job.id; workspaceState.planJob = job;
  workspaceState.timelineId = job.payload?.timeline_id || null; renderPlanResult(job);
  await loadPlanEditor(job, true); document.querySelector('#studio')?.scrollIntoView({behavior: 'smooth'});
}
document.querySelector('#checkPendingOperationBtn')?.addEventListener('click', () => recoverTrackedOperation());
document.querySelector('#repeatPendingOperationBtn')?.addEventListener('click', () => recoverTrackedOperation(true));
document.querySelector('#refreshEditorBtn')?.addEventListener('click', () => loadPlanEditor(workspaceState.planJob, true));
document.querySelector('#replanUnlockedBtn')?.addEventListener('click', () => applyEditorOperations([{op: 'replan'}], '重算未锁定镜头'));
document.querySelector('#addEditorSourcesBtn')?.addEventListener('click', () => {
  if (!editorMatches()) return;
  const ids = [...workspaceState.selectedAssetIds].filter(id => !planEditor.revision.source_asset_ids.includes(id));
  if (!ids.length) return showToast('请先选择尚未加入候选库的已分析素材');
  applyEditorOperations([{op: 'refresh_candidates'}], '添加候选素材', ids);
});
document.querySelector('#undoEditorBtn')?.addEventListener('click', () => applyEditorOperations([{op: 'undo'}], '撤销上次修改'));
document.querySelector('#closeEditorCandidatesBtn')?.addEventListener('click', () => {planEditor.candidateSelection = null; renderPlanEditor();});
document.querySelector('#moreEditorCandidatesBtn')?.addEventListener('click', () => loadEditorCandidates(planEditor.candidateSelection, true));
document.querySelector('#editorHistoryDetails')?.addEventListener('toggle', event => {if (event.target.open) loadEditorHistory();});
document.querySelector('#moreEditorHistoryBtn')?.addEventListener('click', () => loadEditorHistory(true));
for (const id of ['editorTargetDuration', 'editorAspectRatio']) document.querySelector('#' + id)?.addEventListener('input', () => {
  if (planEditor.draft?.precise || planEditor.draft?.subtitle || planEditor.draft?.layer || planEditor.draft?.body) {renderPlanEditor(); return showToast('请先处理当前未保存修改。');}
  rememberEditorDraft({label: '画幅或时长设置尚未保存。', values: configValues()});
  document.querySelector('#editorDraftPanel').hidden = false;
  document.querySelector('#editorDraftLabel').textContent = '画幅或时长设置尚未保存。';
  document.querySelector('#reapplyEditorDraftBtn').disabled = true; updateWorkspaceControls();
  if (typeof renderSubtitleEditor === 'function') renderSubtitleEditor();
  if (typeof renderClipEditor === 'function') renderClipEditor();
  if (typeof renderLayerEditor === 'function') renderLayerEditor();
});
document.querySelector('#editorConfigForm')?.addEventListener('submit', event => {
  event.preventDefault(); const values = configValues();
  try {applyEditorOperations([{op: 'configure', target_duration: parsedDuration(values.duration), aspect_ratio: values.aspect}], '调整画幅与时长');}
  catch (error) {showToast(friendlyApiError(error));}
});
document.querySelector('#discardEditorDraftBtn')?.addEventListener('click', () => {if (!pendingOperations().length) {clearEditorDraft(); renderPlanEditor();}});
document.querySelector('#reapplyEditorDraftBtn')?.addEventListener('click', () => {
  const draft = planEditor.draft;
  if (draft?.body && planEditor.rebased) applyEditorOperations(draft.body.operations, draft.label, draft.body.source_asset_ids || [], true);
});
