import {Asset} from '../shared/contracts';
export type Operation='video_generation'|'face_replace'|'person_replace';
export interface Binding {document_id:string;revision_id:string;shot_id:string}
export type Scalar=string|number|boolean;
export interface ParameterSpec {type:'string'|'integer'|'number'|'boolean';enum?:Scalar[];minimum?:number;maximum?:number;max_length?:number;required?:boolean;default?:Scalar}
export interface RequestContract {parameters:Record<string,ParameterSpec>;operations:Operation[];input_mode:'text'|'image_reference'|'identity_video';output_short_edges?:Record<string,number>}
export interface Scenario {id:string;version:string;name:string;description?:string;validation_status:string;prompt_version?:string;allowed_operations?:Operation[]}
export interface Selection {provider?:string;model_id?:string;model_version?:string;selection_reason?:{method?:string;quality_ranked?:boolean};scenario?:{id:string;version:string;prompt_version?:string}|null;prompt_version?:string}
export interface ParameterField {name:string;label:string;required:boolean;kind:string;value:string;values:Array<Scalar|null>;options:string[];index:number;minimum?:number;maximum?:number;maxLength:number}
export interface Model {id:string;capability:string;provider:string;model_id:string;model_version:string;status:string;available:boolean;limits?:{request_contract?:RequestContract;features?:string[];max_duration_seconds?:number;max_output_duration_seconds?:number;allowed_durations?:number[];generation_resolutions?:string[];generation_modes?:string[];min_reference_inputs?:number;max_reference_inputs?:number;max_prompt_chars?:number}}
export interface Profile {id:string;name:string;status:string;reference_asset_id?:string;scopes?:string[]}
export interface ProfileDraft {name:string;subject_id:string;reference_asset_id:string;granted:boolean}
export interface Draft {operation:Operation;prompt:string;source_asset_id:string;avatar_profile_id:string;reference_asset_ids:string[];duration_seconds:string;model_id:string;resolution:string;mode:string;scenario_id:string;scenario_version:string;parameters:Record<string,Scalar|undefined>;parameter_inputs:Record<string,string>;parameter_model_id:string;storyboard:Binding|null}
export const operations:Array<{id:Operation;label:string;capability:string}>;
export function label(id:string):string;
export function kind(value:string|{kind:string}):string;
export function duration(asset?:Asset):number|null;
export function summary(value:unknown):string;
export function profileBody(value:ProfileDraft,assets:Asset[]):Record<string,unknown>;
export function models(catalog:Model[],operation:Operation):Array<Model&{usable:boolean;reason:string}>;
export function draft(value?:Partial<Draft>):Draft;
export function configure(value:Draft,catalog:Model[]):Draft;
export function scenarios(catalog:Scenario[],operation:Operation):Scenario[];
export function scenarioLabel(row:Scenario):string;
export function durationChoices(model?:Model):number[];
export function parameterFields(value:Draft,model?:Model):ParameterField[];
export function parameterInput(model:Model|undefined,name:string,value:Scalar):Scalar|undefined;
export function selectionText(value:Selection|null|undefined):string;
export function quoteBody(value:Draft,catalog:Model[],assets:Asset[],profiles:Profile[],scenarios?:Scenario[]):Record<string,unknown>;
export function pending<T>(value:T,context:string):T|null;
export function path(project:string,row:{kind:string;jobId?:string}):string;
export function terminal(status:string):boolean;
export function status(status:string):string;
