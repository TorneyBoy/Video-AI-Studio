import {ClientError, EditingAction, EditingRevision, EditorClip, object} from './contracts';

export interface LayerForm {
  kind: 'audio' | 'overlay'; clipId: string | null; trackId: string; assetId: string; start: string; end: string; sourceStart: string;
  volume: string; fadeIn: string; fadeOut: string; role: 'voice' | 'music' | 'other';
  x: string; y: string; width: string; height: string; opacity: string; fit: 'contain' | 'cover' | 'stretch';
}
export function layerForm(revision: EditingRevision | null, kind: LayerForm['kind'] = 'audio', clip?: EditorClip, trackId?: string, saved?: unknown): LayerForm {
  const editor = revision?.editor; const source = editor?.sources?.find(source => kind === 'audio' ? source.has_audio : /^(image|video)/.test(source.kind));
  const raw = object(saved); const fade = clip?.composition?.audio_fade;
  const form: LayerForm = {kind, clipId: clip?.id || null, trackId: trackId || (kind === 'audio' ? '配乐' : '叠加'), assetId: clip?.asset_id || source?.id || '',
    start: String(clip?.range.start ?? 0), end: String(clip?.range.end ?? Math.min(editor?.duration || 2, 2)), sourceStart: String(clip?.source_start ?? 0),
    volume: String(clip?.volume ?? 1), fadeIn: String(fade?.in_seconds || 0), fadeOut: String(fade?.out_seconds || 0), role: (clip?.composition?.audio_role || 'music') as LayerForm['role'],
    x: String(clip?.placement?.x || 0), y: String(clip?.placement?.y || 0), width: String(clip?.placement?.width || Math.max(1, Math.floor((editor?.resolution.width || 1080) / 3))),
    height: String(clip?.placement?.height || Math.max(1, Math.floor((editor?.resolution.height || 1920) / 3))), opacity: String(clip?.opacity ?? 1), fit: (clip?.composition?.fit || 'contain') as LayerForm['fit']};
  for (const key of ['trackId', 'assetId', 'start', 'end', 'sourceStart', 'volume', 'fadeIn', 'fadeOut', 'x', 'y', 'width', 'height', 'opacity'] as const) if (typeof raw[key] === 'string') form[key] = raw[key] as string;
  if (raw.clipId === null || typeof raw.clipId === 'string') form.clipId = raw.clipId;
  if (['audio', 'overlay'].includes(String(raw.kind))) form.kind = raw.kind as LayerForm['kind'];
  if (['voice', 'music', 'other'].includes(String(raw.role))) form.role = raw.role as LayerForm['role'];
  if (['contain', 'cover', 'stretch'].includes(String(raw.fit))) form.fit = raw.fit as LayerForm['fit'];
  return form;
}
export function layerCommand(form: LayerForm): EditingAction {
  const number = (raw: string, label: string) => {const value = Number(raw); if (!raw.trim() || !Number.isFinite(value)) throw new ClientError('layer_input_invalid', '请填写有效的' + label); return value;};
  const base = {clip_id: form.clipId, track_id: form.trackId, asset_id: form.assetId, start: number(form.start, '成片起点'), end: number(form.end, '成片终点'), source_start: number(form.sourceStart, '素材起点')};
  if (form.kind === 'audio') return {op: 'save_audio_clip', ...base, volume: number(form.volume, '音量'), fade_in: number(form.fadeIn, '淡入时长'), fade_out: number(form.fadeOut, '淡出时长'), role: form.role};
  const width = number(form.width, '宽度'); const height = number(form.height, '高度');
  if (!Number.isInteger(width) || !Number.isInteger(height)) throw new ClientError('layer_input_invalid', '叠加宽高需为整数像素');
  return {op: 'save_overlay_clip', ...base, placement: {x: number(form.x, '横向位置'), y: number(form.y, '纵向位置'), width, height}, opacity: number(form.opacity, '透明度'), fit: form.fit};
}
