import {Visual} from './storyboard-form';
export interface Annotation {id:string;start_ms:number|null;end_ms:number|null;visual:Visual;tags:string[]}
export interface Semantic {asset_id:string;revision_id:string|null;head_revision_id:string|null;number:number;source_current:boolean;content:{spans:Annotation[];note:string;provenance:{kind:string}}|null}
export interface Candidate {id:string;asset_id:string;name:string;kind:'video'|'image';start_ms:number;end_ms:number|null;input_fingerprint:string;verdict:string;semantic_provenance:string;observed_visual:Record<string,string>;observed_tags:string[];reasons:string[];review:string[];diversity:{already_recommended_source:boolean;adjacent_recommended_source:boolean}}
export interface Item {shot_id:string;requirement_hash:string;requirement:{visual:Record<string,string>};candidates:Candidate[];gap:string;excluded_counts:Record<string,number>;reshoot:{duration_ms:number;intent:string;visual:Visual;constraints:string[];facts:string[]}}
export interface Override {media_types:Array<'video'|'image'>;orientation:'any'|'portrait'|'landscape'|'square';required_tags:string[];excluded_tags:string[]}
export interface MatchBody {story_revision_id:string;asset_ids:string[];overrides:Record<string,Override>;candidates_per_shot:number}
export interface Report {id:string;document_id:string;story_revision_id:string;stale_inputs:boolean;created_at:string;result:{configuration:MatchBody;items:Item[];excluded_assets:Array<{asset_id:string;name:string;reason:string}>;summary:{shots:number;with_candidates:number;without_candidates:number};notice:string}}
export interface Choice {shot_id:string;mode:'candidate'|'keep'|'reshoot'|'unassigned';candidate_id:string|null;locked:boolean;reviewed:boolean;note:string}
export interface SavedChoice {shot_id:string;requirement_hash:string;mode:string;locked:boolean;binding:{name:string;asset_id:string;start_ms:number;end_ms:number|null;reviewed?:boolean}|null;note:string}
export interface Source {document_id:string;revision_id:string|null;head_revision_id:string|null;story_revision_id:string;story_current:boolean;inputs_current:boolean;number:number;content:{choices:SavedChoice[];allow_reuse:boolean;all_material:boolean}|null}
export interface ItemView extends Item {position:number;seconds:number;choice:Choice;old:SavedChoice|null;gapText:string;decisionText:string;excludedText:string;candidates:Array<Candidate&{rangeText:string;reasonsText:string;reviewText:string;observedText:string}>}
export function copy<T>(value:T):T;
export const visualLabels:Record<string,string>;
export function reason(code:string):string;
export function choices(report:Report,source:Source|null):Choice[];
export function sourceBody(report:Report,source:Source|null,rows:Choice[],removedUnlocks?:string[],allowReuse?:boolean):{report_id:string;base_revision_id:string|null;choices:Choice[];unlock_shot_ids:string[];allow_reuse:boolean};
export function emptyAnnotation(id:string):Annotation;
export interface AnnotationDraft {spans:Array<Annotation&{start:string;end:string;tagsText:string}>;note:string;confirmed:boolean}
export function annotationDraft(semantic:Semantic|null,id:string):AnnotationDraft;
export function annotationBody(draft:AnnotationDraft,baseRevision:string|null,seconds:(value:unknown)=>number):{base_revision_id:string|null;note:string;confirmed:boolean;spans:Annotation[]};
export function tags(value:string):string[];
export function override():Override;
export function viewItems(report:Report|null,rows:Choice[],source:Source|null):ItemView[];

export function overrideFor(values:Record<string,Override>,key:string):Override|null;
export function setOverride(values:Record<string,Override>,key:string,value:Override):void;
