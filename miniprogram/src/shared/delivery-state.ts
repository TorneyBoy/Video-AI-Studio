import {personalSession} from './api';
import {ClientError,object} from './contracts';
import {ReviewReport,reviewContext} from './review-state';
import {sha256} from '../vendor/sha256';

export interface WorkItem {job_id:string;artifact_id:string|null;state:string;status_label:string;reason:string;quality:string;revision_number:number|null;created_at:string;duration:number|null;width:number|null;height:number|null;can_open:boolean;can_compare:boolean}
export interface WorkPage {items:WorkItem[];next_cursor:string|null;has_more:boolean;total_jobs:number}
export interface DeliveryFile {artifact_id:string;binding_hash:string;filename:string;content:string;sha256:string;encoding:string;kind:string;warnings:string[]}
export interface ShareEntry {id:string;artifact_id:string;label:string;audience:string;allow_download:boolean;include_subtitles:boolean;expires_at:string;created_at:string;revoked_at:string|null;status:string;url:string|null;access_note:string}
export interface ShareForm {label:string;audience:'workspace'|'anyone_with_link';allowDownload:boolean;includeSubtitles:boolean;seconds:number}
export interface ShareBody {binding_hash:string;review_id:string;label:string;audience:ShareForm['audience'];allow_download:boolean;include_subtitles:boolean;expires_in_seconds:number}
export interface DeliveryOperation {context:string;artifactId:string;key:string;kind:'create'|'revoke';body:ShareBody|null;shareId?:string}
export interface ComparisonSide {artifact_id:string;revision_number:number|null;review_status:string;can_preview:boolean;reason:string;duration:number;resolution:{width:number;height:number};fps:number;source_labels:Record<string,string>;tracks:Array<{id:string;type:string;muted:boolean;clips:number}>}
export interface ComparisonChange {clip_id:string;kind:string;track_type:string;position:number;before_asset_id:string|null;after_asset_id:string|null;fields:Record<string,{before:unknown;after:unknown}>}
export interface Comparison {left:ComparisonSide;right:ComparisonSide;comparison_hash:string;changes:ComparisonChange[];total_changes:number;has_more:boolean;sync_duration:number;timing_note:string}
const PREFIX='video-ai-studio.delivery.v1.';
export const blankShare=():ShareForm=>({label:'视频分享',audience:'workspace',allowDownload:false,includeSubtitles:false,seconds:86400});
export const shareStatus=(value:string):string=>({active:'有效',revoked:'已撤销',expired:'已过期',invalidated:'审核或内容已变化',key_changed:'授权已更新，需新建链接',unavailable:'服务未配置'}[value]||'待核对');
function key(id:string){return PREFIX+reviewContext(id);}
export function deliveryState(id:string):{form:ShareForm;pending:DeliveryOperation|null}{const saved=object(wx.getStorageSync(key(id)));return{form:{...blankShare(),...object(saved.form)} as ShareForm,pending:saved.pending as DeliveryOperation||null};}
export function saveDeliveryState(id:string,form:ShareForm,pending:DeliveryOperation|null=null):void{wx.setStorageSync(key(id),{form,pending});}
export function shareOperation(id:string,report:ReviewReport,form:ShareForm):DeliveryOperation{
  if(!report.latest)throw new ClientError('share_approval_required','请先批准这份成片');
  const body:ShareBody={binding_hash:report.binding_hash,review_id:report.latest.id,label:form.label,audience:form.audience,allow_download:form.allowDownload,include_subtitles:form.includeSubtitles,expires_in_seconds:form.seconds};
  return{context:reviewContext(id),artifactId:id,key:'share:'+sha256(JSON.stringify(body)+Date.now()+Math.random()),kind:'create',body};
}
function guard(row:DeliveryOperation){if(!personalSession.profile||row.context!==reviewContext(row.artifactId))throw new ClientError('delivery_context_changed','账号或作品已变化，请重新打开');}
export async function sendDelivery(row:DeliveryOperation,form:ShareForm):Promise<ShareEntry>{
  guard(row);const prior=deliveryState(row.artifactId).pending;
  if(prior&&prior.key!==row.key)throw new ClientError('delivery_unconfirmed','请先核对原分享操作');
  saveDeliveryState(row.artifactId,form,row);
  try{const path=`/artifacts/${encodeURIComponent(row.artifactId)}/shares`+(row.kind==='revoke'?`/${encodeURIComponent(row.shareId||'')}/revoke`:'');
    const result=await personalSession.request<ShareEntry>(path,'POST',row.body||{},row.key);guard(row);saveDeliveryState(row.artifactId,form);return result;
  }catch(error){if(!prior&&row.context===reviewContext(row.artifactId)&&error instanceof ClientError&&error.status>=400&&error.status<500)saveDeliveryState(row.artifactId,form);throw error;}
}
export async function checkDelivery(row:DeliveryOperation,form:ShareForm):Promise<{found:boolean;share:ShareEntry|null}>{
  guard(row);let result:{found:boolean;share:ShareEntry|null};
  if(row.kind==='create')result=await personalSession.request(`/artifacts/${encodeURIComponent(row.artifactId)}/share-requests/${encodeURIComponent(row.key)}`);
  else{const share=await personalSession.request<ShareEntry>(`/artifacts/${encodeURIComponent(row.artifactId)}/shares/${encodeURIComponent(row.shareId||'')}/link`,'POST',{});result={found:share.status==='revoked',share};}
  guard(row);if(result.found)saveDeliveryState(row.artifactId,form);return result;
}
export async function writeDeliveryFile(file:DeliveryFile,id:string,binding:string):Promise<string>{
  if(file.artifact_id!==id||file.binding_hash!==binding||sha256(file.content)!==file.sha256||!(/^(delivery|subtitles)-[A-Za-z0-9-]{1,80}(-internal|-public)?\.(json|srt|vtt)$/).test(file.filename))throw new ClientError('delivery_file_invalid','文件与当前成片或内容校验不符');
  const path=wx.env.USER_DATA_PATH+'/'+file.filename;
  await new Promise<void>((resolve,reject)=>wx.getFileSystemManager().writeFile({filePath:path,data:file.content,encoding:'utf8',success:()=>resolve(),fail:()=>reject(new ClientError('delivery_write_failed','文件保存未完成，请检查可用空间'))}));return path;
}
const FIELD_NAMES:Record<string,string>={volume:'音量',range:'成片区间',source_start:'素材起点',source_end:'素材终点',composition:'适配/淡入淡出',transition:'转场',placement:'画面区域',opacity:'透明度',text:'字幕文字',style:'字幕样式',asset_id:'素材',clip_index:'片段顺序',track_id:'所在轨道',track_index:'轨道层级',track_muted:'静音/隐藏',subtitle:'字幕来源关联'};
export function changeView(change:ComparisonChange,comparison:Comparison){return{...change,title:({added:'新增',removed:'移除',changed:'修改'}[change.kind]||'变化')+' · '+({video:'主视频',audio:'音轨',overlay:'叠加',subtitle:'字幕'}[change.track_type]||'片段')+' 第'+change.position+'段',fields:Object.entries(change.fields).filter(([name])=>name!=='id'&&name!=='track_type').map(([name,value])=>{const format=(v:unknown,side:ComparisonSide)=>v===null?'无':name==='asset_id'?side.source_labels[String(v)]||'素材':typeof v==='string'?v:JSON.stringify(v);return{name:FIELD_NAMES[name]||name,before:format(value.before,comparison.left).slice(0,500),after:format(value.after,comparison.right).slice(0,500),fullBefore:format(value.before,comparison.left),fullAfter:format(value.after,comparison.right)};})};}

export function trackSummary(side:ComparisonSide):string{return(side.tracks||[]).map(track=>({video:'主视频',audio:'音轨',overlay:'叠加',subtitle:'字幕'}[track.type]||'轨道')+' '+track.clips+'段'+(track.muted?'（静音/隐藏）':'')).join(' · ')||'无轨道';}
