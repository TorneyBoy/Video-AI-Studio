/** Shared server semantics. Unimplemented client actions are not marked ready. */
export type JsonObject = Record<string, unknown>;
export type JobStatus = 'queued' | 'running' | 'processing' | 'succeeded' | 'failed' | 'cancelled';
export interface AccountProfile {
  id: string; login_name: string | null; display_name: string;
  personal_tenant_id: string; tenant_id: string;
  memberships: Array<{tenant_id: string; role: string}>;
  linked_providers: string[];
}
export interface AccountGrant {
  user: AccountProfile; access_token: string; refresh_token: string;
  access_expires_at: string; refresh_expires_at: string; token_type: 'Bearer';
}
export interface AuthCapabilities {account_login: boolean; registration: boolean; wechat_login: boolean; wechat_reason?: string}
export interface Project {id: string; name: string; description?: string | null; status: string; updated_at: string}
export interface Asset {id: string; name: string; project_id: string | null; kind: string; uri: string; metadata: JsonObject; created_at: string}
export interface UploadSpecification {name: string; project_id: string; content_type: string; size_bytes: number; sha256: string}
export interface UploadIntent {asset_id: string; project_id: string; idempotency_key: string; specification: UploadSpecification; status: string; last_error_code: string | null; expires_at: string | null; created_at: string}
export interface UploadGrant {asset_id: string; status: 'created' | 'ready'; upload_url: string | null; upload_method: 'POST' | 'PUT' | null; upload_headers: Record<string, string>; upload_fields: Record<string, string>; upload_file_field: string; expires_at: string}
export interface StorageConfig {mode: string; max_upload_bytes: number; direct_upload_configured: boolean; upload_protocols: string[]}
export interface Job {
  id: string; project_id: string | null; type: string; status: JobStatus;
  payload: JsonObject; failure_code: string | null; retryable: boolean;
  attempt_count: number; max_attempts: number; policy_blocked: boolean;
  updated_at: string;
  created_at: string;
  failure?: {code: string; message: string; retryable: boolean; policy_blocked: boolean} | null;
}
export interface Artifact {id: string; project_id: string | null; job_id: string | null; kind: string; delivery_status?: string; job_status?: string | null; job_policy_blocked?: boolean; edit_revision_number?: number | null; metadata: JsonObject; created_at: string}
export interface ArtifactAccess {artifact_id: string; url: string; purpose: 'preview' | 'download'; expires_at: string}

export interface Template {id: string; project_id: string | null; name: string; version: string; content: JsonObject}
export interface PlanSelection {
  selection_id: string; clip_id: string; slot: string; asset_id: string; source_asset_id: string; name: string;
  source_start: number; source_end: number; window_start: number; window_end: number;
  timeline_start: number; timeline_end: number; output_duration: number; locked: boolean;
  score: number; rationale: Record<string, number>; selection_basis: 'measured_features' | 'source_order';
}
export interface EditingPlan {template_id?: string; template_version?: string; selected: PlanSelection[]; warnings?: string[]; missing_required_slots?: string[]}
export interface EditingRevision {
  document_id: string; project_id: string; origin_job_id: string; name: string;
  revision_id: string; head_revision_id: string; parent_revision_id: string | null; undo_revision_id: string | null;
  number: number; plan_hash: string; timeline_hash: string; timeline_id: string; source_asset_ids: string[];
  template: JsonObject; plan: EditingPlan; operation: JsonObject; created_at: string;
  editor?: EditorTimeline;
}
export interface SubtitleInfo {origin?: {asset_id: string; segment_id: string; transcript_hash: string; text: string; start: number; end: number}; anchor?: {parts: Array<{clip_id: string; asset_id: string; source_start: number; source_end: number}>}; partial?: boolean; edited?: boolean}
export interface EditorClip {id: string; asset_id: string | null; range: {start: number; end: number}; source_start: number; source_end: number | null; volume: number; opacity: number; text: string | null; style?: {font_size: number; font_color: string} | null; placement?: {x: number; y: number; width?: number; height?: number} | null; subtitle?: SubtitleInfo;
  transition?: {type: string; duration: number} | null; composition?: {audio_fade: {in_seconds: number; out_seconds: number} | null; fit: string; audio_role: string}}
export interface EditorTrack {id: string; type: 'video' | 'audio' | 'overlay' | 'subtitle'; muted: boolean; clips: EditorClip[]}
export interface EditorTimeline {fps: number; duration: number; resolution: {width: number; height: number}; tracks: EditorTrack[];
  sources?: Array<{id: string; name: string; kind: string; has_audio: boolean}>;
  advanced_support?: {supported: boolean; reason: string; required_features: string[]; missing_features: string[]}}
