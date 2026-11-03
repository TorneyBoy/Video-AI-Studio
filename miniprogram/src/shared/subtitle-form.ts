import {ClientError, EditingAction, EditingRevision, EditorClip, object} from './contracts';

export interface SubtitleForm {
  cueId: string | null; trackId: string; text: string; start: string; end: string; fontSize: string; color: string;
  x: string; y: string; anchorMode: 'keep' | 'absolute' | 'clip'; anchorClipId: string;
}
export function blankSubtitle(): SubtitleForm {
  return {cueId: null, trackId: 'subtitles', text: '', start: '0', end: '2', fontSize: '48', color: 'white', x: '54', y: '1632', anchorMode: 'absolute', anchorClipId: ''};
}
export function subtitleForm(revision: EditingRevision, clip?: EditorClip, trackId = 'subtitles', saved?: unknown): SubtitleForm {
  const editor = revision.editor; const raw = object(saved);
  const form: SubtitleForm = {cueId: clip?.id || null, trackId, text: clip?.text || '', start: String(clip?.range.start ?? 0),
    end: String(clip?.range.end ?? Math.min(2, editor?.duration || 2)), fontSize: String(clip?.style?.font_size || 48), color: clip?.style?.font_color || 'white',
    x: String(clip?.placement?.x ?? Math.round((editor?.resolution.width || 1080) * 0.05)), y: String(clip?.placement?.y ?? Math.round((editor?.resolution.height || 1920) * 0.85)),
    anchorMode: clip?.subtitle?.anchor ? 'keep' : 'absolute', anchorClipId: clip?.subtitle?.anchor?.parts[0]?.clip_id || editor?.tracks.find(track => track.type === 'video')?.clips[0]?.id || ''};
  for (const key of ['trackId', 'text', 'start', 'end', 'fontSize', 'color', 'x', 'y', 'anchorClipId'] as const) if (typeof raw[key] === 'string') form[key] = raw[key] as string;
  if (raw.cueId === null || typeof raw.cueId === 'string') form.cueId = raw.cueId;
  if (['keep', 'absolute', 'clip'].includes(String(raw.anchorMode))) form.anchorMode = raw.anchorMode as SubtitleForm['anchorMode'];
  return form;
}
export function subtitleCommand(form: SubtitleForm): EditingAction {
  const number = (raw: string, label: string) => {const value = Number(raw); if (!raw.trim() || !Number.isFinite(value)) throw new ClientError('subtitle_input_invalid', '请填写有效的' + label); return value;};
  const font = number(form.fontSize, '字号'); if (!Number.isInteger(font)) throw new ClientError('subtitle_input_invalid', '字号需为整数');
  return {op: 'save_subtitle', cue_id: form.cueId, track_id: form.trackId, text: form.text,
    start: number(form.start, '起点'), end: number(form.end, '终点'), anchor_mode: form.anchorMode,
    anchor_clip_id: form.anchorMode === 'clip' ? form.anchorClipId : null, style: {font_size: font, font_color: form.color},
    position: {x: number(form.x, '横向位置'), y: number(form.y, '纵向位置')}};
}
