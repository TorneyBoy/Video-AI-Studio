// Run actual native state/pages with offline APIs; no WeChat tools or media.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {execFileSync}=require('node:child_process');const {createHash}=require('node:crypto');
const root=path.resolve(__dirname,'..'),output=fs.mkdtempSync(path.join(os.tmpdir(),'video-ai-mini-delivery-'));
const watchdog=setTimeout(()=>{console.error('Native delivery check did not finish');process.exit(1);},15000);
const values=new Map(),calls=[],writes=[],clipboard=[],shared=[];let page,handler;
global.wx={getStorageSync:key=>values.get(key),setStorageSync:(key,value)=>values.set(key,structuredClone(value)),
  env:{USER_DATA_PATH:'wxfile://offline'},getFileSystemManager:()=>({writeFile:options=>{writes.push(options);options.success();}}),
  setClipboardData:options=>clipboard.push(options.data),shareFileMessage:options=>shared.push(options),
  createVideoContext:()=>({pause(){},seek(){}})};
global.Page=definition=>{page=definition;page.data=structuredClone(page.data);page.setData=change=>Object.assign(page.data,change);};
(async()=>{
  execFileSync(process.execPath,[path.join(root,'miniprogram/node_modules/typescript/bin/tsc'),'--project',path.join(root,'miniprogram/tsconfig.json'),
    '--noEmit','false','--module','Node16','--moduleResolution','Node16','--rootDir',path.join(root,'miniprogram/src'),'--outDir',output],{stdio:'pipe'});
  fs.cpSync(path.join(root,'miniprogram/src/vendor'),path.join(output,'vendor'),{recursive:true});
  const apiPath=path.join(output,'shared/api.js');const session={profile:{id:'owner',tenant_id:'tenant'},activeProject:()=> 'project',
    request:async(...args)=>{calls.push(args);return handler(...args);}};
  require.cache[apiPath]={id:apiPath,filename:apiPath,loaded:true,exports:{personalSession:session,message:error=>error.message}};
  const {ClientError}=require(path.join(output,'shared/contracts.js')),state=require(path.join(output,'shared/delivery-state.js'));
  const report={artifact_id:'artifact',binding_hash:'a'.repeat(64),latest:{id:'approved'},can_download:true,can_review:true};
  const share={id:'share',artifact_id:'artifact',label:'客户审阅',audience:'workspace',status:'active',url:'https://fixture.invalid/share#sg1.ephemeral',expires_at:'later'};
  const form=state.blankShare(),intent=state.shareOperation('artifact',report,form);
  handler=async()=>{throw new ClientError('submission_unknown','Disconnected',0,true);};
  await assert.rejects(state.sendDelivery(intent,form));const original=calls.at(-1);
  assert.equal(state.deliveryState('artifact').pending.key,intent.key);
  await assert.rejects(state.sendDelivery(state.shareOperation('artifact',report,{...form,label:'Other'}),form),error=>error.code==='delivery_unconfirmed');
  handler=async()=>({found:false,share:null});await state.checkDelivery(intent,form);assert.equal(state.deliveryState('artifact').pending.key,intent.key);
  handler=async(...args)=>{assert.deepEqual(args,original);return share;};await state.sendDelivery(intent,form);
  assert.equal(state.deliveryState('artifact').pending,null);
  assert.ok(!JSON.stringify([...values.values()]).match(/https:|ephemeral|wxfile:/));
  const content='中文交付字幕\n',file={artifact_id:'artifact',binding_hash:report.binding_hash,filename:'subtitles-artifact.srt',content,sha256:createHash('sha256').update(content).digest('hex')};
  await assert.rejects(state.writeDeliveryFile({...file,content:'tampered'},'artifact',report.binding_hash));assert.equal(writes.length,0);
  await state.writeDeliveryFile(file,'artifact',report.binding_hash);assert.equal(writes[0].encoding,'utf8');
  await assert.rejects(state.writeDeliveryFile({...file,filename:'../../secret'},'artifact',report.binding_hash));
  require(path.join(output,'pages/delivery/index.js'));
  handler=async endpoint=>endpoint.endsWith('/review')?report:endpoint==='/delivery/config'?{sharing:{available:true,reason:'Fixture'}}:[];
  await page.onLoad({id:'artifact'});let finish;
  handler=async()=>new Promise(resolve=>{finish=resolve;});const oldLink=page.getLink({currentTarget:{dataset:{id:'share'}}});
  assert.equal(page.data.busy,true);page.onHide();finish(share);await oldLink;assert.equal(page.data.url,'');page.copyLink();assert.equal(clipboard.length,0);
  handler=async()=>share;await page.getLink({currentTarget:{dataset:{id:'share'}}});page.copyLink();assert.equal(clipboard[0],share.url);
  handler=async()=>new Promise(resolve=>{finish=resolve;});const oldFile=page.exportFile({currentTarget:{dataset:{kind:'srt'}}});
  page.onHide();finish(file);await oldFile;assert.equal(writes.length,1);page.shareFile();assert.equal(shared.length,0);
  session.profile={id:'other',tenant_id:'other'};await page.onShow();assert.equal(page.data.report,null);assert.deepEqual(page.data.shares,[]);
  session.profile={id:'owner',tenant_id:'tenant'};
  require(path.join(output,'pages/compare/index.js'));const comparisonPage=page;
  handler=async()=>new Promise(resolve=>{finish=resolve;});const loading=comparisonPage.onLoad({left:'artifact'});
  comparisonPage.onHide();finish({project_id:'project'});await loading;assert.equal(comparisonPage.data.projectId,'');
  const long='字'.repeat(600),view=state.changeView({kind:'changed',track_type:'subtitle',position:1,fields:{text:{before:long,after:'修订'}}},
    {left:{source_labels:{}},right:{source_labels:{}}});assert.equal(view.fields[0].before.length,500);assert.equal(view.fields[0].fullBefore,long);
  console.log('native delivery receipts/hash/privacy/lifecycle: PASS (offline; no WeChat/device/media execution)');
})().catch(error=>{console.error(error.stdout?.toString()||error);process.exitCode=1;}).finally(()=>{
  clearTimeout(watchdog);if(!path.resolve(output).startsWith(path.resolve(os.tmpdir())+path.sep+'video-ai-mini-delivery-'))throw new Error('Unsafe temporary path');
  fs.rmSync(output,{recursive:true,force:true});
});
