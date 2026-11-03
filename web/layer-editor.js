// Audio and picture layers use the same immutable save/receipt flow.
const LAYER_FIELDS = {trackId: 'layerTrackName', assetId: 'layerAsset', start: 'layerStart', end: 'layerEnd', sourceStart: 'layerSourceStart',
  volume: 'layerVolume', fadeIn: 'layerFadeIn', fadeOut: 'layerFadeOut', role: 'layerAudioRole', x: 'layerX', y: 'layerY', width: 'layerWidth', height: 'layerHeight', opacity: 'layerOpacity', fit: 'layerFit'};
function openLayerEditor(kind, clipId = null, trackId = null) {
  if (!editorMatches()) return;
  if (planEditor.draft && !(planEditor.draft.layer && planEditor.draft.layer.clipId === clipId)) return showToast('请先处理当前未保存修改。');
  planEditor.layerSelection = {kind, clipId, trackId: trackId || (kind === 'audio' ? '配乐' : '叠加')}; renderLayerEditor();
}
function layerRaw() {return {...planEditor.layerSelection, ...Object.fromEntries(Object.entries(LAYER_FIELDS).map(([key, id]) => [key, String(document.querySelector('#' + id).value ?? '')]))};}
function renderLayerEditor() {
  if (!editorMatches() || !document.querySelector('#layerEditor')) return;
  const editor = planEditor.revision.editor; const blocked = planEditor.busy || Boolean(pendingOperations().length);
  document.querySelector('#advancedRenderNotice').textContent = editor?.advanced_support?.supported === false ? editor.advanced_support.reason : '';
  const tracks = editor?.tracks || []; const list = document.querySelector('#layerTrackList');
  list.innerHTML = tracks.map((track, index) => `<article class="layer-track"><div class="editor-heading"><strong>${esc(({video: '主视频', audio: '音轨', overlay: '叠加', subtitle: '字幕'})[track.type])} · ${esc(track.id)}${track.muted ? ' · 已隐藏/静音' : ''}</strong><div class="editor-toolbar"><button type="button" class="small-action" data-mute-track="${esc(track.id)}" ${blocked || planEditor.draft ? 'disabled' : ''}>${['video','audio'].includes(track.type) ? (track.muted ? '恢复声音' : (track.type === 'video' ? '静音原声' : '静音')) : (track.muted ? '显示' : '隐藏')}</button>${track.type !== 'video' ? `<button type="button" class="small-action" data-move-track="${esc(track.id)}" data-index="${index - 1}" ${blocked || planEditor.draft || index <= 1 ? 'disabled' : ''}>轨道上移</button><button type="button" class="small-action" data-move-track="${esc(track.id)}" data-index="${index + 1}" ${blocked || planEditor.draft || index >= tracks.length - 1 ? 'disabled' : ''}>轨道下移</button>` : ''}${['audio','overlay'].includes(track.type) ? `<button type="button" class="small-action" data-delete-track="${esc(track.id)}" ${blocked || planEditor.draft ? 'disabled' : ''}>删除整轨</button>` : ''}</div></div>${['audio','overlay'].includes(track.type) ? track.clips.map(clip => `<div class="layer-clip"><span>${esc(editor.sources?.find(source => source.id === clip.asset_id)?.name || clip.asset_id || clip.id)} · ${clip.range.start.toFixed(3)}–${clip.range.end.toFixed(3)} 秒</span><button class="small-action" type="button" data-edit-layer="${esc(clip.id)}" data-track="${esc(track.id)}" data-kind="${track.type}">编辑片段</button><button class="small-action" type="button" data-delete-layer="${esc(clip.id)}" ${blocked || planEditor.draft ? 'disabled' : ''}>删除片段</button></div>`).join('') : ''}</article>`).join('');
  list.querySelectorAll('[data-edit-layer]').forEach(button => button.addEventListener('click', () => openLayerEditor(button.dataset.kind, button.dataset.editLayer, button.dataset.track)));
  list.querySelectorAll('[data-delete-layer]').forEach(button => button.addEventListener('click', () => applyEditorOperations([{op: 'delete_layer_clip', clip_id: button.dataset.deleteLayer}], '删除附加片段')));
  list.querySelectorAll('[data-delete-track]').forEach(button => button.addEventListener('click', () => applyEditorOperations([{op: 'delete_layer_track', track_id: button.dataset.deleteTrack}], '删除附加轨道')));
  list.querySelectorAll('[data-move-track]').forEach(button => button.addEventListener('click', () => applyEditorOperations([{op: 'move_track', track_id: button.dataset.moveTrack, to_index: Number(button.dataset.index)}], '调整轨道层级')));
  list.querySelectorAll('[data-mute-track]').forEach(button => button.addEventListener('click', () => {
    const track = tracks.find(track => track.id === button.dataset.muteTrack); applyEditorOperations([{op: 'set_track_mute', track_id: track.id, muted: !track.muted}], '调整轨道显示/声音');
  }));
  for (const id of ['newAudioLayerBtn', 'newOverlayLayerBtn']) document.querySelector('#' + id).disabled = blocked || Boolean(planEditor.draft);
  const raw = planEditor.draft?.layer; if (raw) planEditor.layerSelection = {kind: raw.kind, clipId: raw.clipId, trackId: raw.trackId};
  const selection = planEditor.layerSelection; const form = document.querySelector('#layerForm'); form.hidden = !selection;
  if (!selection) return;
  const track = tracks.find(track => track.clips.some(clip => clip.id === selection.clipId)); const clip = track?.clips.find(clip => clip.id === selection.clipId);
  const missing = Boolean(selection.clipId && !clip); if (missing && !raw) {form.hidden = true; return;}
  const sources = (editor?.sources || []).filter(source => selection.kind === 'audio' ? source.has_audio : /^(video|image)/.test(source.kind));
  document.querySelector('#layerAsset').innerHTML = sources.map(source => `<option value="${esc(source.id)}">${esc(source.name)}</option>`).join('');
  const fade = clip?.composition?.audio_fade;
  const values = raw || {trackId: selection.trackId, assetId: clip?.asset_id || sources[0]?.id || '', start: clip?.range.start ?? 0,
    end: clip?.range.end ?? Math.min(2, editor?.duration || 2), sourceStart: clip?.source_start || 0,
    volume: clip?.volume ?? 1, fadeIn: fade?.in_seconds || 0, fadeOut: fade?.out_seconds || 0, role: clip?.composition?.audio_role || 'music',
    x: clip?.placement?.x || 0, y: clip?.placement?.y || 0, width: clip?.placement?.width || Math.max(1, Math.floor((editor?.resolution.width || 1080)/3)),
    height: clip?.placement?.height || Math.max(1, Math.floor((editor?.resolution.height || 1920)/3)), opacity: clip?.opacity ?? 1, fit: clip?.composition?.fit || 'contain'};
  document.querySelector('#layerFormTitle').textContent = missing ? '原片段已移除，草稿仍保留' : (selection.kind === 'audio' ? '音轨片段' : '画面叠加');
  for (const [key, id] of Object.entries(LAYER_FIELDS)) {const input = document.querySelector('#' + id); input.value = values[key] ?? '';
    input.disabled = blocked || Boolean(planEditor.draft?.body) || Boolean(planEditor.draft && !raw);}
  document.querySelector('#audioLayerFields').hidden = selection.kind !== 'audio'; document.querySelector('#overlayLayerFields').hidden = selection.kind !== 'overlay';
  document.querySelector('#saveLayerBtn').disabled = blocked || Boolean(planEditor.draft?.body) || Boolean(planEditor.draft && !raw) || missing || !sources.length;
  document.querySelector('#copyLayerAsNewBtn').hidden = !missing; document.querySelector('#copyLayerAsNewBtn').disabled = blocked;
}
function rememberLayer() {
  if (!editorMatches() || !planEditor.layerSelection || planEditor.busy || pendingOperations().length || planEditor.draft?.body || (planEditor.draft && !planEditor.draft.layer)) return;
  rememberEditorDraft({label: '音轨或叠加参数尚未保存。', layer: layerRaw()});
  document.querySelector('#editorDraftPanel').hidden = false; document.querySelector('#editorDraftLabel').textContent = planEditor.draft.label;
  document.querySelector('#reapplyEditorDraftBtn').disabled = true; updateWorkspaceControls();
  for (const id of ['editorTargetDuration', 'editorAspectRatio']) document.querySelector('#' + id).disabled = true;
  if (typeof renderClipEditor === 'function') renderClipEditor(); if (typeof renderSubtitleEditor === 'function') renderSubtitleEditor();
}
async function saveLayerForm() {
  const raw = layerRaw(); const number = (key, label) => {const value = Number(raw[key]); if (!raw[key].trim() || !Number.isFinite(value)) throw new Error('请填写有效的' + label); return value;};
  try {
    const base = {clip_id: raw.clipId, track_id: raw.trackId, asset_id: raw.assetId, start: number('start', '成片起点'), end: number('end', '成片终点'), source_start: number('sourceStart', '素材起点')};
    let action;
    if (raw.kind === 'audio') action = {op: 'save_audio_clip', ...base, volume: number('volume','音量'), fade_in: number('fadeIn','淡入时长'), fade_out: number('fadeOut','淡出时长'), role: raw.role};
    else {const width = number('width','宽度'); const height = number('height','高度'); if (!Number.isInteger(width) || !Number.isInteger(height)) throw new Error('叠加宽高需为整数像素');
      action = {op: 'save_overlay_clip', ...base, placement: {x: number('x','横向位置'), y: number('y','纵向位置'), width, height}, opacity: number('opacity','透明度'), fit: raw.fit};}
    await applyEditorOperations([action], raw.kind === 'audio' ? '保存音轨片段' : '保存画面叠加');
  } catch (error) {showToast(friendlyApiError(error));}
}
document.querySelector('#newAudioLayerBtn')?.addEventListener('click', () => openLayerEditor('audio'));
document.querySelector('#newOverlayLayerBtn')?.addEventListener('click', () => openLayerEditor('overlay'));
document.querySelector('#layerForm')?.addEventListener('submit', event => {event.preventDefault(); saveLayerForm();});
document.querySelector('#closeLayerFormBtn')?.addEventListener('click', () => {if (!planEditor.draft) {planEditor.layerSelection = null; renderLayerEditor();}});
for (const id of Object.values(LAYER_FIELDS)) document.querySelector('#' + id)?.addEventListener('input', rememberLayer);
document.querySelector('#copyLayerAsNewBtn')?.addEventListener('click', () => {
  if (planEditor.busy || pendingOperations().length || !planEditor.draft?.layer) return;
  const layer = {...planEditor.draft.layer, clipId: null}; rememberEditorDraft({label: '已转为新增片段草稿，请审阅后保存。', layer});
  planEditor.layerSelection = {kind: layer.kind, clipId: null, trackId: layer.trackId}; renderPlanEditor();
});
