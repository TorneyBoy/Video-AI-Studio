import {personalSession} from './api';
import {ClientError,object} from './contracts';
import {guardStudio,studioContext,studioHash,pendingStudioOperations} from './studio-state';
import {pendingBatchRun,RunItem} from './batch-run-state';
import {pendingBatch} from './batch-state';
import {pendingPreparation} from './preparation-state';
import {pendingTask} from './billing-state';
import {ReviewReport} from './review-state';
import {sha256} from '../vendor/sha256';
import {fileFingerprint} from './file-hash';

export interface BatchWork extends RunItem {work:{state:string;status_label:string;reason:string;can_open:boolean;artifact_id:string|null;job_status:string}}
export interface BatchDeliveryView {batch_id:string;batch_name:string;project_id:string;run_id:string;created_at:string;quality:string;total:number;counts:Record<string,number>;items:BatchWork[];note:string;configuration:{available:boolean;max_video_bytes:number;note:string}}
export interface ReviewPreparation {batch_id:string;run_id:string;can_review:boolean;items:Array<{key:string;name:string;artifact_id:string|null;revision_number:number;report:ReviewReport|null}>}
export interface ManifestItem {key:string;name:string;artifact_id:string|null;revision_number:number;included:boolean;reason:string|null;filename?:string;binding_hash?:string;review_id?:string;output_sha256?:string;size_bytes?:number;subtitle_note?:string}
export interface DeliveryPreview {batch_id:string;run_id:string;manifest_hash:string;can_package:boolean;reasons:string[];manifest:{selected_count:number;included_count:number;excluded_count:number;video_bytes:number;include_subtitles:boolean;items:ManifestItem[]};file:{filename:string;content:string;sha256:string};configuration:BatchDeliveryView['configuration']}
export interface PackageView {id:string;batch_id:string;run_id:string;job_id:string;status:string;included_count:number;excluded_count:number;video_bytes:number;manifest_hash:string;size_bytes:number|null;sha256:string|null;can_download:boolean;reason:string|null;created_at:string;expires_at:string;requires_validation:boolean;note:string}
export interface BatchDeliveryOperation {context:string;batchId:string;runId:string;kind:'review'|'package';key:string;body:Record<string,unknown>}
export interface DeliveryReceipt {found:boolean;result:Record<string,unknown>|null}
const PREFIX='video-ai-studio.batch-delivery-operation.v1.';
export const workStatus=(state:string)=>({processing:'处理中',pending_review:'待审核',approved:'已批准',returned:'需修改',stale:'需重新审核',unavailable:'暂不可领取',failed:'未完成'}[state]||'待核对');
export const packageReason=(reason:string)=>({package_service_unavailable:'打包服务尚未启用，可逐项领取',package_no_approved_outputs:'所选项尚无已批准成片',package_video_limit:'所选视频总大小超过打包上限，请减少选择',package_text_limit:'清单或字幕过大，请减少选择或不包含字幕',package_approval_changed:'包内成片的批准或归档已变化',package_expired:'交付包已超过保留期限'}[reason]||'请核对当前成片、审核与任务状态');
export function deliveryPath(batchId:string,runId:string){return'/batches/'+encodeURIComponent(batchId)+'/runs/'+encodeURIComponent(runId);}
export function pendingBatchDelivery():BatchDeliveryOperation|null{const row=object(wx.getStorageSync(PREFIX+studioContext()));return row.context===studioContext()&&['review','package'].includes(String(row.kind))&&typeof row.key==='string'?row as unknown as BatchDeliveryOperation:null;}
export function otherDeliveryPending():boolean{return Boolean(pendingBatchRun()||pendingBatch()||pendingPreparation()||pendingTask()||pendingStudioOperations().length);}
export function deliveryOperation(batchId:string,runId:string,kind:BatchDeliveryOperation['kind'],body:Record<string,unknown>):BatchDeliveryOperation{return{context:studioContext(),batchId,runId,kind,key:'batch-'+kind+':'+studioHash({body,batchId,runId,time:Date.now(),random:Math.random()}),body};}
export function acknowledgeDelivery(row:BatchDeliveryOperation){const key=PREFIX+row.context;if(object(wx.getStorageSync(key)).key===row.key)wx.removeStorageSync(key);}
export async function sendBatchDelivery<T>(row:BatchDeliveryOperation,repeat=false):Promise<T>{guardStudio(row.context);const prior=pendingBatchDelivery();if(prior&&prior.key!==row.key)throw new ClientError('batch_delivery_unconfirmed','请先核对原审核或打包提交');if(!prior&&!repeat&&otherDeliveryPending())throw new ClientError('operation_unconfirmed','请先核对其他制作或任务操作');
 try{wx.setStorageSync(PREFIX+row.context,row);}catch(_){throw new ClientError('operation_cache_unavailable','无法保存原提交记录，请检查本机存储');}
 try{return await personalSession.request<T>(deliveryPath(row.batchId,row.runId)+(row.kind==='review'?'/reviews':'/packages'),'POST',row.body,row.key);}
 catch(error){if(!prior&&!repeat&&error instanceof ClientError&&error.status>=400&&error.status<500)acknowledgeDelivery(row);throw error;}}
export async function checkBatchDelivery(row:BatchDeliveryOperation):Promise<DeliveryReceipt>{guardStudio(row.context);return personalSession.request<DeliveryReceipt>(deliveryPath(row.batchId,row.runId)+(row.kind==='review'?'/review-requests/':'/package-requests/')+encodeURIComponent(row.key));}
export async function writeBatchManifest(value:DeliveryPreview):Promise<string>{
 if(value.file.sha256!==value.manifest_hash||sha256(value.file.content)!==value.manifest_hash||value.file.filename!=='batch-delivery-'+value.run_id+'.json')throw new ClientError('manifest_invalid','交付清单校验失败，请重新获取');
 const path=wx.env.USER_DATA_PATH+'/'+value.file.filename;
 await new Promise<void>((resolve,reject)=>wx.getFileSystemManager().writeFile({filePath:path,data:value.file.content,encoding:'utf8',success:()=>resolve(),fail:()=>reject(new ClientError('manifest_save_failed','清单保存失败，请检查空间'))}));return path;
}
export async function downloadVerified(url:string,size:number,hash:string):Promise<string>{
 if(!url.startsWith('https://')||!Number.isSafeInteger(size)||size<1||!/^[0-9a-f]{64}$/.test(hash))throw new ClientError('download_invalid','下载授权或文件校验信息无效');
 const path=await new Promise<string>((resolve,reject)=>wx.downloadFile({url,success:result=>result.statusCode===200?resolve(result.tempFilePath):reject(new ClientError('download_failed','下载未完成，请重新获取授权')),fail:()=>reject(new ClientError('download_failed','下载未完成，请检查网络与空间'))}));
 if(await fileFingerprint(path,size,()=>{})!==hash)throw new ClientError('download_hash_invalid','下载内容校验失败，请重新获取');return path;
}
