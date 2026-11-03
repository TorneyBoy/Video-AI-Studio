// Original text stays on the server/version; only explicit editor inputs are drafts.
const SUB_FIELDS = {text: 'subtitleText', start: 'subtitleStart', end: 'subtitleEnd', fontSize: 'subtitleFontSize',
  color: 'subtitleColor', x: 'subtitleX', y: 'subtitleY', anchorMode: 'subtitleAnchorMode', anchorClipId: 'subtitleAnchorClip'};
function subtitleRows() {
  return (planEditor.revision?.editor?.tracks || []).filter(track => track.type === 'subtitle')
    .flatMap(track => track.clips.map(clip => ({trackId: track.id, clip})));
}
function subtitleRaw() {
  const selection = planEditor.subtitleSelection || {};
  return {...Object.fromEntries(Object.entries(SUB_FIELDS).map(([key, id]) => [key, String(document.querySelector('#' + id).value ?? '')])),
    cueId: selection.cueId ?? null, trackId: selection.trackId || 'subtitles'};
}
function openSubtitle(cueId = null, trackId = 'subtitles') {
  if (planEditor.draft && !(planEditor.draft.subtitle && planEditor.draft.subtitle.cueId === cueId)) return showToast('请先处理当前未保存修改。');
  planEditor.subtitleSelection = {cueId, trackId}; renderSubtitleEditor();
}
function renderSubtitleEditor() {
  const panel = document.querySelector('#subtitleEditor'); if (!panel || !editorMatches()) return;
  const rows = subtitleRows(); const blocked = planEditor.busy || Boolean(pendingOperations().length);
  const list = document.querySelector('#subtitleCueList');
  list.innerHTML = rows.map(({clip, trackId}, index) => `<article class="subtitle-row"><div><strong>${index + 1}. ${esc(clip.text)}</strong><small>${clip.range.start.toFixed(3)}–${clip.range.end.toFixed(3)} 秒 · ${clip.subtitle?.anchor ? '随来源片段' : '固定时间'}</small>${clip.subtitle?.origin ? `<small>原文：${esc(clip.subtitle.origin.text)}</small>` : ''}${clip.subtitle?.partial ? '<small class="subtitle-partial">仅使用原句部分音频，请审阅文字。</small>' : ''}</div><button type="button" class="small-action" data-edit-subtitle="${esc(clip.id)}" data-track="${esc(trackId)}" ${blocked ? 'disabled' : ''}>编辑这句</button><button type="button" class="small-action" data-delete-subtitle="${esc(clip.id)}" ${blocked || planEditor.draft ? 'disabled' : ''}>删除这句</button></article>`).join('') || '<p class="muted">还没有字幕。可导入已有识别结果，或手动添加。</p>';
  list.querySelectorAll('[data-edit-subtitle]').forEach(button => button.addEventListener('click', () => openSubtitle(button.dataset.editSubtitle, button.dataset.track)));
  list.querySelectorAll('[data-delete-subtitle]').forEach(button => button.addEventListener('click', () => applyEditorOperations([{op: 'delete_subtitle', cue_id: button.dataset.deleteSubtitle}], '删除字幕')));
  for (const id of ['importTranscriptBtn', 'newSubtitleBtn']) document.querySelector('#' + id).disabled = blocked || Boolean(planEditor.draft);
  for (const id of ['exportSrtBtn', 'exportVttBtn']) document.querySelector('#' + id).disabled = blocked || Boolean(planEditor.draft) || !rows.length;
  const form = document.querySelector('#subtitleForm');
  const raw = planEditor.draft?.subtitle;
  if (raw) planEditor.subtitleSelection = {cueId: raw.cueId, trackId: raw.trackId};
  const selection = planEditor.subtitleSelection; form.hidden = !selection;
  if (!selection) return;
  const found = rows.find(row => row.clip.id === selection.cueId);
  const missing = Boolean(selection.cueId && !found);
  if (missing && !raw) {form.hidden = true; return;}
  const cue = found?.clip; const editor = planEditor.revision.editor;
  const sourceClips = (editor?.tracks || []).filter(track => track.type === 'video').flatMap(track => track.clips);
  const choose = document.querySelector('#subtitleAnchorClip');
  choose.innerHTML = sourceClips.map((clip, index) => `<option value="${esc(clip.id)}">第 ${index + 1} 段 · ${clip.range.start.toFixed(3)}–${clip.range.end.toFixed(3)} 秒</option>`).join('');
  const values = raw || {cueId: selection.cueId, trackId: selection.trackId, text: cue?.text || '', start: cue?.range.start ?? 0,
    end: cue?.range.end ?? Math.min(2, editor?.duration || 2), fontSize: cue?.style?.font_size || 48,
    color: cue?.style?.font_color || 'white', x: cue?.placement?.x ?? Math.round((editor?.resolution.width || 1080) * 0.05),
    y: cue?.placement?.y ?? Math.round((editor?.resolution.height || 1920) * 0.85),
    anchorMode: cue?.subtitle?.anchor ? 'keep' : 'absolute', anchorClipId: cue?.subtitle?.anchor?.parts?.[0]?.clip_id || sourceClips[0]?.id || ''};
  document.querySelector('#subtitleFormTitle').textContent = missing ? '原句已在新版删除，文字草稿仍保留' : cue ? '修订这句字幕' : '新增字幕';
  const foreign = planEditor.draft && !raw;
  for (const [key, id] of Object.entries(SUB_FIELDS)) {
    const element = document.querySelector('#' + id); element.value = values[key] ?? '';
    element.disabled = blocked || Boolean(planEditor.draft?.body) || Boolean(foreign);
  }
  document.querySelector('#saveSubtitleBtn').disabled = blocked || Boolean(planEditor.draft?.body) || Boolean(foreign) || missing;
  document.querySelector('#copySubtitleAsNewBtn').hidden = !missing;
  document.querySelector('#copySubtitleAsNewBtn').disabled = blocked;
  updateSubtitleMode();
}
function updateSubtitleMode() {
  const mode = document.querySelector('#subtitleAnchorMode')?.value;
  if (mode === 'keep') {document.querySelector('#subtitleStart').disabled = true; document.querySelector('#subtitleEnd').disabled = true;}
  document.querySelector('#subtitleAnchorClip').disabled ||= mode !== 'clip';
  const preview = document.querySelector('#subtitleStylePreview');
  if (preview) {preview.textContent = document.querySelector('#subtitleText').value || '字幕样式示意';
    const size = Number(document.querySelector('#subtitleFontSize').value); preview.style.fontSize = Math.max(12, Math.min(32, Number.isFinite(size) ? size / 2 : 24)) + 'px';
    const color = document.querySelector('#subtitleColor').value; preview.style.color = /^(white|black|red|#[A-Fa-f0-9]{6})$/.test(color) ? color : 'white';}
}
function rememberSubtitle() {
  if (!editorMatches() || !planEditor.subtitleSelection || planEditor.busy || pendingOperations().length || planEditor.draft?.body || (planEditor.draft && !planEditor.draft.subtitle)) return;
  rememberEditorDraft({label: '字幕修改尚未保存。', subtitle: subtitleRaw()});
  document.querySelector('#editorDraftPanel').hidden = false; document.querySelector('#editorDraftLabel').textContent = '字幕修改尚未保存。';
  document.querySelector('#reapplyEditorDraftBtn').disabled = true; updateWorkspaceControls();
  for (const id of ['preciseSourceStart', 'preciseSourceEnd', 'preciseSplitOffset', 'preciseVolume', 'editorTargetDuration', 'editorAspectRatio']) {
    const field = document.querySelector('#' + id); if (field) field.disabled = true;
  }
  for (const id of ['subtitleStart', 'subtitleEnd', 'subtitleAnchorClip']) document.querySelector('#' + id).disabled = false;
  updateSubtitleMode();
  if (typeof renderClipEditor === 'function') renderClipEditor();
    if (typeof renderLayerEditor === 'function') renderLayerEditor();
}
async function saveSubtitleForm() {
  if (!editorMatches()) return; const raw = subtitleRaw();
  const number = (key, label) => {const result = Number(raw[key]); if (!raw[key].trim() || !Number.isFinite(result)) throw new Error(`请填写有效的${label}`); return result;};
  try {
    const font = number('fontSize', '字号'); if (!Number.isInteger(font)) throw new Error('字号需为整数');
    await applyEditorOperations([{op: 'save_subtitle', cue_id: raw.cueId, track_id: raw.trackId, text: raw.text,
      start: number('start', '字幕起点'), end: number('end', '字幕终点'), anchor_mode: raw.anchorMode,
      anchor_clip_id: raw.anchorMode === 'clip' ? raw.anchorClipId : null,
      style: {font_size: font, font_color: raw.color}, position: {x: number('x', '横向位置'), y: number('y', '纵向位置')}}], '保存字幕修订');
  } catch (error) {showToast(friendlyApiError(error));}
}
async function loadSubtitleOriginals(append = false) {
  if (!editorMatches()) return; const revision = planEditor.revision; const context = editorContext();
  try {
    const rows = await api(`/editing/${encodeURIComponent(revision.document_id)}/revisions/${encodeURIComponent(revision.revision_id)}/transcripts?skip=${append ? planEditor.originals.length : 0}&limit=50`);
    if (context !== editorContext() || planEditor.revision?.revision_id !== revision.revision_id) return;
    planEditor.originals = append ? [...planEditor.originals, ...rows.items] : rows.items; planEditor.originalTotal = rows.total;
    document.querySelector('#subtitleOriginalList').innerHTML = planEditor.originals.map(row => `<article class="subtitle-original"><strong>${esc(row.name)}</strong><small>源 ${row.start.toFixed(3)}–${row.end.toFixed(3)} 秒</small><p>${esc(row.text)}</p></article>`).join('') || '<p>该版本没有保存可用原文。导入已有识别时可读取当前项目的分析结果，不发起新识别。</p>';
    document.querySelector('#moreSubtitleOriginalsBtn').hidden = planEditor.originals.length >= rows.total;
  } catch (error) {showToast(friendlyApiError(error));}
}
async function exportSubtitleText(format) {
  if (!editorMatches() || planEditor.busy || pendingOperations().length || planEditor.draft) return showToast('请先处理未保存修改或待核对提交。');
  const revision = planEditor.revision; const context = editorContext();
  try {
    const file = await api(`/editing/${encodeURIComponent(revision.document_id)}/revisions/${encodeURIComponent(revision.revision_id)}/subtitles?format=${format}`);
    if (context !== editorContext() || planEditor.revision?.revision_id !== revision.revision_id) return;
    if (file.revision_id !== revision.revision_id || file.timeline_hash !== revision.timeline_hash || globalThis.sha256(file.content) !== file.sha256) throw new Error('字幕文件与当前版本或内容校验不符');
    const url = URL.createObjectURL(new Blob([file.content], {type: 'text/plain;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = file.filename; document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000); showToast(`已导出当前 v${revision.number} 的 ${format.toUpperCase()} 文字和时间。`);
  } catch (error) {showToast(friendlyApiError(error));}
}
document.querySelector('#importTranscriptBtn')?.addEventListener('click', () => applyEditorOperations([{op: 'import_transcript'}], '导入已有识别字幕'));
document.querySelector('#newSubtitleBtn')?.addEventListener('click', () => openSubtitle());
document.querySelector('#copySubtitleAsNewBtn')?.addEventListener('click', () => {
  if (planEditor.busy || pendingOperations().length || !planEditor.draft?.subtitle) return;
  const subtitle = {...planEditor.draft.subtitle, cueId: null, anchorMode: 'absolute'};
  rememberEditorDraft({label: '已将保留文字转为新增字幕草稿，请审阅时间后保存。', subtitle});
  planEditor.subtitleSelection = {cueId: null, trackId: subtitle.trackId}; renderPlanEditor();
});
document.querySelector('#subtitleForm')?.addEventListener('submit', event => {event.preventDefault(); saveSubtitleForm();});
document.querySelector('#closeSubtitleFormBtn')?.addEventListener('click', () => {if (!planEditor.draft) {planEditor.subtitleSelection = null; renderSubtitleEditor();}});
for (const id of Object.values(SUB_FIELDS)) document.querySelector('#' + id)?.addEventListener('input', rememberSubtitle);
document.querySelector('#subtitleOriginalDetails')?.addEventListener('toggle', event => {if (event.target.open) loadSubtitleOriginals();});
document.querySelector('#moreSubtitleOriginalsBtn')?.addEventListener('click', () => loadSubtitleOriginals(true));
document.querySelector('#exportSrtBtn')?.addEventListener('click', () => exportSubtitleText('srt'));
document.querySelector('#exportVttBtn')?.addEventListener('click', () => exportSubtitleText('vtt'));
