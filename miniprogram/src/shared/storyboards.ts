import {personalSession} from './api';
import {ClientError} from './contracts';
import * as Forms from '../vendor/storyboard-form';

export interface Operation {kind:'reference'|'create'|'save';key:string;path:string;receipt:string;body:unknown}
export interface Cache {context:string;base:Forms.Revision|null;draft:Forms.Content|null;referenceId:string|null;pending:Operation|null;rangeForm?:{asset_id:string;start:string;end:string;note:string};durationInputs?:Record<string,string>}
export function context(){return (personalSession.profile?.id||'')+':'+personalSession.activeProject();}
function key(){return 'video-ai-studio.storyboards.v1.'+context();}
export function cache():Cache {const row=wx.getStorageSync(key());return row?.context===context()?row:{context:context(),base:null,draft:null,referenceId:null,pending:null};}
export function persist(row:Cache){if(row.context!==context()||!personalSession.profile)throw new ClientError('session_changed','账号或项目已变化');wx.setStorageSync(key(),row);}
export function operation(kind:Operation['kind'],path:string,receipt:string,body:unknown):Operation{return {kind,key:'story-'+Forms.id(),path,receipt,body:Forms.copy(body)};}
export async function send(row:Cache,op:Operation):Promise<Forms.Revision|Forms.Reference>{const previous=cache().pending;if(previous&&JSON.stringify(previous)!==JSON.stringify(op))throw new ClientError('operation_unconfirmed','请先核对原提交');row.pending=Forms.copy(op);persist(row);try{return await personalSession.request(op.path,'POST',op.body,op.key);}catch(error){if(row.context===context()&&error instanceof ClientError&&error.status>=400&&error.status<500){row.pending=null;persist(row);}throw error;}}
export async function receipt(op:Operation){return personalSession.request<{found:boolean;result:Forms.Revision|Forms.Reference}>(op.receipt+encodeURIComponent(op.key));}
