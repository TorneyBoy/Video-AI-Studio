// Real authoring helpers/controller with offline DOM/transport; no provider call.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const Forms=require('../web/storyboard-form.js');
assert.equal(fs.readFileSync(require.resolve('../web/storyboard-form.js'),'utf8'),fs.readFileSync(require.resolve('../miniprogram/src/vendor/storyboard-form.js'),'utf8'));
assert.equal(Forms.seconds('1.001'),1001);assert.throws(()=>Forms.seconds('1.0001'));assert.throws(()=>Forms.seconds('NaN'));
let draft=Forms.empty();draft.shots[0].intent='展示商品';draft.shots[0].visual.subject='自己的蓝色商品';draft.status='confirmed';Forms.validate(draft);
assert.throws(()=>Forms.validate({...draft,target_duration_ms:1}));
const values=new Map(),elements=new Map(),calls=[];let handler;
function element(id){if(!elements.has(id))elements.set(id,{hidden:false,innerHTML:'',addEventListener(){},querySelector:element,querySelectorAll:()=>[],play:async()=>{},pause(){}});return elements.get(id);}
const context={console,JSON,Object,String,Number,Array,Math,Date,Set,StoryForms:Forms,encodeURIComponent,
selectedProjectId:'project',tenantId:'tenant',authPrincipalId:'owner',window:{confirm:()=>true},
document:{querySelector:element},localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)},
esc:text=>String(text).replaceAll('<','&lt;'),friendlyApiError:error=>error.message,showToast:()=>{},
assetType:row=>row.kind,assetIsReady:()=>true,api:async(path,options)=>{calls.push({path,options});return handler(path,options);}};
context.editorContext=()=>context.authPrincipalId+':'+context.tenantId+':'+context.selectedProjectId;
vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('../web/storyboards.js'),'utf8')+'\nthis.ui={storyState,storySend,storyPending,storyOperation,recoverStoryOperation,storyPersist,openStory,clearStoryboards,renderStoryboards,loadStoryboards};',context);
const ui=context.ui;
(async()=>{
  ui.storyState.context=context.editorContext();ui.storyState.draft=Forms.copy(draft);
  const op=ui.storyOperation('create','/projects/project/storyboards','/projects/project/storyboard-requests/',{reference_id:null,content:draft});
  handler=async()=>{throw Error('Disconnected');};await ui.storySend(op);assert.equal(ui.storyPending().key,op.key);const original=calls.at(-1);
  handler=async()=>({found:false});await ui.recoverStoryOperation();assert.equal(ui.storyPending().key,op.key);
  const first={document_id:'doc',revision_id:'v1',head_revision_id:'v1',number:1,reference_id:null,content:draft};
  handler=async(path)=>path.includes('/storyboard-requests/')?{found:false}:first;await ui.recoverStoryOperation(true);
  assert.deepEqual(calls.at(-1),original);assert.equal(ui.storyPending(),null);assert.equal(ui.storyState.base.revision_id,'v1');
  const changed=Forms.copy(draft);changed.shots[0].voiceover='<img src=x onerror=alert(1)>';
  ui.storyState.draft=changed;const save=ui.storyOperation('save','/storyboards/doc/revisions','/storyboards/doc/requests/',Forms.body(changed,first));
  handler=async()=>{throw Error('Disconnected');};await ui.storySend(save);
  handler=async()=>({found:true,result:{...first,revision_id:'v2',head_revision_id:'v3',number:2,content:changed}});await ui.recoverStoryOperation();
  assert.equal(ui.storyState.draft.shots[0].voiceover,changed.shots[0].voiceover);assert.equal(ui.storyState.base.revision_id,'v1');assert.match(ui.storyState.notice,/更新版本/);
  ui.renderStoryboards();assert.ok(!element('#storyboardPanel').innerHTML.includes('<img src=x'));assert.match(element('#storyboardPanel').innerHTML,/&lt;img/);
  let complete;handler=()=>new Promise(resolve=>complete=resolve);const delayed=ui.openStory('doc');context.selectedProjectId='other-project';ui.clearStoryboards();complete(first);await delayed;assert.equal(ui.storyState.base,null);
  assert.ok([...values.values()].every(value=>!value.includes('temporary=')&&!value.includes('access_token')));
  console.log('PASS web storyboard duration, original-request recovery, historical receipt, escaping and context fencing');
})().catch(error=>{console.error(error);process.exitCode=1;});
