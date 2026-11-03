// Execute real delivery and public-page scripts with offline transport/DOM.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const values=new Map(),elements=new Map(),calls=[],downloads=[],notices=[];let handler;
function element(id){if(!elements.has(id))elements.set(id,{hidden:false,innerHTML:'',textContent:'',value:'',src:'',currentTime:0,checked:false,listeners:{},
  addEventListener(name,fn){this.listeners[name]=fn;},querySelector:element,querySelectorAll:()=>[],scrollIntoView(){},pause(){},removeAttribute(name){this[name]='';},
  replaceChildren(...children){this.children=children;},click(){downloads.push(this);}});return elements.get(id);}
const hash=raw=>createHash('sha256').update(raw).digest('hex');
const context={console,JSON,Number,String,Object,Array,Math,Date,encodeURIComponent,Blob,setTimeout:fn=>fn(),
  selectedProjectId:'project',tenantId:'tenant',authPrincipalId:'owner',workReview:{},
  document:{querySelector:element,querySelectorAll:()=>[],createElement:()=>({...element('new-anchor')})},
  localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)},
  navigator:{clipboard:{writeText:async()=>{}}},URL:{createObjectURL:()=> 'blob:offline',revokeObjectURL(){}},
  sha256:hash,esc:raw=>String(raw).replaceAll('<','&lt;'),friendlyApiError:error=>error.message,showToast:text=>notices.push(text),
  safeRemoteUri:uri=>/^https:\/\//.test(uri)?uri:'',api:async(path,options={})=>{calls.push({path,...options});return handler(path,options);}};
context.editorContext=()=>`${context.authPrincipalId}:${context.tenantId}:${context.selectedProjectId}`;
vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('../web/delivery.js'),'utf8')+
  '\nthis.ui={deliveryCenter,workComparison,openDeliveryCenter,sendDeliveryOp,recoverDeliveryOp,clearDeliveryCenter,exportDeliveryFile,clearWorkCenter,viewComparisonSide};',context);
const ui=context.ui,report={artifact_id:'artifact',binding_hash:'a'.repeat(64),latest:{id:'approved'},can_review:true,can_download:true,revision_number:1};
const share={id:'share',label:'客户审阅',audience:'workspace',expires_at:'later',status:'active',url:'https://fixture.invalid/share#sg1.ephemeral'};
(async()=>{
  handler=async endpoint=>endpoint.endsWith('/review')?report:endpoint==='/delivery/config'?{sharing:{available:true}}:[];
  await ui.openDeliveryCenter('artifact');
  const row={kind:'create',artifactId:'artifact',context:context.editorContext(),key:'original-share-intent',body:{binding_hash:report.binding_hash,review_id:'approved'}};
  handler=async()=>{throw new Error('Disconnected');};await ui.sendDeliveryOp(row);const original=calls.at(-1);
  assert.equal(ui.deliveryCenter.pending.key,row.key);
  handler=async()=>({found:false,share:null});await ui.recoverDeliveryOp(false);assert.equal(ui.deliveryCenter.pending.key,row.key);
  handler=async(path,options)=>{if(path.endsWith('/shares')){assert.deepEqual({path,...options},original);return share;}return[];};
  await ui.recoverDeliveryOp(true);assert.equal(ui.deliveryCenter.pending,null);assert.equal(ui.deliveryCenter.url,share.url);
  assert.ok(![...values.values()].some(text=>/https:|ephemeral|blob:/.test(text)));
  const content='中文交付\n',file={artifact_id:'artifact',binding_hash:report.binding_hash,filename:'delivery-artifact-internal.json',content,sha256:hash(content)};
  handler=async()=>({...file,content:'tampered'});await ui.exportDeliveryFile('manifest','internal');assert.equal(downloads.length,0);
  handler=async()=>file;await ui.exportDeliveryFile('manifest','internal');assert.equal(downloads.length,1);
  let finish;handler=()=>new Promise(resolve=>{finish=resolve;});const stale=ui.exportDeliveryFile('manifest','internal');
  context.selectedProjectId='other';ui.clearWorkCenter();finish(file);await stale;assert.equal(downloads.length,1);assert.equal(element('#deliveryPanel').hidden,true);
  assert.equal(element('#workCenterTools').innerHTML,'');assert.equal(element('#moreWorksBtn').hidden,true);
  Object.assign(ui.workComparison,{context:context.editorContext(),result:{left:{artifact_id:'left'},right:{artifact_id:'right'},sync_duration:8},generation:1,busy:false});
  const resolves={};handler=path=>new Promise(resolve=>{resolves[path]=resolve;});element('#comparisonTime').value='2.25';
  const left=ui.viewComparisonSide(0),right=ui.viewComparisonSide(1);
  resolves['/artifacts/right/access']({url:'https://fixture.invalid/right'});await right;
  resolves['/artifacts/left/access']({url:'https://fixture.invalid/left'});await left;
  assert.equal(element('#comparisonVideo').src,'https://fixture.invalid/right');element('#comparisonVideo').onloadedmetadata();assert.equal(element('#comparisonVideo').currentTime,2.25);
  await publicPage();console.log('web/public delivery receipts/hash/scope/lifecycle/AB ordering: PASS (offline; no media execution)');
})().catch(error=>{console.error(error);process.exitCode=1;});

async function publicPage(){
  elements.clear();downloads.length=0;const requests=[];let next={ok:true,status:200,json:async()=>({label:'客户审阅',duration:2,measurements:{width:320,height:240},expires_at:'2027-01-01',allow_download:true,include_subtitles:true})};
  const publicContext={console,Blob,URL:context.URL,setTimeout:fn=>fn(),sha256:hash,
    document:{getElementById:element,createElement:()=>({...element('public-anchor')})},
    window:{location:{hash:'#sg1.00000000-0000-4000-8000-000000000001.'+'a'.repeat(43),pathname:'/share'},history:{replaceState:(...args)=>requests.push({history:args})}},
    fetch:async(path,options)=>{requests.push({path,...options});return next;}};
  vm.createContext(publicContext);vm.runInContext(fs.readFileSync(require.resolve('../share.js'),'utf8'),publicContext);
  await new Promise(resolve=>setImmediate(resolve));assert.equal(requests[0].history[2],'/share');
  assert.equal(JSON.parse(requests[1].body).token,'sg1.00000000-0000-4000-8000-000000000001.'+'a'.repeat(43));assert.ok(!requests[1].path.includes('secret'));
  assert.equal(element('sharedVideo').src,'');assert.equal(element('playShare').disabled,false);
  next={ok:true,status:200,json:async()=>({url:'https://fixture.invalid/short-preview'})};await element('playShare').listeners.click();
  assert.equal(element('sharedVideo').src,'https://fixture.invalid/short-preview');
  next={ok:true,status:200,json:async()=>({content:'bad',sha256:hash('other'),filename:'video-delivery.json'})};
  await element('manifestShare').listeners.click();assert.equal(downloads.length,0);assert.match(element('shareStatus').textContent,/校验/);
  next={ok:false,status:404};await element('inspectShare').listeners.click();assert.equal(element('playShare').disabled,true);assert.equal(element('sharedVideo').src,'');
  assert.equal(element('sharedDownload').children.length,0);assert.match(element('shareStatus').textContent,/失效/);
}
