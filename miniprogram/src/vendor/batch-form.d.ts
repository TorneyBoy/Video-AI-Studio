export interface Settings {template_id?:string|null;target_duration?:number|null;aspect_ratio?:'9:16'|'16:9'|'1:1'|null;asset_ids?:string[]}
export interface Item {key:string;name:string;overrides:Settings}
export interface Draft {name:string;shared:Settings & {template_id:string|null;target_duration:number|null;aspect_ratio:'9:16'|'16:9'|'1:1'|null;asset_ids:string[]};items:Item[]}
export interface Issue {key:string|null;field:string;message:string}
export function empty():Draft;
export function body(form:Draft):Draft;
export function append(form:Draft,rows:Array<{name:string;overrides?:Settings}>):Draft;
export function copy(form:Draft,keys:string[]):Draft;
export function effective(form:Draft,row:Item):Draft['shared'];
export function paste(text:string):Array<{name:string;overrides:Settings}>;
export function duration(value:string):number|null;
export function clone<T>(value:T):T;
export function validate(form:Draft):Issue[];
