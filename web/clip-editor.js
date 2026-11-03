// Frame-aligned clip controls share the version/CAS/unknown-write machinery.
function preciseSelection() {
  if (!editorMatches()) return null;
  const id = planEditor.draft?.precise?.clipId || planEditor.preciseClipId;
  for (const track of planEditor.revision.editor?.tracks || []) {
    const index = track.clips.findIndex(clip => clip.id === id);
    if (index >= 0) return {track, clip: track.clips[index], index};
  }
  return null;
}
function selectPreciseClip(id) {
  if (planEditor.draft && planEditor.draft.precise?.clipId !== id) return showToast('请先保存或放弃当前未保存修改。');
  const subtitleTrack = planEditor.revision?.editor?.tracks.find(track => track.type === 'subtitle' && track.clips.some(clip => clip.id === id));
  if (subtitleTrack && typeof openSubtitle === 'function') return openSubtitle(id, subtitleTrack.id);
  const layerTrack = planEditor.revision?.editor?.tracks.find(track => ['audio','overlay'].includes(track.type) && track.clips.some(clip => clip.id === id));
  if (layerTrack && typeof openLayerEditor === 'function') return openLayerEditor(layerTrack.type, id, layerTrack.id);
  planEditor.preciseClipId = id; renderClipEditor();
}
function renderClipEditor() {
  const bands = document.querySelector('#editorTrackBands'); if (!bands || !editorMatches()) return;
  const editor = planEditor.revision.editor;
  if (!editor?.tracks) {bands.textContent = ''; return;}
  const names = {video: '主视频', audio: '音轨', overlay: '叠加', subtitle: '字幕'};
  const labels = new Map(planEditor.revision.plan.selected.map(row => [row.clip_id, row.name]));
  document.querySelector('#editorTiming').textContent = `${editor.duration.toFixed(3)} 秒 · ${editor.fps} fps · ${editor.resolution.width}×${editor.resolution.height}`;
  bands.innerHTML = editor.tracks.map(track => `<div class="editor-track"><strong>${esc(names[track.type] || track.type)}${track.muted ? ' · 静音' : ''}</strong><div class="editor-strip">${track.clips.map((clip, index) => `<button type="button" class="clip-segment" data-precise-clip="${esc(clip.id)}" style="flex-grow:${Math.max(0.001, clip.range.end - clip.range.start)}"><span>${index + 1}. ${esc(labels.get(clip.id) || clip.text || clip.id)}</span><small>${clip.range.start.toFixed(3)}–${clip.range.end.toFixed(3)} 秒</small></button>`).join('') || '<span>暂无片段</span>'}</div></div>`).join('');
  bands.querySelectorAll('[data-precise-clip]').forEach(button => button.addEventListener('click', () => selectPreciseClip(button.dataset.preciseClip)));
  const selected = preciseSelection(); const panel = document.querySelector('#preciseClipPanel'); panel.hidden = !selected;
  if (!selected) return;
  const {track, clip, index} = selected; const draft = planEditor.draft?.precise;
  const row = planEditor.revision.plan.selected.find(row => row.clip_id === clip.id);
  const blocked = planEditor.busy || Boolean(pendingOperations().length) || Boolean(planEditor.draft?.body) || Boolean(planEditor.draft?.subtitle) || Boolean(planEditor.draft?.layer);
  document.querySelector('#preciseClipTitle').textContent = `${names[track.type] || track.type} · 第 ${index + 1} 段${row?.locked ? ' · 已锁定' : ''}`;
  document.querySelector('#preciseClipBounds').textContent = row ? `可用源区间 ${row.window_start.toFixed(3)}–${row.window_end.toFixed(3)} 秒；当前源 ${clip.source_start.toFixed(3)}–${Number(clip.source_end).toFixed(3)} 秒。` : '';
  for (const [id, value] of [['preciseSourceStart', draft?.sourceStart ?? clip.source_start], ['preciseSourceEnd', draft?.sourceEnd ?? clip.source_end],
    ['preciseSplitOffset', draft?.splitOffset ?? (clip.range.end - clip.range.start) / 2], ['preciseVolume', draft?.volume ?? clip.volume],
    ['preciseFit', draft?.fit ?? clip.composition?.fit ?? 'contain'], ['preciseTransition', draft?.transitionType ?? clip.transition?.type ?? 'none'],
    ['preciseTransitionDuration', draft?.transitionDuration ?? clip.transition?.duration ?? 0]]) {
    const input = document.querySelector('#' + id); input.value = value ?? ''; input.disabled = blocked || row?.locked || Boolean(planEditor.draft?.values);
  }
  for (const id of ['trimPreciseBtn', 'splitPreciseBtn', 'deletePreciseBtn', 'movePreciseUpBtn', 'movePreciseDownBtn']) {
    document.querySelector('#' + id).disabled = blocked || Boolean(row?.locked) || track.type !== 'video';
  }
  document.querySelector('#movePreciseUpBtn').disabled ||= index === 0 || Boolean(planEditor.draft);
  document.querySelector('#movePreciseDownBtn').disabled ||= index === track.clips.length - 1 || Boolean(planEditor.draft);
  document.querySelector('#deletePreciseBtn').disabled ||= track.clips.length < 2 || Boolean(planEditor.draft);
  document.querySelector('#savePreciseVolumeBtn').disabled = blocked || Boolean(row?.locked) || !['video', 'audio'].includes(track.type);
  for (const id of ['savePreciseFitBtn', 'savePreciseTransitionBtn']) document.querySelector('#' + id).disabled = blocked || row?.locked || track.type !== 'video' || Boolean(planEditor.draft?.values);
  restrictPreciseDraft();
}
function restrictPreciseDraft() {
  const kind = planEditor.draft?.precise?.kind;
  if (!kind) return;
  for (const [id, group] of [['preciseSourceStart', 'trim'], ['preciseSourceEnd', 'trim'], ['preciseSplitOffset', 'split'], ['preciseVolume', 'volume'],
    ['trimPreciseBtn', 'trim'], ['splitPreciseBtn', 'split'], ['savePreciseVolumeBtn', 'volume'],
    ['preciseFit', 'fit'], ['savePreciseFitBtn', 'fit'], ['preciseTransition', 'transition'], ['preciseTransitionDuration', 'transition'], ['savePreciseTransitionBtn', 'transition']]) {
    document.querySelector('#' + id).disabled ||= group !== kind;
  }
  for (const id of ['movePreciseUpBtn', 'movePreciseDownBtn', 'deletePreciseBtn']) document.querySelector('#' + id).disabled = true;
}
function preciseInputs() {
  return {clipId: preciseSelection()?.clip.id, sourceStart: document.querySelector('#preciseSourceStart').value,
    sourceEnd: document.querySelector('#preciseSourceEnd').value, splitOffset: document.querySelector('#preciseSplitOffset').value,
    volume: document.querySelector('#preciseVolume').value, fit: document.querySelector('#preciseFit').value,
    transitionType: document.querySelector('#preciseTransition').value, transitionDuration: document.querySelector('#preciseTransitionDuration').value};
}
function finiteClipInput(id, name) {
  const raw = document.querySelector('#' + id).value;
  const value = Number(raw); if (!String(raw).trim() || !Number.isFinite(value)) throw new Error(`请填写有效的${name}`);
  return value;
}
async function submitClipAction(kind) {
  const selected = preciseSelection(); if (!selected) return;
  const {clip, index} = selected;
  try {
    let action; let label;
    if (kind === 'trim') {action = {op: 'trim_clip', clip_id: clip.id, source_start: finiteClipInput('preciseSourceStart', '源起点'), source_end: finiteClipInput('preciseSourceEnd', '源终点')}; label = '裁切并顺接';}
    else if (kind === 'split') {action = {op: 'split_clip', clip_id: clip.id, offset_seconds: finiteClipInput('preciseSplitOffset', '分割偏移')}; label = '分割片段';}
    else if (kind === 'volume') {action = {op: 'set_clip_audio', clip_id: clip.id, volume: finiteClipInput('preciseVolume', '音量倍数')}; label = '调整原声音量';}
    else if (kind === 'fit') {action = {op: 'set_video_fit', clip_id: clip.id, mode: document.querySelector('#preciseFit').value}; label = '调整画面适配';}
    else if (kind === 'transition') {const type = document.querySelector('#preciseTransition').value; action = {op: 'set_transition', clip_id: clip.id, type, duration: type === 'none' ? 0 : finiteClipInput('preciseTransitionDuration', '转场时长')}; label = '调整片段入场转场';}
    else if (kind === 'delete') {action = {op: 'remove_clip', clip_id: clip.id}; label = '删除片段并顺接';}
    else {action = {op: 'move_clip', clip_id: clip.id, to_index: index + (kind === 'up' ? -1 : 1)}; label = '调整片段顺序';}
    await applyEditorOperations([action], label);
  } catch (error) {showToast(friendlyApiError(error));}
}
for (const id of ['preciseSourceStart', 'preciseSourceEnd', 'preciseSplitOffset', 'preciseVolume', 'preciseFit', 'preciseTransition', 'preciseTransitionDuration']) {
  document.querySelector('#' + id)?.addEventListener('input', () => {
    if (!preciseSelection() || planEditor.busy || pendingOperations().length || planEditor.draft?.body || planEditor.draft?.values || planEditor.draft?.subtitle || planEditor.draft?.layer) return;
    const kind = id === 'preciseFit' ? 'fit' : id.startsWith('preciseTransition') ? 'transition' : id === 'preciseVolume' ? 'volume' : id === 'preciseSplitOffset' ? 'split' : 'trim';
    if (planEditor.draft?.precise?.kind && planEditor.draft.precise.kind !== kind) return;
    rememberEditorDraft({label: '片段参数尚未保存。请保存当前修改或放弃草稿后再进行其他操作。', precise: {...preciseInputs(), kind}});
    document.querySelector('#editorDraftPanel').hidden = false;
    document.querySelector('#editorDraftLabel').textContent = planEditor.draft.label;
    document.querySelector('#reapplyEditorDraftBtn').disabled = true;
    updateWorkspaceControls();
    restrictPreciseDraft();
    if (typeof renderSubtitleEditor === 'function') renderSubtitleEditor();
    if (typeof renderLayerEditor === 'function') renderLayerEditor();
  });
}
for (const [id, kind] of [['trimPreciseBtn', 'trim'], ['splitPreciseBtn', 'split'], ['savePreciseVolumeBtn', 'volume'], ['savePreciseFitBtn', 'fit'], ['savePreciseTransitionBtn', 'transition'], ['deletePreciseBtn', 'delete'], ['movePreciseUpBtn', 'up'], ['movePreciseDownBtn', 'down']]) {
  document.querySelector('#' + id)?.addEventListener('click', () => submitClipAction(kind));
}
