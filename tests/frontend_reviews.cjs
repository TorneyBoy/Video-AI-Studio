// Execute the real review client with offline storage and transport failures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {createHash} = require('node:crypto');
const elements = new Map(); let issueRows = [];
function element(id) {if (!elements.has(id)) elements.set(id, {hidden:false,innerHTML:'',value:'',src:'',currentTime:0,listeners:{},dataset:{},
  addEventListener(name,fn) {this.listeners[name] = fn;}, querySelector: element, querySelectorAll() {return [];}}); return elements.get(id);}
const values = new Map(); const calls = []; const notices = []; let handler;
const first = {artifact_id:'artifact',binding_hash:'a'.repeat(64),binding:{},status:'pending',latest:null,
  can_review:true,can_preview:true,can_download:false,quality_ready:true,quality_scope:'结构质检',reason:'请审阅',
  duration:8,measurements:{width:1080,height:1920},revision_number:1,origin_job_id:null};
const context = {console,JSON,Number,String,Object,Array,Math,encodeURIComponent, authPrincipalId:'owner',tenantId:'tenant',selectedProjectId:'project',
  document:{addEventListener(){},querySelector:element,querySelectorAll(selector) {return selector === '[data-review-issue]' ? issueRows : [];}},
  localStorage:{getItem:key => values.get(key) || null,setItem:(key,value) => values.set(key,value)},
  editorContext() {return `${this.authPrincipalId}:${this.tenantId}:${this.selectedProjectId}`;}, esc:String,
  sha256:raw => createHash('sha256').update(raw).digest('hex'),friendlyApiError:error => error.message,showToast:text => notices.push(text),
  safeRemoteUri:uri => /^https:\/\//.test(uri) ? uri : '', api:async (path,options={}) => {calls.push({path,...options}); return handler(path,options);}};
// Functions called without a receiver still use the current scope values.
context.editorContext = () => `${context.authPrincipalId}:${context.tenantId}:${context.selectedProjectId}`;
vm.createContext(context);
vm.runInContext(fs.readFileSync(require.resolve('../web/scenario-review-form.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(require.resolve('../web/reviews.js'),'utf8') + '\nthis.ui={workReview,openWorkReview,submitWorkReview,refreshWorkReview,sendWorkReview,recoverWorkReview,clearWorkReview,rememberWorkReview,downloadReviewedWork,scenarioWorkReview,submitScenarioWorkReview,sendScenarioWorkReview,recoverScenarioWorkReview,renderScenarioWorkReview};',context);
const ui = context.ui;
const clone = value => JSON.parse(JSON.stringify(value));
function note(text) {element('#workReviewNote').value = text; issueRows = []; ui.rememberWorkReview();}

(async () => {
  handler = async () => clone(first); await ui.openWorkReview('artifact'); note('原始审核意见');
  handler = async () => {throw Object.assign(new Error('Other page reviewed'),{status:409});};
  await ui.submitWorkReview('changes_requested');
  assert.equal(ui.workReview.form.note,'原始审核意见'); assert.equal(ui.workReview.pending,null); assert.equal(ui.workReview.conflict,true);
  const beforeConflict = calls.length; await ui.submitWorkReview('approved'); assert.equal(calls.length,beforeConflict);
  const other = {...first,latest:{id:'other',number:1,decision:'approved',note:'Other',issues:[]}};
  handler = async () => clone(other); await ui.refreshWorkReview();
  assert.equal(ui.workReview.form.note,'原始审核意见'); note('已经重新审阅');
  handler = async () => {throw new Error('Disconnected after submission');};
  await ui.submitWorkReview('approved'); const pending = clone(ui.workReview.pending); const original = calls.at(-1);
  assert.equal(pending.body.base_review_id,'other'); assert.equal(pending.body.note,'已经重新审阅');
  await ui.submitWorkReview('changes_requested'); assert.equal(calls.at(-1),original);
  const beforeDownload = calls.length; await ui.downloadReviewedWork(); assert.equal(calls.length,beforeDownload);
  handler = async () => ({found:false,review:null,current:clone(other)}); await ui.recoverWorkReview(false); assert.equal(ui.workReview.pending.key,pending.key);
  const accepted = {...other,status:'approved',can_download:true,latest:{id:'saved',number:2,decision:'approved',note:'已经重新审阅',issues:[]}};
  handler = async (path,options) => {assert.deepEqual({path,...options},original); return {review:accepted.latest,current:clone(accepted)};};
  await ui.recoverWorkReview(true);
  assert.equal(ui.workReview.pending,null); assert.equal(ui.workReview.report.latest.id,'saved'); assert.equal(ui.workReview.form.note,'');
  handler = async () => ({url:'https://fixture.invalid/media?ephemeral-signature'}); await ui.downloadReviewedWork();
  assert.ok(ui.workReview.downloadUrl.includes('ephemeral-signature'));
  assert.ok(![...values.values()].some(text => /https:|ephemeral-signature/.test(text)),'no signed URLs in review storage');
  note('Quota draft'); const previousSet = context.localStorage.setItem; context.localStorage.setItem = () => {throw new Error('Quota');};
  const beforeQuota = calls.length; await ui.submitWorkReview('approved'); assert.equal(calls.length,beforeQuota); assert.equal(ui.workReview.pending,null);
  assert.equal(ui.workReview.form.note,'Quota draft'); context.localStorage.setItem = previousSet;
  let resolve; handler = () => new Promise(done => {resolve=done;}); const old = ui.refreshWorkReview();
  context.selectedProjectId='new-project'; ui.clearWorkReview(); resolve(clone(first)); await old;
  assert.equal(ui.workReview.report,null); assert.equal(element('#workReviewPanel').hidden,true);
  Object.assign(context,{workspaceState:{},renderBelongsToEditor:()=>true,providerSnapshot:()=>({}),formatUsage:()=>'',formatCost:()=>'',statusLabel:()=> '已完成',renderProviderMeta:()=>''});
  element('#previewVideo').pause=()=>{};element('#previewVideo').removeAttribute=()=>{element('#previewVideo').src='';};
  vm.runInContext(fs.readFileSync(require.resolve('../web/workspace.js'),'utf8')+'\nthis.loadArtifact=loadWorkspaceArtifact;',context);
  let finishOld;
  handler=async(path,options)=>{
    if(path==='/artifacts?job_id=old-job')return new Promise(done=>{finishOld=done;});
    if(path==='/artifacts?job_id=new-job')return[{id:'new-artifact',metadata:{},kind:'render'}];
    if(path==='/artifacts/new-artifact/review')return{...clone(first),artifact_id:'new-artifact'};
    if(path==='/artifacts/new-artifact/access'){assert.equal(JSON.parse(options.body).purpose,'preview');return{url:'https://fixture.invalid/new-video'};}
    throw new Error('Unexpected '+path);
  };
  const oldView=context.loadArtifact({id:'old-job',project_id:'new-project',status:'succeeded'},'high','old-artifact');
  await context.loadArtifact({id:'new-job',project_id:'new-project',status:'succeeded'},'high','new-artifact');
  finishOld([{id:'old-artifact',metadata:{},kind:'render'}]);await oldView;
  assert.equal(element('#previewVideo').src,'https://fixture.invalid/new-video');assert.equal(ui.workReview.artifactId,'new-artifact');
  assert.ok(![...values.values()].some(text => text.includes('access_token')));
  context.friendlyApiError=error=>error.message;
  // Scenario reviews capture explicit human judgments without implying release approval.
  const Scenario=require('../web/scenario-review-form.js');assert.equal(fs.readFileSync(require.resolve('../web/scenario-review-form.js'),'utf8'),fs.readFileSync(require.resolve('../miniprogram/src/vendor/scenario-review-form.js'),'utf8'));
  const binding={id:'product_showcase',version:'v1',content_hash:'b'.repeat(64),prompt_version:'prompt-v1'},profile={...binding,source_policy:'real_first',preserve_speech_meaning:false},quality={artifact_id:'artifact',output_sha256:'c'.repeat(64),scenario:binding,required_dimensions:['facts','technical','narrative','visual','audio'],has_audio:false,technical_findings:[{code:'blur_detected',severity:'warning',message:'请核对清晰度'}],evidence_ids:['artifact'],reviews:[]};
  const initial=Scenario.blank(quality);assert.ok(initial.dimensions.every(row=>row.status==='unreviewed'&&row.score===''));assert.ok(Scenario.body(quality,initial,[profile]).dimensions.every(row=>row.status==='unreviewed'&&!('score' in row)&&row.evidence_ids.length===0));
  const invalid=structuredClone(initial);invalid.dimensions[0].score='5';assert.throws(()=>Scenario.body(quality,invalid,[profile]),/不能填写评分/);invalid.dimensions[0].status='passed';invalid.dimensions[0].score='6';assert.throws(()=>Scenario.body(quality,invalid,[profile]),/0至5/);
  assert.equal(Scenario.audioNotApplicable({...quality,has_audio:null},[profile]),false);assert.equal(Scenario.audioNotApplicable(quality,[{...profile,source_policy:'speech_first'}]),false);assert.equal(Scenario.audioNotApplicable(quality,[{...profile,preserve_speech_meaning:true}]),false);assert.equal(Scenario.audioNotApplicable(quality,[]),false);
  assert.ok(Scenario.draft(initial,{...quality,output_sha256:'d'.repeat(64)}).dimensions.every(row=>row.status==='unreviewed'&&row.score===''));
  context.selectedProjectId='project';const sceneReport={...clone(first),project_id:'project'};let qualityMode='get';let savedQuality;
  handler=async(path,options)=>{if(path.endsWith('/review'))return sceneReport;if(path.endsWith('/quality/scenarios'))return {profiles:[profile]};if(path.endsWith('/quality/artifacts/artifact'))return quality;throw new Error('Unexpected '+path);};await ui.openWorkReview('artifact');assert.ok(ui.scenarioWorkReview.form.dimensions.every(row=>row.status==='unreviewed'));assert.match(ui.renderScenarioWorkReview(),/实际|真实人工/);assert.match(ui.renderScenarioWorkReview(),/blur_detected/);assert.match(ui.renderScenarioWorkReview(),/不代表商业许可/);
  ui.scenarioWorkReview.form.dimensions[0]={dimension:'facts',status:'passed',score:'4.5',note:'已逐帧核对商品标识'};ui.scenarioWorkReview.form.dimensions[4]={dimension:'audio',status:'not_applicable',score:'',note:'测量为无音轨，当前场景无需口播'};
  handler=async()=>{throw new Error('Disconnected after quality write');};await ui.submitScenarioWorkReview();const qualityOriginal=calls.at(-1),qualityPending=clone(ui.scenarioWorkReview.pending);assert.equal(JSON.parse(qualityOriginal.body).dimensions[0].score,4.5);assert.equal(JSON.parse(qualityOriginal.body).dimensions[1].status,'unreviewed');assert.equal(JSON.parse(qualityOriginal.body).reviewer_id,undefined);assert.equal(qualityPending.body.scenario.content_hash,binding.content_hash);
  const beforeSceneBlock=calls.length;await ui.submitWorkReview('approved');await ui.downloadReviewedWork();assert.equal(calls.length,beforeSceneBlock);
  handler=async()=>{throw Object.assign(new Error('Rejected repeat'),{status:409});};await ui.recoverScenarioWorkReview(true);assert.deepEqual(calls.at(-1),qualityOriginal);assert.equal(ui.scenarioWorkReview.pending.key,qualityPending.key,'unknown original remains after repeated 4xx');
  handler=async()=>({found:false,result:null});await ui.recoverScenarioWorkReview(false);assert.equal(ui.scenarioWorkReview.pending.key,qualityPending.key);assert.ok(calls.at(-1).path.endsWith('/reviews/requests/'+encodeURIComponent(qualityPending.key)));
  savedQuality={id:'quality-review',artifact_id:'artifact',review:qualityPending.body,assessment:{verdict:'needs_review'},stale:false,created_at:'now'};
  handler=async(path,options)=>{if(path.endsWith('/quality/reviews')){assert.deepEqual({path,...options},qualityOriginal);return savedQuality;}if(path.endsWith('/review'))throw new Error('Report refresh failed');throw new Error('Unexpected '+path);};await ui.recoverScenarioWorkReview(true);assert.equal(ui.scenarioWorkReview.pending,null);assert.equal(ui.scenarioWorkReview.staleReport,true);const staleCalls=calls.length;await ui.submitWorkReview('approved');await ui.downloadReviewedWork();assert.equal(calls.length,staleCalls,'a failed old-report refresh cannot reuse the old release binding');
  handler=async path=>path.endsWith('/review')?{...sceneReport,binding_hash:'e'.repeat(64)}:path.endsWith('/quality/scenarios')?{profiles:[profile]}:{...quality,reviews:[savedQuality]};await ui.refreshWorkReview();assert.equal(ui.workReview.report.binding_hash,'e'.repeat(64));assert.equal(ui.scenarioWorkReview.staleReport,false);assert.match(ui.renderScenarioWorkReview(),/仍需审阅/);assert.notEqual(ui.workReview.report.status,'approved','saving a scenario review never approves release');
  const quotaSet=context.localStorage.setItem;context.localStorage.setItem=()=>{throw new Error('Quota');};const beforeSceneQuota=calls.length;await ui.submitScenarioWorkReview();assert.equal(calls.length,beforeSceneQuota);assert.equal(ui.scenarioWorkReview.pending,null);context.localStorage.setItem=quotaSet;
  let cacheWrites=0;const cacheStore=context.localStorage.setItem;context.localStorage.setItem=(key,value)=>{cacheWrites++;if(cacheWrites===3)throw new Error('Quota after confirmed write');cacheStore(key,value);};handler=async path=>path.endsWith('/quality/reviews')?savedQuality:path.endsWith('/review')?{...sceneReport,binding_hash:'e'.repeat(64)}:path.endsWith('/quality/scenarios')?{profiles:[profile]}:{...quality,reviews:[savedQuality]};await ui.submitScenarioWorkReview();assert.ok(ui.scenarioWorkReview.pending,'confirmed write with failed local cleanup retains recovery metadata');const afterCleanupFailure=calls.length;await ui.submitWorkReview('approved');await ui.downloadReviewedWork();assert.equal(calls.length,afterCleanupFailure,'failed local cleanup cannot unlock old release actions');context.localStorage.setItem=cacheStore;handler=async path=>path.includes('/reviews/requests/')?{found:true,result:savedQuality}:path.endsWith('/review')?{...sceneReport,binding_hash:'e'.repeat(64)}:path.endsWith('/quality/scenarios')?{profiles:[profile]}:{...quality,reviews:[savedQuality]};await ui.recoverScenarioWorkReview(false);assert.equal(ui.scenarioWorkReview.pending,null);
  let finishQuality;handler=()=>new Promise(done=>finishQuality=done);const lateQuality=ui.submitScenarioWorkReview();const retained=clone(ui.scenarioWorkReview.pending);context.selectedProjectId='other-project';ui.clearWorkReview();finishQuality(savedQuality);await lateQuality;assert.equal(ui.scenarioWorkReview.data,null);assert.ok([...values.values()].some(raw=>JSON.parse(raw).pending?.key===retained.key),'late quality success cannot discard the original recovery metadata');
  console.log('web review receipts/drafts/context: PASS (offline; no media/cloud execution)');
})().catch(error => {console.error(error);process.exitCode=1;});
