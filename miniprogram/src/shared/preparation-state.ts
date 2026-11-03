import {hasPendingWave} from './pending-wave';
/** Only first-party settings/IDs in storage; source/result snapshots stay on server. */
import {personalSession} from './api';
import {ClientError,object} from './contracts';
import {guardStudio,studioContext,studioHash} from './studio-state';
export interface PreparationDraft {context:string;kind:'plan'|'analysis';body:Record<string,unknown>;hash:string}
export interface PreparationItem {asset_id:string;name:string;kind:string;disposition:string;job_id:string|null;job_status:string|null;problem:string|null;amount_micros:string|null;currency:string|null;price_version:string|null}
export interface PreparationQuote {id:string;kind:string;project_id:string|null;expires_at:string;status:string;can_submit:boolean;reasons:string[];note:string;items:PreparationItem[];totals:Array<{currency:string;amount_micros:string}>}
export interface PreparationIntent {context:string;quoteId:string;key:string}
export interface PreparationReceipt {found:boolean;result:{job_id:string|null;items:Array<{asset_id:string;job_id:string|null;disposition:string}>}|null}
const DRAFT='video-ai-studio.preparation-draft.v1.',PENDING='video-ai-studio.preparation.v1.';
export function savePreparation(kind:'plan'|'analysis',input:Record<string,unknown>):void{const context=studioContext();guardStudio(context);const keys=kind==='plan'?['project_id','template_id','asset_ids','target_duration','aspect_ratio','quality','max_attempts']:['asset_ids','shot_analysis','transcribe','whisper_model','timeout_seconds','max_attempts','force_new'];const body:Record<string,unknown>={};for(const key of keys)if(input[key]!==undefined)body[key]=input[key];body.asset_ids=[...new Set((Array.isArray(body.asset_ids)?body.asset_ids:[]).filter((id):id is string=>typeof id==='string'))].sort();wx.setStorageSync(DRAFT+context,{context,kind,body,hash:studioHash(body)});}
export function preparationDraft():PreparationDraft|null{const row=object(wx.getStorageSync(DRAFT+studioContext()));return row.context===studioContext()&&['plan','analysis'].includes(String(row.kind))&&row.hash===studioHash(row.body)?row as unknown as PreparationDraft:null;}
export function pendingPreparation():PreparationIntent|null{const row=object(wx.getStorageSync(PENDING+studioContext()));return row.context===studioContext()&&typeof row.quoteId==='string'&&typeof row.key==='string'?row as unknown as PreparationIntent:null;}
export function preparationIntent(quote:PreparationQuote):PreparationIntent{return{context:studioContext(),quoteId:quote.id,key:'analysis:quote:'+quote.id};}
export async function checkPreparation(row:PreparationIntent):Promise<PreparationReceipt>{guardStudio(row.context);const result=await personalSession.request<PreparationReceipt>(`/preparation/quotes/${encodeURIComponent(row.quoteId)}/requests/${encodeURIComponent(row.key)}`);guardStudio(row.context);if(result.found)wx.removeStorageSync(PENDING+row.context);return result;}
export async function sendPreparation(row:PreparationIntent,repeat=false):Promise<unknown>{guardStudio(row.context);const prior=pendingPreparation();if(!prior&&!repeat&&hasPendingWave(row.context))throw new ClientError('batch_run_unconfirmed','请先在批次生成页面核对原提交');if(prior&&prior.key!==row.key)throw new ClientError('analysis_unconfirmed','请先核对原分析提交');try{wx.setStorageSync(PENDING+row.context,row);}catch(_){throw new ClientError('operation_cache_unavailable','无法保存原提交记录，请检查存储');}
 try{const result=await personalSession.request(`/preparation/quotes/${encodeURIComponent(row.quoteId)}/confirm`,'POST',{},row.key);guardStudio(row.context);wx.removeStorageSync(PENDING+row.context);return result;}
 catch(error){if(!repeat&&!prior&&row.context===studioContext()&&error instanceof ClientError&&error.status>=400&&error.status<500)wx.removeStorageSync(PENDING+row.context);throw error;}}
export const dispositionName=(value:string):string=>({cached:'使用已有结果',reused:'复用原任务',new:'新增分析'}[value]||value);
