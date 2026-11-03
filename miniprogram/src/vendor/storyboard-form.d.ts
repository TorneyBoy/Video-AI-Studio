export interface Visual {subject:string;action:string;scene:string;composition:string;camera:string;lighting:string;style:string}
export interface Shot {id:string;duration_ms:number;intent:string;visual:Visual;constraints:string[];voiceover:string;screen_text:string;sound_notes:string;reference_span_id:string|null;locked:boolean}
export interface Fact {id:string;text:string;confirmed:boolean;locked:boolean}
export interface Content {schema_version:'storyboard/1';title:string;target_duration_ms:number;facts:Fact[];shots:Shot[];status:'draft'|'confirmed'}
export interface Revision {document_id:string;project_id:string;reference_id:string|null;revision_id:string;head_revision_id:string;number:number;content:Content;created_at:string}
export interface Reference {id:string;asset_id:string;asset_name:string;project_id:string;source_current:boolean;allow_output:boolean;evidence:{range:{start_ms:number;end_ms:number};spans:Array<{id:string;start_ms:number;end_ms:number;confidence:number|null;evidence:string;transcript:string}>;warnings:string[];status:string;provenance:{provider:string|null;version:string|null}}}
export const visualFields:Array<keyof Visual>;
export function copy<T>(value:T):T;
export function id():string;
export function shot(duration?:number,span?:string|null):Shot;
export function empty():Content;
export function fromReference(reference:Reference):Content;
export function total(content:Content):Content;
export function seconds(value:unknown):number;
export function validate(content:Content):Content;
export function body(content:Content,base:Revision):{base_revision_id:string;content:Content;unlock_shot_ids:string[];unlock_fact_ids:string[]};
