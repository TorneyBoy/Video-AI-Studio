import {personalSession} from './api';
import {Asset,ClientError,object} from './contracts';
import {guardStudio,pendingStudioOperations,studioContext} from './studio-state';
import {hasPendingWave} from './pending-wave';
import {pendingTask} from './billing-state';
import {pendingPreparation} from './preparation-state';
import {pendingBatch} from './batch-state';
import * as Forms from '../vendor/ai-tools-form';
export interface Job extends Forms.Selection {id:string;status:string;operation:Forms.Operation;prompt:string;failure_code:string|null;can_adopt:boolean;adopted_asset_id:string|null;artifact_id:string|null;storyboard:Forms.Binding|null;execution_generation:number;attempt_count:number}
export interface Quote extends Forms.Selection {id:string;summary:{operation:Forms.Operation;duration_seconds:number;storyboard:Forms.Binding|null};amount_micros:number;currency:string;expires_at:string;can_submit:boolean;reasons:string[];provider:string;model_id:string;model_version:string;price_version:string;status:string;job_id:string|null}
export interface View {scenario_profiles?:Forms.Scenario[];capabilities:Forms.Model[];profiles:Forms.Profile[];jobs:Job[]}
export interface Pending {context:string;kind:'quote'|'submit'|'adopt'|'profile';key:string;body:Record<string,unknown>;jobId?:string}
export interface Cache {draft:Forms.Draft;pending:Pending|null}
const PREFIX='video-ai-studio.ai-production.v1.';
const inFlight=new Set<string>();
export function cache():Cache {const saved=object(wx.getStorageSync(PREFIX+studioContext()));return{draft:Forms.draft(object(saved.draft) as Partial<Forms.Draft>),pending:Forms.pending((saved.pending||null) as Pending|null,studioContext())};}
export function persist(value:Cache):void {guardStudio(studioContext());wx.setStorageSync(PREFIX+studioContext(),value);}
export function base():string {guardStudio(studioContext());return '/projects/'+encodeURIComponent(personalSession.activeProject())+'/ai-production';}
export function intent(kind:Pending['kind'],body:Record<string,unknown>,jobId?:string):Pending{return {context:studioContext(),kind,body,key:'ai-'+kind+'-'+Date.now()+'-'+Math.random().toString(36).slice(2),...(jobId?{jobId}:{})};}
export async function send<T>(value:Cache,row:Pending,repeat=false):Promise<T>{guardStudio(row.context);if(inFlight.has(row.context))throw new ClientError('operation_busy','请等待当前操作完成');if(!repeat&&(value.pending||pendingStudioOperations().length||hasPendingWave(row.context)||pendingTask()||pendingPreparation()||pendingBatch()))throw new ClientError('operation_unconfirmed','请先到费用与任务恢复或原制作页面核对上次提交');value.pending=row;try{persist(value);}catch(_){throw new ClientError('operation_cache_unavailable','无法保存原请求，尚未提交；请检查小程序存储');}inFlight.add(row.context);try{const result=await personalSession.request<T>(Forms.path(personalSession.activeProject(),row),'POST',row.body,row.key);guardStudio(row.context);value.pending=null;persist(value);return result;}catch(error){if(row.context===studioContext()&&!repeat&&error instanceof ClientError&&error.status>=400&&error.status<500){value.pending=null;persist(value);}throw error;}finally{inFlight.delete(row.context);}}
export async function check(value:Cache):Promise<{found:boolean;result:Job|Forms.Profile|null}>{const row=value.pending;if(!row)throw new ClientError('request_missing','没有待确认的请求');guardStudio(row.context);if(row.kind==='quote')return {found:false,result:null};if(row.kind==='profile'){const receipt=await personalSession.request<{found:boolean;result:Forms.Profile|null}>(base()+'/profiles/requests/'+encodeURIComponent(row.key));guardStudio(row.context);if(receipt.found&&receipt.result){value.pending=null;persist(value);}return receipt;}const response=row.kind==='submit'?await personalSession.request<{found:boolean;result:Job|null}>(base()+'/requests/'+encodeURIComponent(row.key)):{found:false,result:await personalSession.request<Job>(base()+'/jobs/'+encodeURIComponent(row.jobId||''))};guardStudio(row.context);const found=row.kind==='submit'?Boolean(response.found&&response.result):Boolean(response.result?.adopted_asset_id);if(found){value.pending=null;persist(value);}return {found,result:response.result};}
export async function preview(id:string):Promise<string>{const context=studioContext();const value=await personalSession.request<{url:string}>('/assets/'+encodeURIComponent(id)+'/access','POST',{});guardStudio(context);return value.url;}
export type Adoption=Asset;
