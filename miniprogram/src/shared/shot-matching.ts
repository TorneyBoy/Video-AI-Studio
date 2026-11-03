import {personalSession} from './api';
import {ClientError} from './contracts';
import * as Forms from '../vendor/shot-matching-form';
import {id} from '../vendor/storyboard-form';
export interface Operation {kind:'report'|'source'|'annotation';key:string;path:string;receipt:string;body:unknown}
export interface Cache {context:string;assetIds:string[];overrides:Record<string,Forms.Override>;report:Forms.Report|null;source:Forms.Source|null;rows:Forms.Choice[];removedUnlocks:string[];allowReuse:boolean;pending:Operation|null;annotation?:{assetId:string;head:Forms.Semantic|null;draft:Forms.AnnotationDraft}}
export function context(documentId:string){return (personalSession.profile?.id||'')+':'+personalSession.activeProject()+':'+documentId;}
function key(documentId:string){return 'video-ai-studio.matching.v1.'+context(documentId);}
export function cache(documentId:string):Cache{const row=wx.getStorageSync(key(documentId));return row?.context===context(documentId)?row:{context:context(documentId),assetIds:[],overrides:{},report:null,source:null,rows:[],removedUnlocks:[],allowReuse:false,pending:null};}
export function persist(documentId:string,row:Cache){if(!personalSession.profile||row.context!==context(documentId))throw new ClientError('session_changed','账号或项目已变化');wx.setStorageSync(key(documentId),row);}
export function operation(kind:Operation['kind'],path:string,receipt:string,body:unknown):Operation{return {kind,path,receipt,body:Forms.copy(body),key:'matching-'+id()};}
export async function send(documentId:string,row:Cache,op:Operation):Promise<Forms.Report|Forms.Source|Forms.Semantic>{const previous=cache(documentId).pending;if(previous&&JSON.stringify(previous)!==JSON.stringify(op))throw new ClientError('operation_unconfirmed','请先核对原提交');row.pending=Forms.copy(op);persist(documentId,row);try{return await personalSession.request(op.path,'POST',op.body,op.key);}catch(error){if(row.context===context(documentId)&&error instanceof ClientError&&error.status>=400&&error.status<500){row.pending=null;persist(documentId,row);}throw error;}}
export function receipt(op:Operation){return personalSession.request<{found:boolean;result:Forms.Report|Forms.Source|Forms.Semantic}>(op.receipt+encodeURIComponent(op.key));}
