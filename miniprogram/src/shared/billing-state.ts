import {hasPendingWave} from './pending-wave';
import {personalSession} from './api';
import {ClientError,object} from './contracts';
import {studioContext} from './studio-state';
import {sha256} from '../vendor/sha256';

export interface ProjectPolicy {project_id:string;revision:number;currency:string;limit_micros:string|null;remaining_micros:string|null;can_manage:boolean;note:string}
export interface Totals {currency:string;reserved_micros:string;pending_micros:string;pending_count:number;settled_micros:string;adjustment_micros:string;released_micros:string}
export interface FinanceSummary {scope:string;period_key:string;note:string;totals:Totals[];project_policy:ProjectPolicy|null;tenant_policy:{currency:string;monthly_limit_micros:string|null;task_limit_micros:string|null;remaining_micros:string|null}}
export interface CostEntry {id:string;job_id:string;job_type:string;job_status:string;status_label:string;status:string;amount_micros:string;adjustment_micros:string;net_settled_micros:string|null;currency:string;is_estimate:boolean;period_key:string}
export interface TaskView {id:string;project_id:string|null;type:string;status:'queued'|'running'|'failed'|'succeeded'|'cancelled';attempt_count:number;execution_generation:number;max_attempts:number;can_retry:boolean;can_cancel:boolean;failure_message:string|null}
export interface ListPage<T> {items:T[];next_skip:number;has_more:boolean}
export interface TaskIntent {context:string;jobId:string;key:string;body:{action:'retry'|'cancel';expected_status:string;expected_attempt:number;expected_generation:number}}
export interface TaskReceipt {found:boolean;current:TaskView}
export interface RenderQuote {id:string;project_id:string;amount_micros:string;currency:string;price_version:string;expires_at:string;can_submit:boolean;status:string;reasons:string[];note:string;provider:string;model_id:string;model_version:string;duration_bound_seconds:number|null;summary:{quality:string;duration:number;width:number;height:number;revision_number:number|null}}
const PREFIX='video-ai-studio.task-action.v1.';
export const statusName=(value:string):string=>({queued:'排队中',running:'处理中',failed:'未完成',succeeded:'已完成',cancelled:'已取消'}[value]||value);
export function microText(value:string|number|null|undefined):string{
  if(value===null||value===undefined)return'未设置';let text=String(value);if(!/^-?\d+$/.test(text))return'金额待核对';const sign=text[0]==='-'?'-':'';
  text=text.replace(/^-/,'').replace(/^0+(?=\d)/,'').padStart(7,'0');const whole=text.slice(0,-6).replace(/\B(?=(\d{3})+(?!\d))/g,','),fraction=text.slice(-6).replace(/0+$/,'');return sign+whole+(fraction?'.'+fraction:'');
}
export function parseLimit(value:string):number|null{
  const text=value.trim();if(!text)return null;if(!/^(0|[1-9]\d*)(\.\d{1,6})?$/.test(text))throw new ClientError('limit_invalid','请填写非负金额，最多6位小数；留空不加项目上限');
  const [whole,fraction='']=text.split('.'),digits=(whole+fraction.padEnd(6,'0')).replace(/^0+(?=\d)/,'');if(digits.length>16||(digits.length===16&&digits>'9000000000000000'))throw new ClientError('limit_invalid','金额超出可设置范围');return Number(digits);
}
function key(){return PREFIX+studioContext();}
export function pendingTask():TaskIntent|null{const saved=object(wx.getStorageSync(key()));return saved.context===studioContext()&&typeof saved.key==='string'?saved as unknown as TaskIntent:null;}
function guard(row:TaskIntent){if(!studioContext()||row.context!==studioContext())throw new ClientError('task_context_changed','账号或项目已变化，请重新打开');}
export function taskIntent(job:TaskView,action:'retry'|'cancel'):TaskIntent{const body={action,expected_status:job.status,expected_attempt:job.attempt_count,expected_generation:job.execution_generation};return{context:studioContext(),jobId:job.id,key:'task:'+sha256(JSON.stringify(body)+Date.now()+Math.random()),body};}
export function forgetTask(){wx.removeStorageSync(key());}
export async function sendTask(row:TaskIntent):Promise<TaskReceipt>{guard(row);const prior=pendingTask();if(!prior&&hasPendingWave(row.context))throw new ClientError('batch_run_unconfirmed','请先在批次生成页面核对原提交');if(prior&&prior.key!==row.key)throw new ClientError('task_unconfirmed','请先核对原任务操作');wx.setStorageSync(key(),row);
  try{const receipt=await personalSession.request<TaskReceipt>(`/billing/jobs/${encodeURIComponent(row.jobId)}/actions`,'POST',row.body,row.key);guard(row);forgetTask();return receipt;}
  catch(error){if(!prior&&row.context===studioContext()&&error instanceof ClientError&&error.status>=400&&error.status<500)forgetTask();throw error;}}
export async function checkTask(row:TaskIntent):Promise<TaskReceipt>{guard(row);const receipt=await personalSession.request<TaskReceipt>(`/billing/jobs/${encodeURIComponent(row.jobId)}/actions/${encodeURIComponent(row.key)}`);guard(row);if(receipt.found)forgetTask();return receipt;}

export function costReason(value:string):string{return({original_render_unresolved:'原生成任务需先处理',original_analysis_unresolved:'原分析任务需先处理',monthly_limit:'空间本月额度不足',task_limit:'超过单任务上限',currency_mismatch:'报价币种与空间额度不一致',project_limit:'超过项目累计上限',project_currency_mismatch:'报价币种与项目预算不一致',expired:'报价已过期',consumed:'报价已使用',ready:'当前不可提交'}[value]||'请核对费用与预算');}
export function taskName(value:string):string{return({'render.draft':'预览生成','render.high':'高清生成','render.standard':'标准生成','render.studio':'成片生成','auto_edit.plan':'剪辑计划','asset.analyze':'素材分析','batch.plan':'批量剪辑计划','batch.render':'批量生成汇总','delivery.package':'交付打包','video.generate':'AI视频','voice.generate':'配音','avatar.generate':'数字人'}[value]||'制作任务');}
