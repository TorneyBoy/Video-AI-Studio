// Actual fixed-wave component with offline responses; no supplier or media calls.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const values=new Map(),elements=new Map(),calls=[],downloads=[],events={};let handler,external=false;
function element(id){if(!elements.has(id))elements.set(id,{hidden:false,innerHTML:'',scrollIntoView(){},addEventListener(){},querySelector:element,querySelectorAll:()=>[]});return elements.get(id);}
const sha=value=>createHash('sha256').update(value).digest('hex');
const view={batch_id:'batch',batch_name:'Fixed products',project_id:'project',run_id:'run',quality:'high',created_at:'2026-10-18T00:00:00Z',total:3,counts:{pending_review:2,failed:1},configuration:{available:true,note:'Fixture'},items:Array.from({length:3},(_,i)=>({key:'item-'+i,name:'Product '+i,revision_number:i+1,artifact_id:i<2?'artifact-'+i:null,job_id:'render-'+i,work:{state:i<2?'pending_review':'failed',reason:i<2?'Needs review':'Render failed'}}))};
const prepared={batch_id:'batch',run_id:'run',can_review:true,items:view.items.map(row=>({...row,report:row.artifact_id?{can_review:true,can_preview:true,binding_hash:sha(row.artifact_id),latest:null}:null}))};
const manifest={selected_count:3,included_count:2,excluded_count:1,video_bytes:40,include_subtitles:false,items:view.items.map((row,i)=>({...row,included:i<2,filename:'00'+(i+1)+'-'+row.artifact_id+'.mp4',reason:i<2?null:'Render failed'}))};
const content=JSON.stringify(manifest),preview={batch_id:'batch',run_id:'run',manifest,manifest_hash:sha(content),can_package:true,reasons:[],configuration:view.configuration,file:{filename:'batch-delivery-run.json',content,sha256:sha(content)}};
const context={console,JSON,Object,String,Number,Array,Math,Date,Set,encodeURIComponent,Blob,setTimeout:fn=>fn(),selectedProjectId:'project',
 document:{hidden:false,querySelector:element,addEventListener:(name,fn)=>events[name]=fn,body:{appendChild(){}},createElement:()=>({click(){downloads.push({url:this.href,filename:this.download});},remove(){}})},
 URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL(){}},
 localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
 sha256:sha,esc:String,friendlyApiError:error=>error.message,showToast:message=>context.notice=message,
 pendingBatchRun:()=>null,pendingOperations:()=>external?[{}]:[],preparationPending:()=>null,batchPending:()=>null,
 api:async(path,options={})=>{calls.push({path,...options});return handler(path,options);}};
