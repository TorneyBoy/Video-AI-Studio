// Execute actual batch UI/form code offline; no provider or browser shortcuts.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const Forms=require('../web/batch-form.js');assert.equal(fs.readFileSync(require.resolve('../web/batch-form.js'),'utf8'),fs.readFileSync(require.resolve('../miniprogram/src/vendor/batch-form.js'),'utf8'));
let f=Forms.empty();f.shared.template_id='template';f.shared.asset_ids=['a','b'];f=Forms.append(f,Forms.paste(Array.from({length:10},(_,i)=>'商品'+i).join('\n')));assert.equal(f.items.length,10);assert.equal(new Set(f.items.map(row=>row.key)).size,10);assert.deepEqual(Forms.validate(f),[]);
f=Forms.copy(f,[f.items[0].key]);assert.equal(f.items.length,11);assert.throws(()=>Forms.paste('名称\ta\ttemplate\t6\t2:3'));assert.throws(()=>Forms.paste('[{"name":"bad","provider_job_id":"remote"}]'));
const elements=new Map(),values=new Map(),calls=[];let handler;
function element(id){if(!elements.has(id))elements.set(id,{hidden:false,innerHTML:'',value:'',addEventListener(){},querySelector:element,querySelectorAll:()=>[],scrollIntoView(){}});return elements.get(id);}
const template={id:'template',name:'Quick'};
function view(body,id='batch',state='draft'){return{id,project_id:'project',name:body.name,state,revision:1,shared:body.shared,aggregate:{status:state==='draft'?'draft':'running',total:body.items.length,succeeded:0,attention:0},items:body.items.map((row,i)=>({...row,id:'item-'+i,position:i,status:state==='draft'?'draft':'queued',job_id:state==='draft'?null:'job-'+i}))};}
let server;
const quote={id:'quote',batch_id:'batch',revision:1,item_count:10,unique_asset_count:2,expires_at:'2099-01-01T00:00:00Z',can_submit:true,reasons:[],totals:[],items:[],analysis:[],note:'Fixture'};
const context={console,JSON,Object,String,Number,Array,Math,Date,Set,encodeURIComponent,BatchForms:Forms,tenantId:'tenant',selectedProjectId:'project',
 document:{hidden:false,querySelector:element,addEventListener(){}},localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
 sha256:raw=>createHash('sha256').update(raw).digest('hex'),esc:String,friendlyApiError:error=>error.message,moneyText:String,costReason:String,showToast(){},openBilling(){},openPlanFromJob:async()=>{},
 workspaceState:{selectedAssetIds:new Set(['a','b']),assetCache:{}},api:async(path,options={})=>{calls.push({path,...options});return handler(path,options);}};
context.editorContext=()=>`owner:${context.tenantId}:${context.selectedProjectId}`;
vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('../web/batches.js'),'utf8')+'\nthis.ui={batchState,openBatches,batchAction,batchPending,batchChange,batchInput,recoverBatchOperation,clearBatches};',context);const ui=context.ui;
const routine=async(path,options)=>path.startsWith('/batches?')?{items:[],has_more:false,next_skip:0}:path.startsWith('/templates')?[template]:path.endsWith('/validate')?{valid:true,issues:[]}:path.endsWith('/quotes')?quote:path==='/batches'&&options.method==='POST'?(server=view(JSON.parse(options.body))):path==='/batches/batch'?server:{};
(async()=>{
 handler=routine;await ui.openBatches();ui.batchState.cache.form=Forms.clone({...f,items:f.items.slice(0,10)});await ui.batchAction('quote');assert.equal(ui.batchState.quote.id,'quote');assert.equal(server.items.length,10);assert.equal(calls.filter(row=>row.path==='/batches'&&row.method==='POST').length,1);
 ui.batchInput(()=>{ui.batchState.cache.form.name='保留实时输入';});assert.equal(ui.batchState.quote,null);ui.batchChange(()=>{ui.batchState.cache.form.items[0].overrides.target_duration=5;});assert.equal(ui.batchState.cache.form.name,'保留实时输入');assert.equal(ui.batchState.quote,null);assert.ok(ui.batchState.cache.form.items[0].overrides.target_duration===5);
 handler=async(path,options)=>{if(path.endsWith('/configuration'))return{revision:2,head_revision:2};if(path.endsWith('/quotes'))return{...quote,revision:2};return routine(path,options);};await ui.batchAction('quote');assert.equal(ui.batchState.cache.revision,2);
 handler=async()=>{throw new Error('Disconnected');};await ui.batchAction('confirm');const pending=ui.batchPending(),original=calls.at(-1);assert.ok(pending);assert.equal(pending.kind,'confirm');assert.equal(JSON.parse(original.body).quote_id,'quote');
 handler=async()=>({found:false});await ui.recoverBatchOperation();assert.equal(ui.batchPending().key,pending.key);
 handler=async()=>{throw Object.assign(new Error('Permission changed'),{status:403});};await ui.recoverBatchOperation(true);assert.equal(ui.batchPending().key,pending.key);
 server={...server,state:'submitted',revision:2,aggregate:{...server.aggregate,status:'running'}};handler=async(path,options)=>{if(options.method==='POST'){assert.deepEqual({path,...options},original);return{batch_id:'batch'};}return server;};await ui.recoverBatchOperation(true);assert.equal(ui.batchPending(),null);assert.equal(ui.batchState.current.state,'submitted');
 // A late success cannot erase the original operation record or replace another project.
 await ui.batchAction('new');ui.batchState.cache.form=Forms.clone({...f,items:f.items.slice(0,10)});let release;handler=()=>new Promise(resolve=>release=resolve);const saving=ui.batchAction('save');context.selectedProjectId='other';ui.clearBatches();release(view(Forms.body(f)));await saving;assert.equal(ui.batchState.cache,null);context.selectedProjectId='project';assert.equal(ui.batchPending().kind,'create');
 assert.ok([...values.values()].every(value=>!value.includes('https://')&&!value.includes('access_token')));console.log('PASS web batch 10-item configuration, shared rules, CAS, quoted confirmation and unknown/late recovery');
})().catch(error=>{console.error(error);process.exitCode=1;});
