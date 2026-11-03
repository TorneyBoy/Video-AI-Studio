// Execute actual quote UI; all requests are offline metadata fixtures.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const values=new Map(),elements=new Map(),calls=[],plans=[];let handler;
function element(id){if(!elements.has(id))elements.set(id,{hidden:false,innerHTML:'',value:'',listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},querySelector:element,scrollIntoView(){}});return elements.get(id);}
const quote={id:'quote',kind:'analysis',project_id:'project',expires_at:'2099-01-01T00:00:00Z',can_submit:true,reasons:[],totals:[{currency:'CNY',amount_micros:'500000'}],items:[{asset_id:'a',name:'Source',disposition:'new',amount_micros:'500000',currency:'CNY'}],note:'Not final bill'};
const context={console,JSON,String,Number,Object,Array,Math,Date,encodeURIComponent,tenantId:'tenant',selectedProjectId:'project',document:{hidden:false,querySelector:element,addEventListener(){}},
 localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
 esc:String,sha256:raw=>createHash('sha256').update(raw).digest('hex'),friendlyApiError:error=>error.message,showToast(){},moneyText:(amount,currency)=>currency+' '+amount,costReason:String,
 workspaceState:{selectedAssetIds:new Set(['a']),assets:[{id:'a',kind:'video'}],assetCache:{},selectedTemplateId:'template'},assetIsReady:()=>true,pendingOperations:()=>[],openBilling(){},loadJobs:async()=>{},loadWorkspace:async()=>{},
 submitTrackedOperation:async record=>plans.push(record),api:async(path,options={})=>{calls.push({path,...options});return handler(path,options);}};
context.editorContext=()=>`owner:${context.tenantId}:${context.selectedProjectId}`;
vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('../web/preparation.js'),'utf8')+'\nthis.ui={preparationState,preparationPending,openAnalysisQuote,confirmPreparation,recoverPreparation,openPreparation,clearPreparation};',context);
const ui=context.ui;
(async()=>{
 handler=async()=>quote;await ui.openAnalysisQuote();assert.equal(calls.length,1);assert.ok(calls[0].path.endsWith('analysis-quotes'));assert.ok(element('#preparationPanel').innerHTML.includes('新增分析'));
 context.workspaceState.selectedAssetIds=new Set(['changed']);await ui.confirmPreparation();assert.equal(calls.length,1);assert.equal(ui.preparationState.quote,null);
 context.workspaceState.selectedAssetIds=new Set(['a']);element('[data-preparation-setting="transcribe"]').listeners.change({target:{checked:false}});assert.equal(ui.preparationState.quote,null);await ui.openAnalysisQuote();assert.equal(JSON.parse(calls.at(-1).body).transcribe,false);handler=async()=>{throw new Error('Disconnected');};await ui.confirmPreparation();const original=calls.at(-1),pending=ui.preparationPending();assert.ok(pending);assert.equal(original.body,'{}');
 handler=async()=>({found:false});await ui.recoverPreparation(false);assert.equal(ui.preparationPending().key,pending.key);
 handler=async()=>{throw Object.assign(new Error('Permission changed'),{status:403});};await ui.recoverPreparation(true);assert.equal(ui.preparationPending().key,pending.key);
 handler=async(path,options)=>{assert.deepEqual({path,...options},original);return{items:[{asset_id:'a',job_id:'job'}]};};await ui.recoverPreparation(true);assert.equal(ui.preparationPending(),null);
 handler=async()=>quote;await ui.openPreparation('plan',{project_id:'project',template_id:'template',asset_ids:['a']});await ui.confirmPreparation();assert.equal(plans[0].body.quote_id,'quote');assert.equal(plans[0].key,'plan:quote:quote');
 let resolve;handler=()=>new Promise(r=>resolve=r);const waiting=ui.openAnalysisQuote();context.selectedProjectId='other';ui.clearPreparation();resolve(quote);await waiting;assert.equal(ui.preparationState.quote,null);assert.equal(ui.preparationPending(),null);
 assert.ok([...values.values()].every(value=>!value.includes('http')&&!value.includes('token')));console.log('PASS actual web preparation quotes, changed inputs, original receipts and late context guards');
})().catch(error=>{console.error(error);process.exitCode=1;});
