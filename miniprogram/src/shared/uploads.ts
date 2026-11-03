import {message, personalSession} from './api';
import {Asset, ClientError, object, StorageConfig, UploadGrant, UploadIntent, UploadSpecification} from './contracts';
import {fileFingerprint} from './file-hash';

export interface LocalMedia {path: string; name: string; size: number; contentType: string}
export interface UploadRow {
  id: string; user: string; tenant: string; project: string; name: string; stage: string; label: string;
  percent: number | null; busy: boolean; error: string; assetId?: string; key?: string;
  specification?: UploadSpecification; analyze: boolean;
}
type Owner = {user: string; tenant: string; project: string};
const records = new Map<string, UploadRow>();
const listeners = new Set<() => void>();
let sequence = 0;

function owner(): Owner {
  const profile = personalSession.profile; const project = personalSession.activeProject();
  if (!profile || !project) throw new ClientError('workspace_required', '请先登录并选择项目');
  return {user: profile.id, tenant: profile.tenant_id, project};
}
function belongs(row: Owner, current: Owner): boolean {return row.user === current.user && row.tenant === current.tenant && row.project === current.project;}
function guard(row: Owner): void {if (!belongs(row, owner())) throw new ClientError('workspace_changed', '账号或项目已变化，原上传可回到对应项目恢复');}
function notify(): void {listeners.forEach(listener => listener());}
function update(row: UploadRow, change: Partial<UploadRow>): void {Object.assign(row, change); notify();}
async function request<T>(row: Owner, path: string, method: 'GET' | 'POST' = 'GET', data?: unknown, key?: string): Promise<T> {
  guard(row); const result = await personalSession.request<T>(path, method, data, key); guard(row); return result;
}
export function subscribeUploads(listener: () => void): () => void {listeners.add(listener); return () => {listeners.delete(listener);};}
export function currentUploads(): UploadRow[] {
  try {const current = owner(); return [...records.values()].filter(row => belongs(row, current)).map(row => ({...row}));}
  catch (_) {return [];}
}
export function contentType(name: string, kind?: string): string {
  const extension = name.split('.').pop()?.toLowerCase() || '';
  const types: Record<string, string> = {mp4: 'video/mp4', mov: 'video/quicktime', m4v: 'video/mp4', webm: 'video/webm',
    mkv: 'video/x-matroska', avi: 'video/x-msvideo', jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
    webp: 'image/webp', gif: 'image/gif', heic: 'image/heic', mp3: 'audio/mpeg', wav: 'audio/wav',
    m4a: 'audio/mp4', aac: 'audio/aac', flac: 'audio/flac', ogg: 'audio/ogg'};
  return types[extension] || (kind === 'video' ? 'video/mp4' : kind === 'image' ? 'image/jpeg' : 'application/octet-stream');
}
export async function recoverUploads(): Promise<void> {
  const current = owner();
  const rows = await request<UploadIntent[]>(current, '/assets/upload-intents?project_id=' + encodeURIComponent(current.project));
  for (const item of rows) {
    if ([...records.values()].some(row => belongs(row, current) && row.assetId === item.asset_id)) continue;
    records.set(item.asset_id, {...current, id: item.asset_id, name: item.specification.name, stage: 'waiting',
      label: '上次上传未完成，可先核验或重新选择原文件', percent: null, busy: false, error: '',
      assetId: item.asset_id, key: item.idempotency_key, specification: item.specification, analyze: false});
  }
  notify();
}
async function find(row: Owner, hash: string): Promise<UploadIntent | undefined> {
  return (await request<UploadIntent[]>(row, `/assets/upload-intents?project_id=${encodeURIComponent(row.project)}&sha256=${hash}&include_completed=true&limit=1`))[0];
}
async function grant(row: UploadRow): Promise<UploadGrant> {
  const result = await request<UploadGrant>(row, '/assets/upload-intents', 'POST', row.specification, row.key);
  row.assetId = result.asset_id;
  // One UI record per durable intent, including one rediscovered on another tab.
  for (const [id, other] of records) if (id !== row.id && belongs(other, row) && other.assetId === row.assetId && !other.busy) records.delete(id);
  return result;
}
async function verify(row: UploadRow): Promise<Asset> {
  try {return await request<Asset>(row, `/assets/${encodeURIComponent(row.assetId!)}/verify-storage-upload`, 'POST');}
  catch (error) {
    if (!(error instanceof ClientError) || !['upload_expired', 'upload_session_expired'].includes(error.code)) throw error;
    await grant(row);
    return request<Asset>(row, `/assets/${encodeURIComponent(row.assetId!)}/verify-storage-upload`, 'POST');
  }
}
function upload(row: UploadRow, file: LocalMedia, authorization: UploadGrant): Promise<void> {
  guard(row);
  if (authorization.upload_method !== 'POST' || !/^https:\/\/[^/?#@]+\//.test(authorization.upload_url || ''))
    throw new ClientError('upload_protocol_unsupported', '当前上传服务不支持小程序直传，请联系管理员配置 POST 上传');
  // Signed URLs, fields and cloud credentials stay in this call's memory only.
  // Platform bearer/session tokens are never sent to the storage host.
  return new Promise<void>((resolve, reject) => {
    const task = wx.uploadFile({url: authorization.upload_url!, filePath: file.path,
      name: authorization.upload_file_field || 'file', header: authorization.upload_headers,
      formData: authorization.upload_fields, timeout: 10 * 60 * 1000,
      success: result => result.statusCode >= 200 && result.statusCode < 300 || result.statusCode === 409
        ? resolve() : reject(new ClientError('upload_failed', `上传未完成（${result.statusCode}），请先核验或重选原文件`)),
      fail: () => reject(new ClientError('upload_unknown', '上传结果尚未确认，请先核验已有上传', 0, true))});
    task.onProgressUpdate(event => update(row, {percent: event.progress}));
  });
}
async function analyze(row: UploadRow, asset: Asset): Promise<void> {
  if (!row.analyze || !asset.kind.startsWith('video') || object(asset.metadata.analysis).completeness === 'complete') return;
  row.label = '已上传；选中素材后查看分析报价并确认';
}
export async function verifyPending(id: string): Promise<void> {
  const row = records.get(id); if (!row || row.busy || !row.assetId) return;
  guard(row); update(row, {busy: true, error: '', label: '正在核验已有上传', percent: null});
  try {
    const asset = await verify(row); update(row, {stage: 'ready', label: '已上传并核验'});
    await analyze(row, asset);
  } catch (error) {update(row, {stage: 'waiting', label: '上传尚未完成', error: message(error)}); throw error;}
  finally {update(row, {busy: false});}
}
export async function uploadMedia(file: LocalMedia, analyzeVideo: boolean, expectedId?: string): Promise<string> {
  const current = owner();
  const row = expectedId ? records.get(expectedId) : {...current, id: `local-${Date.now()}-${++sequence}`,
    name: file.name, stage: 'hashing', label: '计算文件指纹', percent: 0, busy: false, error: '', analyze: analyzeVideo} as UploadRow;
  if (!row || row.busy) throw new ClientError('upload_unavailable', '此上传记录当前不可操作');
  guard(row); records.set(row.id, row); update(row, {busy: true, error: '', label: '计算文件指纹', stage: 'hashing', analyze: analyzeVideo});
  try {
    const config = await request<StorageConfig>(row, '/storage/config');
    if (!config.direct_upload_configured) throw new ClientError('storage_unavailable', '上传服务尚未就绪');
    if (file.size <= 0 || file.size > config.max_upload_bytes) throw new ClientError('file_size_invalid', '文件为空或超过本项目上传限制');
    if (!/^(video|image|audio)\//.test(file.contentType)) throw new ClientError('file_type_invalid', '请选择视频、图片或音频文件');
    const hash = await fileFingerprint(file.path, file.size, percent => update(row, {percent}));
    guard(row);
    if (row.specification && (row.specification.sha256 !== hash || row.specification.size_bytes !== file.size))
      throw new ClientError('original_file_required', '文件与原上传不一致，请重新选择原文件');
    const existing = row.specification ? undefined : await find(row, hash);
    row.key ||= existing?.idempotency_key || `upload:${row.project}:${hash}`;
    row.specification ||= existing?.specification || {name: file.name, content_type: file.contentType, project_id: row.project, size_bytes: file.size, sha256: hash};
    row.assetId ||= existing?.asset_id;
    update(row, {label: '准备上传授权', percent: null});
    let authorization: UploadGrant;
    try {authorization = await grant(row);}
    catch (error) {
      if (!(error instanceof ClientError) || error.code !== 'idempotency_input_conflict') throw error;
      const concurrent = await find(row, hash); if (!concurrent) throw error;
      row.key = concurrent.idempotency_key; row.specification = concurrent.specification;
      authorization = await grant(row);
    }
    let asset: Asset | undefined;
    if (authorization.status === 'ready') asset = await request<Asset>(row, '/assets/' + encodeURIComponent(row.assetId!));
    else {
      try {asset = await verify(row);}
      catch (error) {if (!(error instanceof ClientError) || error.code !== 'object_not_uploaded') throw error;}
      if (!asset) {
        if (Date.parse(authorization.expires_at) <= Date.now() + 10000) authorization = await grant(row);
        update(row, {stage: 'uploading', label: '正在上传', percent: 0});
        await upload(row, file, authorization);
        update(row, {stage: 'verifying', label: '正在核验文件', percent: null});
        asset = await verify(row);
      }
    }
    update(row, {stage: 'ready', label: '已上传并核验', percent: null});
    await analyze(row, asset);
    return asset.id;
  } catch (error) {update(row, {stage: 'waiting', percent: null, label: '可恢复上传', error: message(error)}); throw error;}
  finally {update(row, {busy: false});}
}
