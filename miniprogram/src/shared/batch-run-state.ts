import {deliveryWaveReference} from './pending-wave';
import {personalSession} from './api';
import {ClientError,object} from './contracts';
import {guardStudio,studioContext,studioHash} from './studio-state';
import {BatchAggregate} from './batch-state';
import {TaskView} from './billing-state';

export interface RunItem {key:string;name:string;job_id:string|null;revision_id:string;revision_number:number;disposition:string;artifact_id:string|null;status:string;task:TaskView|null}
export interface RenderRun {id:string;batch_id:string;quality:string;parent_job_id:string|null;aggregate:BatchAggregate;items:RunItem[];created_at:string;note:string}
export interface BatchRenderPrice {id:string;batch_id:string;quality:string;force_new:boolean;expires_at:string;status:string;can_submit:boolean;reasons:string[];totals:Array<{currency:string;amount_micros:string}>;item_count:number;note:string;items:Array<{key:string;name:string;revision_id:string;revision_number:number;disposition:string;job_id:string|null;amount_micros:string|null;currency:string|null;problem:string|null}>}
export interface BatchRunOperation {context:string;batchId:string;phaseId:string;kind:'render'|'action';key:string;body:Record<string,unknown>}
export interface BatchRunReceipt {found:boolean;can_abandon?:boolean;result?:{batch_id:string;run_id?:string;items?:Array<{applied?:boolean;reason?:string}>}}
const PREFIX='video-ai-studio.batch-run-operation.v1.';
export function pendingBatchRun():BatchRunOperation|null{const row=object(wx.getStorageSync(PREFIX+studioContext()));return row.context===studioContext()&&['render','action'].includes(String(row.kind))&&typeof row.key==='string'?row as unknown as BatchRunOperation:null;}
export function runOperation(batchId:string,kind:BatchRunOperation['kind'],body:Record<string,unknown>,phaseId='plan'):BatchRunOperation{return{context:studioContext(),batchId,phaseId,kind,key:kind==='render'?'batch-render-confirm:'+String(body.quote_id):'batch-task:'+studioHash({batchId,phaseId,body,time:Date.now(),random:Math.random()}),body};}
export function acknowledgeRun(row:BatchRunOperation):void{const saved=object(wx.getStorageSync(PREFIX+row.context));if(saved.key===row.key)wx.removeStorageSync(PREFIX+row.context);}
export async function sendRun<T>(row:BatchRunOperation,repeat=false):Promise<T>{guardStudio(row.context);const prior=pendingBatchRun();if(!prior&&!repeat&&deliveryWaveReference(row.context))throw new ClientError('batch_delivery_unconfirmed','请先核对原审核或打包提交');if(prior&&prior.key!==row.key)throw new ClientError('batch_run_unconfirmed','请先核对原批次生成或任务操作');try{wx.setStorageSync(PREFIX+row.context,row);}catch(_){throw new ClientError('batch_run_cache_unavailable','无法保存原提交记录，请检查存储后再提交');}
 const endpoint='/batches/'+encodeURIComponent(row.batchId)+(row.kind==='render'?'/render-confirm':'/phases/'+encodeURIComponent(row.phaseId)+'/actions');
 try{const result=await personalSession.request<T>(endpoint,'POST',row.body,row.key);guardStudio(row.context);return result;}
 catch(error){if(!prior&&!repeat&&error instanceof ClientError&&error.status>=400&&error.status<500)wx.removeStorageSync(PREFIX+row.context);throw error;}}
export async function checkRun(row:BatchRunOperation):Promise<BatchRunReceipt>{guardStudio(row.context);const endpoint='/batches/'+encodeURIComponent(row.batchId)+(row.kind==='render'?'/render-requests/':'/phases/'+encodeURIComponent(row.phaseId)+'/actions/requests/')+encodeURIComponent(row.key)+(row.kind==='render'?'?quote_id='+encodeURIComponent(String(row.body.quote_id)):'');const receipt=await personalSession.request<BatchRunReceipt>(endpoint);guardStudio(row.context);return receipt;}
export function taskSelection(key:string,task:TaskView):Record<string,unknown>{return{key,job_id:task.id,expected_status:task.status,expected_attempt:task.attempt_count,expected_generation:task.execution_generation};}
