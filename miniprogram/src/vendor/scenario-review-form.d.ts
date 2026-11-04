export type Dimension='facts'|'technical'|'narrative'|'visual'|'audio';
export type Status='unreviewed'|'passed'|'failed'|'not_applicable';
export interface Binding {id:string;version:string;content_hash:string;prompt_version:string}
export interface Profile extends Binding {source_policy?:string;preserve_speech_meaning?:boolean}
export interface DimensionForm {dimension:Dimension;status:Status;score:string;note:string}
export interface Form {identity:string;dimensions:DimensionForm[]}
export interface DimensionRead {dimension:Dimension;status:Status;score?:number|null;note:string;evidence_ids:string[]}
export interface Body {artifact_id:string;output_sha256:string;scenario:Binding;rubric_version:string;dimensions:DimensionRead[]}
export interface Record {id:string;artifact_id:string;stale:boolean;created_at:string;review:Body&{reviewer_id:string};assessment:{verdict:string;commercial_or_release_admission:boolean}}
export interface Context {artifact_id:string;output_sha256:string;scenario:Binding|null;required_dimensions:Dimension[];has_audio:boolean|null;technical_findings:Array<{code:string;severity:string;message:string}>;evidence_ids:string[];reviews:Record[]}
export const dimensions:Array<{id:Dimension;label:string}>;
export const statuses:Array<{id:Status;label:string}>;
export const rubricVersion:string;
export function label(id:string):string;
export function statusLabel(id:string):string;
export function identity(context:Context|null):string;
export function fields(context:Context):Array<{id:Dimension;label:string}>;
export function blank(context:Context):Form;
export function draft(value:Form|null|undefined,context:Context):Form;
export function audioNotApplicable(context:Context,profiles:Profile[]):boolean;
export function choices(context:Context,profiles:Profile[],dimension:Dimension):Array<{id:Status;label:string}>;
export function body(context:Context,value:Form,profiles?:Profile[]):Body;
export function verdict(value:string):string;
