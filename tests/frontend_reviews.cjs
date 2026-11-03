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
vm.runInContext(fs.readFileSync(require.resolve('../web/reviews.js'),'utf8') + '\nthis.ui={workReview,openWorkReview,submitWorkReview,refreshWorkReview,sendWorkReview,recoverWorkReview,clearWorkReview,rememberWorkReview,downloadReviewedWork};',context);
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
  console.log('web review receipts/drafts/context: PASS (offline; no media/cloud execution)');
})().catch(error => {console.error(error);process.exitCode=1;});
