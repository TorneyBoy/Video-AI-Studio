// Real billing UI with offline transport: prices, original actions and context.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const values=new Map(),elements=new Map(),calls=[],submissions=[],notices=[];let handler;
function element(id){if(!elements.has(id))elements.set(id,{hidden:false,innerHTML:'',value:'',listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},querySelector:element,querySelectorAll:()=>[],scrollIntoView(){}});return elements.get(id);}
const job={id:'job',project_id:'project',type:'render.high',status:'failed',attempt_count:1,execution_generation:1,max_attempts:3,can_retry:true,can_cancel:false};
const summary={scope:'project_lifetime',period_key:'2026-10',note:'Fixture metadata only',totals:[],tenant_policy:{currency:'CNY',monthly_limit_micros:'1000000',task_limit_micros:'600000',remaining_micros:'1000000'},project_policy:{project_id:'project',revision:1,currency:'CNY',limit_micros:'600000',remaining_micros:'600000',can_manage:true,note:'Fixture'}};
const context={console,JSON,Number,String,Object,Array,Math,Date,encodeURIComponent,tenantId:'tenant',selectedProjectId:'project',document:{hidden:false,querySelector:element,addEventListener(){}},
  localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
  esc:String,sha256:raw=>createHash('sha256').update(raw).digest('hex'),friendlyApiError:error=>error.message,showToast:notice=>notices.push(notice),statusLabel:String,
  editorBlocked:()=>false,editorMatches:()=>true,workspaceState:{planJob:{id:'origin'}},planEditor:{revision:{document_id:'doc',revision_id:'revision',number:1}},
  submitTrackedOperation:async record=>submissions.push(record),api:async(path,options={})=>{calls.push({path,...options});return handler(path,options);}};
context.editorContext=()=>`owner:${context.tenantId}:${context.selectedProjectId}`;
vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('../web/billing.js'),'utf8')+'\nthis.ui={billingState,renderQuoteState,microText,parseLimitText,openBilling,startBillingAction,checkBillingAction,sendBillingAction,clearBilling,openRenderQuote,confirmRenderQuote,saveProjectLimit};',context);
const ui=context.ui;
const routine=async path=>path.startsWith('/billing/overview')?summary:path.startsWith('/billing/entries')?{items:[{id:'unknown',job_id:'job',job_type:'qa',job_status:'failed',status:'pending_reconciliation',status_label:'待对账',currency:'CNY',amount_micros:'0',adjustment_micros:'0',net_settled_micros:null,is_estimate:true}],next_skip:1,has_more:false}:{items:[job],next_skip:1,has_more:false};
(async()=>{
  assert.equal(ui.microText('9007199254740993'),'9,007,199,254.740993');assert.equal(ui.microText('-1'),'-0.000001');
  assert.equal(ui.parseLimitText('1.000001'),1000001);assert.equal(ui.parseLimitText(''),null);assert.throws(()=>ui.parseLimitText('9007199254.740993'));
  handler=routine;await ui.openBilling();assert.ok(element('#billingPanel').innerHTML.includes('金额待核对'));
  handler=async(path,options)=>{if(options.method==='POST')throw new Error('Disconnected');return routine(path);};
  await ui.startBillingAction('job','retry');const pending=ui.billingState.pending,original=calls.findLast(row=>row.path.endsWith('/actions'));
  assert.equal(pending.body.expected_generation,1);assert.ok([...values.values()].some(value=>value.includes(pending.key)));
  handler=async(path,options)=>{if(path.endsWith('/actions/'+encodeURIComponent(pending.key)))return{found:false,current:job};return routine(path);};
  await ui.checkBillingAction();assert.equal(ui.billingState.pending.key,pending.key);
  handler=async(path,options)=>{if(options.method==='POST')throw Object.assign(new Error('Permission changed after unknown request'),{status:403});return routine(path);};
  await ui.sendBillingAction(pending,true);assert.equal(ui.billingState.pending.key,pending.key);
  handler=async(path,options)=>{if(options.method==='POST'){assert.equal(JSON.stringify({path,...options}),JSON.stringify(original));return{found:true,current:{...job,status:'queued'}};}return routine(path);};
  await ui.sendBillingAction(pending,true);assert.equal(ui.billingState.pending,null);
  // Conditional project updates keep original form and revision after conflict.
  Object.assign(ui.billingState,{limitDirty:true,limitDraft:'0.4',limitCurrency:'CNY',limitBase:1});element('#projectLimit').value='0.4';element('#projectCurrency').value='CNY';
  handler=async(path,options)=>{if(path.endsWith('/spending-limit')){assert.equal(JSON.parse(options.body).base_revision,1);throw Object.assign(new Error('Conflict'),{status:409});}return{...await routine(path),...(path.startsWith('/billing/overview')?{project_policy:{...summary.project_policy,revision:2}}:{})};};
  await ui.saveProjectLimit();assert.equal(ui.billingState.limitDraft,'0.4');assert.equal(ui.billingState.limitBase,1);assert.equal(ui.billingState.summary.project_policy.revision,2);
  const quote={id:'quote',project_id:'project',summary:{revision_number:1,duration:12,width:1080,height:1920},amount_micros:'500000',currency:'CNY',price_version:'fixture-1',provider:'fixture',model_id:'model',model_version:'1',duration_bound_seconds:30,expires_at:'2099-01-01T00:00:00Z',can_submit:true,reasons:[],note:'Not a bill'};
  handler=async()=>quote;await ui.openRenderQuote('high');assert.equal(submissions.length,0);
  context.planEditor.revision.revision_id='new-revision';await ui.confirmRenderQuote();assert.equal(submissions.length,0);
  context.planEditor.revision.revision_id='revision';await ui.confirmRenderQuote();assert.equal(submissions.length,1);assert.equal(submissions[0].body.quote_id,'quote');assert.equal(submissions[0].key,'render:quote:quote');
  let finish;handler=()=>new Promise(resolve=>{finish=resolve;});const late=ui.openRenderQuote('draft');context.selectedProjectId='other';ui.clearBilling();finish(quote);await late;
  assert.equal(ui.renderQuoteState.quote,null);assert.equal(element('#renderQuotePanel').hidden,true);assert.ok(![...values.values()].some(value=>/https:|access_token|provider_job_id/.test(value)));
  console.log('web billing quotes/money/original actions/CAS/context: PASS (offline; no paid calls)');
})().catch(error=>{console.error(error);process.exitCode=1;});
