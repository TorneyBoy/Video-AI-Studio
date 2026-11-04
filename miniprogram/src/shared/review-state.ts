/** Raw review drafts and original receipts only; never persist access URLs. */
import {personalSession} from './api';
import {ClientError, object} from './contracts';
import {sha256} from '../vendor/sha256';

export type ReviewDecision = 'approved' | 'changes_requested';
export interface ReviewIssue {category: 'picture' | 'audio' | 'subtitle' | 'content' | 'other'; severity: 'error' | 'warning' | 'info'; message: string; start: number | null; end: number | null}
export interface ReviewRecord {id: string; number: number; artifact_id: string; binding_hash: string; actor_id: string; decision: ReviewDecision; note: string; issues: ReviewIssue[]; created_at: string}
export interface ReviewReport {
  artifact_id: string; job_id: string | null; project_id: string | null; binding_hash: string;
  binding: Record<string, unknown>; status: 'pending' | ReviewDecision | 'stale'; latest: ReviewRecord | null;
  can_review: boolean; can_preview: boolean; can_download: boolean; quality_ready: boolean; quality_scope: string; reason: string;
  duration: number | null; measurements: Record<string, unknown>; revision_number: number | null; origin_job_id: string | null;
}
export interface ReviewIssueForm {category: ReviewIssue['category']; severity: ReviewIssue['severity']; message: string; start: string; end: string}
export interface ReviewForm {note: string; issues: ReviewIssueForm[]}
export interface ReviewBody {binding_hash: string; base_review_id: string | null; decision: ReviewDecision; note: string; issues: ReviewIssue[]}
export interface ReviewPending {context: string; artifactId: string; key: string; body: ReviewBody}
export interface ReviewResult {review: ReviewRecord; current: ReviewReport}
const PREFIX = 'video-ai-studio.review.v1.';
export const reviewStatus = (status: string): string => ({pending: '待审核', approved: '已通过', changes_requested: '已退回', stale: '内容或证据已变化，需重新审核'}[status] || '待核对');
export const blankReviewIssue = (): ReviewIssueForm => ({category: 'content', severity: 'error', message: '', start: '', end: ''});
export function reviewContext(artifactId: string): string {return `${personalSession.profile?.id || ''}:${personalSession.profile?.tenant_id || ''}:${artifactId}`;}
function guard(context: string, artifactId: string): void {if (!personalSession.profile || context !== reviewContext(artifactId)) throw new ClientError('review_context_changed', '账号或作品已变化，请重新打开审核页');}
function cacheKey(artifactId: string): string {return PREFIX + reviewContext(artifactId);}
export function readReviewState(artifactId: string): {form: ReviewForm; pending: ReviewPending | null} {
  const saved = object(wx.getStorageSync(cacheKey(artifactId))); const raw = object(saved.form);
  return {form: {note: typeof raw.note === 'string' ? raw.note : '', issues: Array.isArray(raw.issues) ? raw.issues as ReviewIssueForm[] : []}, pending: saved.pending ? saved.pending as ReviewPending : null};
}
export function saveReviewState(artifactId: string, form: ReviewForm, pending: ReviewPending | null = null): void {
  guard(reviewContext(artifactId), artifactId); wx.setStorageSync(cacheKey(artifactId), {form, pending});
}
export function clearReviewState(artifactId: string): void {wx.removeStorageSync(cacheKey(artifactId));}
export function reviewBody(report: ReviewReport, form: ReviewForm, decision: ReviewDecision): ReviewBody {
  const time = (raw: string): number | null => {if (!raw.trim()) return null; const result = Number(raw); if (!Number.isFinite(result) || result < 0) throw new ClientError('review_input_invalid', '问题时间需为非负秒数'); return result;};
  const issues = form.issues.map(row => {if (!row.message.trim()) throw new ClientError('review_input_invalid', '请填写每条问题说明，或移除空条目');
    return {category: row.category, severity: row.severity, message: row.message, start: time(row.start), end: time(row.end)};});
  if (decision === 'changes_requested' && !form.note.trim() && !issues.length) throw new ClientError('review_input_invalid', '退回时请填写意见或添加问题');
  if (decision === 'approved' && issues.some(row => row.severity === 'error')) throw new ClientError('review_input_invalid', '仍有错误级问题，请退回修改或先移除已解决的问题');
  return {binding_hash: report.binding_hash, base_review_id: report.latest?.id || null, decision, note: form.note, issues};
}
export function reviewOperation(artifactId: string, body: ReviewBody): ReviewPending {
  return {artifactId, body, context: reviewContext(artifactId), key: 'review:' + sha256(JSON.stringify(body))};
}
export async function sendReview(row: ReviewPending, form: ReviewForm): Promise<ReviewResult> {
  guard(row.context, row.artifactId);
  const pending = readReviewState(row.artifactId).pending;
  if (pending && (pending.key !== row.key || JSON.stringify(pending.body) !== JSON.stringify(row.body))) throw new ClientError('review_unconfirmed', '请先核对原审核提交');
  saveReviewState(row.artifactId, form, row);
  try {
    const result = await personalSession.request<ReviewResult>(`/artifacts/${encodeURIComponent(row.artifactId)}/reviews`, 'POST', row.body, row.key);
    guard(row.context, row.artifactId); clearReviewState(row.artifactId); return result;
  } catch (error) {
    if (row.context === reviewContext(row.artifactId) && error instanceof ClientError && error.status >= 400 && error.status < 500) saveReviewState(row.artifactId, form);
    throw error;
  }
}
export async function checkReview(row: ReviewPending): Promise<{found: boolean; review: ReviewRecord | null; current: ReviewReport}> {
  guard(row.context, row.artifactId);
  const result = await personalSession.request<{found: boolean; review: ReviewRecord | null; current: ReviewReport}>(`/artifacts/${encodeURIComponent(row.artifactId)}/review-requests/${encodeURIComponent(row.key)}`);
  guard(row.context, row.artifactId); if (result.found) clearReviewState(row.artifactId); return result;
}

