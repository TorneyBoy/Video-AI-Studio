import {hasPendingWave} from './pending-wave';
/** Persist only first-party operation metadata and editor inputs, never grants,
 * media URLs or server source snapshots. Unknown writes keep their original key. */
import {sha256} from '../vendor/sha256';
import {personalSession} from './api';
import {ClientError, EditingReceipt, Job, object} from './contracts';

export interface StudioOperation {
  context: string; kind: 'plan' | 'edit' | 'render'; key: string; body: Record<string, unknown>;
  label: string; createdAt: number; documentId?: string; revisionId?: string; jobId?: string;
}
const PREFIX = 'video-ai-studio.operation.';
const CACHE = 'video-ai-studio.studio.';
const inFlight = new Set<string>();

export function studioContext(): string {
  const profile = personalSession.profile; const project = personalSession.activeProject();
  return profile && project ? `${profile.id}:${profile.tenant_id}:${project}` : '';
}
export function guardStudio(context: string): void {
  if (!context || context !== studioContext()) throw new ClientError('workspace_changed', '账号或项目已变化，请回到原项目继续');
}
export function studioHash(body: unknown): string {return sha256(JSON.stringify(body));}
export function readStudioCache(): Record<string, unknown> {return object(wx.getStorageSync(CACHE + studioContext()));}
export function writeStudioCache(change: Record<string, unknown>): void {
  const context = studioContext(); guardStudio(context);
  wx.setStorageSync(CACHE + context, {...readStudioCache(), ...change});
}
function operationKey(row: StudioOperation): string {return PREFIX + row.context + '.' + row.key;}
function endpoint(row: StudioOperation): string {
  if (row.kind === 'plan') return '/jobs/auto-edit';
  if (row.kind === 'edit') return `/editing/${encodeURIComponent(row.documentId || '')}/revisions`;
  return `/editing/${encodeURIComponent(row.documentId || '')}/revisions/${encodeURIComponent(row.revisionId || '')}/render`;
}
export function pendingStudioOperations(): StudioOperation[] {
  const context = studioContext(); if (!context) return [];
  try {
    return wx.getStorageInfoSync().keys.filter(key => key.startsWith(PREFIX + context + '.'))
      .map(key => object(wx.getStorageSync(key))).filter(row => row.context === context &&
        ['plan', 'edit', 'render'].includes(String(row.kind)) && typeof row.key === 'string' && row.body && typeof row.body === 'object')
      .map(row => row as unknown as StudioOperation).sort((a, b) => a.createdAt - b.createdAt);
  } catch (_) {return [];}
}
export function operation(kind: StudioOperation['kind'], key: string, body: Record<string, unknown>, label: string,
                           binding: Partial<StudioOperation> = {}): StudioOperation {
  const context = studioContext(); guardStudio(context);
  return {...binding, context, kind, key, body, label, createdAt: Date.now()};
}
export function confirmStudioOperation(row: StudioOperation): void {wx.removeStorageSync(operationKey(row));}
export async function sendStudioOperation<T>(row: StudioOperation, repeat = false): Promise<T> {
  guardStudio(row.context);
  if (!repeat && hasPendingWave(row.context)) throw new ClientError('batch_run_unconfirmed','请先在批次生成页面核对原提交');
  if (inFlight.has(row.context)) throw new ClientError('operation_busy', '请等待当前操作完成');
  if (!repeat && pendingStudioOperations().length) throw new ClientError('operation_unconfirmed', '请先核对上次提交');
  try {wx.setStorageSync(operationKey(row), row);}
  catch (_) {throw new ClientError('operation_cache_unavailable', '无法保存操作记录，请检查小程序存储后再提交');}
  inFlight.add(row.context); let result: T;
  try {result = await personalSession.request<T>(endpoint(row), 'POST', row.body, row.key);}
  catch (error) {
    if (!repeat && error instanceof ClientError && error.status >= 400 && error.status < 500) confirmStudioOperation(row);
    throw error;
  } finally {inFlight.delete(row.context);}
  confirmStudioOperation(row); guardStudio(row.context); return result;
}
export async function checkStudioOperation(row: StudioOperation): Promise<{found: boolean; result?: unknown}> {
  guardStudio(row.context);
  if (row.kind === 'edit') {
    const receipt = await personalSession.request<EditingReceipt>(`/editing/${encodeURIComponent(row.documentId || '')}/requests/${encodeURIComponent(row.key)}`);
    guardStudio(row.context); return {found: receipt.found, result: receipt.revision};
  }
  const rows = await personalSession.request<Job[]>(`/jobs?project_id=${encodeURIComponent(personalSession.activeProject())}&idempotency_key=${encodeURIComponent(row.key)}&limit=1`);
  guardStudio(row.context); return {found: rows.length > 0, result: rows[0]};
}
export function editorDraft(documentId: string): Record<string, unknown> {
  return object(wx.getStorageSync(CACHE + studioContext() + '.draft.' + documentId));
}
export function saveEditorDraft(documentId: string, draft: Record<string, unknown>): void {
  const context = studioContext(); guardStudio(context);
  wx.setStorageSync(CACHE + context + '.draft.' + documentId, draft);
}
export function removeEditorDraft(documentId: string): void {wx.removeStorageSync(CACHE + studioContext() + '.draft.' + documentId);}