context.editorContext=()=>`owner:tenant:${context.selectedProjectId}`;vm.createContext(context);
vm.runInContext(fs.readFileSync(require.resolve('../web/batch-delivery.js'),'utf8')+'\nthis.ui={batchDeliveryState,openBatchDelivery,clearBatchDelivery,batchDeliveryAction,pendingBatchDelivery,recoverBatchDelivery,downloadBatchPackage};',context);const ui=context.ui;
const routine=async(path)=>path.includes('/delivery?')?structuredClone(view):path.endsWith('/packages')?{items:[],has_more:false,next_skip:0}:path.endsWith('/review-prepare')?structuredClone(prepared):path.endsWith('/delivery-preview')?structuredClone(preview):{};
(async()=>{
 handler=routine;await ui.openBatchDelivery('batch','run');await ui.batchDeliveryAction('all');await ui.batchDeliveryAction('prepare');assert.equal(ui.batchDeliveryState.selected.size,3);
 external=true;const before=calls.length;await ui.batchDeliveryAction('review');assert.equal(calls.length,before);assert.match(context.notice,/原提交/);external=false;
 handler=async()=>{throw new Error('Disconnected');};await ui.batchDeliveryAction('review');const pending=ui.pendingBatchDelivery(),original=calls.at(-1),body=JSON.parse(original.body);assert.ok(pending);assert.equal(body.items.length,2);assert.equal(body.items[1].artifact_id,'artifact-1');assert.equal(body.items[1].review.binding_hash,sha('artifact-1'));assert.equal(body.items[1].review.base_review_id,null);
 handler=async()=>({found:false});await ui.recoverBatchDelivery();assert.equal(ui.pendingBatchDelivery().key,pending.key);
 handler=async()=>{throw Object.assign(new Error('Forbidden'),{status:403});};await ui.recoverBatchDelivery(true);assert.equal(ui.pendingBatchDelivery().key,pending.key);
 handler=async(path,options)=>{if(options.method==='POST'){assert.deepEqual({path,...options},original);return{items:[{applied:true},{applied:false}]};}return routine(path);};await ui.recoverBatchDelivery(true);assert.equal(ui.pendingBatchDelivery(),null);
 handler=routine;await ui.batchDeliveryAction('preview');assert.equal(ui.batchDeliveryState.preview.manifest.excluded_count,1);await ui.batchDeliveryAction('manifest');assert.deepEqual(downloads.at(-1),{url:'blob:fixture',filename:'batch-delivery-run.json'});
 ui.batchDeliveryState.preview.file.content='tampered';const prior=downloads.length;await ui.batchDeliveryAction('manifest');assert.equal(downloads.length,prior);assert.match(ui.batchDeliveryState.notice,/校验失败/);
 await ui.batchDeliveryAction('preview');handler=async(path,options)=>options.method==='POST'?{package_id:'package',job_id:'job',disposition:'reused'}:routine(path);await ui.batchDeliveryAction('package');assert.match(ui.batchDeliveryState.notice,/复用/);assert.equal(ui.pendingBatchDelivery(),null);
 handler=async()=>({package_id:'package',url:'https://fixture.invalid/zip?signature=short',filename:'other.zip'});await ui.downloadBatchPackage('package');assert.equal(downloads.length,prior);
 handler=async()=>({package_id:'package',url:'https://fixture.invalid/zip?signature=short',filename:'batch-package.zip'});await ui.downloadBatchPackage('package');assert.equal(downloads.at(-1).filename,'batch-package.zip');
 handler=async(path)=>path.endsWith('/delivery-preview')?structuredClone(preview):({artifact_id:path.includes('artifact-0')?'wrong':'artifact-1',url:'https://fixture.invalid/video?signature=short'});await ui.batchDeliveryAction('files');assert.match(ui.batchDeliveryState.notice,/已发起 1 项下载，1 项未完成/);
 // A late success while hidden must retain the original operation for recovery.
 handler=routine;await ui.batchDeliveryAction('prepare');let release;handler=()=>new Promise(resolve=>release=resolve);const sending=ui.batchDeliveryAction('review');context.document.hidden=true;events.visibilitychange();release({items:[{applied:true}]});await sending;assert.ok(ui.pendingBatchDelivery());assert.equal(ui.batchDeliveryState.prepared,null);
 const originalContext=context.editorContext();context.selectedProjectId='other';ui.clearBatchDelivery();assert.equal(ui.pendingBatchDelivery(),null);context.selectedProjectId='project';assert.ok(ui.pendingBatchDelivery());values.delete('video-ai-studio.batch-delivery-operation.v1.'+originalContext);
 handler=async(path)=>path.includes('/delivery?')?{...view,project_id:'other'}:routine(path);await ui.openBatchDelivery('different','run');assert.equal(ui.batchDeliveryState.view,null);assert.equal(ui.batchDeliveryState.packages.length,0);assert.match(ui.batchDeliveryState.notice,/所属项目/);
 assert.ok([...values.values()].every(value=>!value.includes('https://')&&!value.includes('signature=')));console.log('PASS web fixed output selection, immutable recovery, partial receipts, manifest hash, package reuse and late/context fences');
})().catch(error=>{console.error(error);process.exitCode=1;});