export type EditingAction = {op: 'set_lock'; selection_id: string; locked: boolean}
  | {op: 'replace'; selection_id: string; candidate_id: string}
  | {op: 'replan'; selection_ids?: string[]}
  | {op: 'restore'; revision_id: string} | {op: 'undo'} | {op: 'refresh_candidates'}
  | {op: 'configure'; target_duration: number | null; aspect_ratio: '9:16' | '16:9' | '1:1'}
  | {op: 'trim_clip'; clip_id: string; source_start: number; source_end: number}
  | {op: 'split_clip'; clip_id: string; offset_seconds: number}
  | {op: 'move_clip'; clip_id: string; to_index: number}
  | {op: 'remove_clip'; clip_id: string}
  | {op: 'set_clip_audio'; clip_id: string; volume: number}
  | {op: 'import_transcript'; track_id?: string; asset_ids?: string[]; refresh_sources?: boolean}
  | {op: 'save_subtitle'; cue_id: string | null; track_id: string; text: string; start: number; end: number;
     anchor_mode: 'keep' | 'absolute' | 'clip'; anchor_clip_id: string | null; style: {font_size: number; font_color: string}; position: {x: number; y: number}}
  | {op: 'delete_subtitle'; cue_id: string}
  | {op: 'save_audio_clip'; clip_id: string | null; track_id: string; asset_id: string; start: number; end: number; source_start: number; volume: number; fade_in: number; fade_out: number; role: 'voice' | 'music' | 'other'}
  | {op: 'save_overlay_clip'; clip_id: string | null; track_id: string; asset_id: string; start: number; end: number; source_start: number; placement: {x: number; y: number; width: number; height: number}; opacity: number; fit: 'contain' | 'cover' | 'stretch'}
  | {op: 'delete_layer_clip'; clip_id: string} | {op: 'delete_layer_track'; track_id: string}
  | {op: 'set_track_mute'; track_id: string; muted: boolean} | {op: 'move_track'; track_id: string; to_index: number}
  | {op: 'set_transition'; clip_id: string; type: 'none' | 'fade' | 'dissolve' | 'wipeleft' | 'wiperight' | 'slideleft' | 'slideright'; duration: number}
  | {op: 'set_video_fit'; clip_id: string; mode: 'contain' | 'cover' | 'stretch'};
export interface SubtitleExport {revision_id: string; timeline_hash: string; format: 'srt' | 'vtt'; filename: string; content: string; sha256: string; cue_count: number; warnings: string[]}
export interface OriginalTranscript {asset_id: string; name: string; transcript_hash: string; language: string | null; id: string; start: number; end: number; text: string}
export interface EditingApplyRequest {base_revision_id: string; operations: EditingAction[]; source_asset_ids?: string[]; note?: string}
export interface EditingReceipt {found: boolean; revision: EditingRevision | null}
export interface ShotCandidate {
  candidate_id: string; source_asset_id: string; name: string; kind: string;
  source_start: number; source_end: number; duration: number; orientation: string | null;
  score: number; rationale: Record<string, number>; selection_basis: 'measured_features' | 'source_order'; eligible_slots: string[];
}
export interface PageResult<T> {items: T[]; total: number; skip: number; limit: number}
export interface RenderEnqueueResult {job: Job; manifest: JsonObject}

export function object(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};
}

export class ClientError extends Error {
  constructor(public code: string, message: string, public status = 0, public outcomeUnknown = false) {super(message);}
}

export function serverError(data: unknown, status: number): ClientError {
  const detail = object(data).detail ?? data;
  if (Array.isArray(detail)) {
    const message = detail.map(row => String(object(row).msg || '')).filter(Boolean).join('；');
    return new ClientError('validation_error', message || '请检查填写内容', status);
  }
  const fields = object(detail);
  const code = String(fields.code || 'request_failed');
  const messages: Record<string, string> = {
    artifact_archive_pending: '视频正在归档，请稍后刷新', artifact_quality_pending: '视频尚未通过质检',
    artifact_job_not_deliverable: '任务尚未达到交付条件', artifact_retention_expired: '该成片已超过保留期限',
    artifact_delivery_forbidden: '该成片当前不可领取', budget_exceeded: '预算不足，请先查看费用与额度',
    budget_cost_unknown: '费用待对账，请稍后查看', session_expired: '登录已失效，请重新登录',
    session_revoked: '登录已失效，请重新登录', registration_disabled: '当前暂未开放注册',
    asset_labels_conflict: '素材已在其他页面修改，请重新打开整理面板后审阅',
    object_not_uploaded: '尚未找到已上传文件，请重新选择原文件继续',
    object_hash_mismatch: '文件指纹不一致，请重新选择原文件',
    object_size_mismatch: '文件大小不一致，请重新选择原文件',
    upload_expired: '上传授权已过期，可以续签后继续',
  };
  return new ClientError(code, messages[code] ||
    (typeof detail === 'string' ? detail : String(fields.message || (status === 401 ? '请重新登录' : '暂时无法完成，请稍后重试'))), status);
}