// Scenario evidence remains separate from the explicit release approval above.
import * as ScenarioForms from '../vendor/scenario-review-form';
export {ScenarioForms};
export interface QualityPending {context:string;projectId:string;artifactId:string;key:string;body:ScenarioForms.Body}
export interface QualityState {form:ScenarioForms.Form|null;pending:QualityPending|null}
const QUALITY_PREFIX='video-ai-studio.scenario-review.v1.';
const qualityFlight=new Set<string>();
export function qualityContext(projectId:string,artifactId:string):string {return `${personalSession.profile?.id||''}:${personalSession.profile?.tenant_id||''}:${projectId}:${artifactId}`;}
function guardQuality(row:{context:string;projectId:string;artifactId:string},current?:()=>boolean):void {if(!personalSession.profile||personalSession.activeProject()!==row.projectId||row.context!==qualityContext(row.projectId,row.artifactId)||current&& !current())throw new ClientError('quality_context_changed','账号、项目或成片已变化，请回到原作品核对');}
export function qualityBase(projectId:string):string{return '/projects/'+encodeURIComponent(projectId)+'/quality';}
export function readQualityState(projectId:string,artifactId:string):QualityState {const context=qualityContext(projectId,artifactId),saved=object(wx.getStorageSync(QUALITY_PREFIX+context)),pending=saved.pending as QualityPending|undefined;return {form:saved.form as ScenarioForms.Form||null,pending:pending?.context===context&&pending.projectId===projectId&&pending.artifactId===artifactId&&pending.body?.artifact_id===artifactId?pending:null};}
export function saveQualityState(projectId:string,artifactId:string,form:ScenarioForms.Form|null,pending:QualityPending|null=null):void {const context=qualityContext(projectId,artifactId);guardQuality({context,projectId,artifactId});wx.setStorageSync(QUALITY_PREFIX+context,{form,pending});}
export function qualityOperation(projectId:string,body:ScenarioForms.Body):QualityPending {return {projectId,artifactId:body.artifact_id,context:qualityContext(projectId,body.artifact_id),body,key:'quality:'+Date.now()+'-'+Math.random().toString(36).slice(2)};}
export async function sendQualityReview(row:QualityPending,form:ScenarioForms.Form|null,repeat=false,current?:()=>boolean):Promise<ScenarioForms.Record> {
  guardQuality(row,current);if(qualityFlight.has(row.context))throw new ClientError('quality_busy','请等待当前评审完成');const pending=readQualityState(row.projectId,row.artifactId).pending;
  if(pending&&(!repeat||pending.key!==row.key||JSON.stringify(pending.body)!==JSON.stringify(row.body)))throw new ClientError('quality_unconfirmed','请先核对原场景评审提交');
  saveQualityState(row.projectId,row.artifactId,form,row);qualityFlight.add(row.context);
  try{const result=await personalSession.request<ScenarioForms.Record>(qualityBase(row.projectId)+'/reviews','POST',row.body,row.key);guardQuality(row,current);saveQualityState(row.projectId,row.artifactId,form,null);return result;}
  catch(error){if(!repeat&&row.context===qualityContext(row.projectId,row.artifactId)&&personalSession.activeProject()===row.projectId&&(!current||current())&&error instanceof ClientError&&error.status>=400&&error.status<500)saveQualityState(row.projectId,row.artifactId,form,null);throw error;}
  finally{qualityFlight.delete(row.context);}
}
export async function checkQualityReview(row:QualityPending,current?:()=>boolean):Promise<{found:boolean;result:ScenarioForms.Record|null}> {guardQuality(row,current);const result=await personalSession.request<{found:boolean;result:ScenarioForms.Record|null}>(qualityBase(row.projectId)+'/reviews/requests/'+encodeURIComponent(row.key));guardQuality(row,current);if(result.found&&result.result){const saved=readQualityState(row.projectId,row.artifactId);saveQualityState(row.projectId,row.artifactId,saved.form,null);}return result;}
