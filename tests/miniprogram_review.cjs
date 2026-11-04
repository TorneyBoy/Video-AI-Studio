// Real native page/state code with offline wx APIs, not a device acceptance.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname,'..');
const output = fs.mkdtempSync(path.join(os.tmpdir(),'video-ai-mini-review-'));
const watchdog = setTimeout(() => {console.error('Native review contract did not finish');process.exit(1);},15000);
let page; const values=new Map(); const downloads=[]; const albums=[]; const seeks=[];
global.wx={getStorageSync:key=>values.get(key),setStorageSync:(key,value)=>values.set(key,structuredClone(value)),removeStorageSync:key=>values.delete(key),
  createVideoContext:()=>({seek:time=>seeks.push(time)}),downloadFile:options=>{downloads.push(options);options.success({statusCode:200,tempFilePath:'wxfile://offline-media'});},
  saveVideoToPhotosAlbum:options=>{albums.push(options.filePath);options.success();}};
global.Page=definition=>{page=definition;};
(async()=>{
  execFileSync(process.execPath,[path.join(root,'miniprogram/node_modules/typescript/bin/tsc'),'--project',path.join(root,'miniprogram/tsconfig.json'),
    '--noEmit','false','--module','Node16','--moduleResolution','Node16','--rootDir',path.join(root,'miniprogram/src'),'--outDir',output],{stdio:'pipe'});
  fs.cpSync(path.join(root,'miniprogram/src/vendor'),path.join(output,'vendor'),{recursive:true});
  let handler;const calls=[];const apiPath=path.join(output,'shared/api.js');
  let activeProject='project';
  const personalSession={profile:{id:'owner',tenant_id:'tenant'},activeProject:()=> activeProject,request:async(...args)=>{calls.push(args);return handler(...args);}};
  require.cache[apiPath]={id:apiPath,filename:apiPath,loaded:true,exports:{personalSession,message:error=>error.message}};
  const {ClientError}=require(path.join(output,'shared/contracts.js'));
  const state=require(path.join(output,'shared/review-state.js'));
  require(path.join(output,'pages/work-detail/index.js'));
  page.data=structuredClone(page.data);page.setData=change=>Object.assign(page.data,change);
  let report={artifact_id:'artifact',binding_hash:'a'.repeat(64),binding:{},status:'pending',latest:null,can_review:true,can_preview:true,can_download:false,
    quality_ready:true,quality_scope:'结构质检',reason:'请审阅',duration:8,measurements:{width:1080,height:1920},revision_number:1,origin_job_id:null};
  handler=async endpoint=>endpoint.endsWith('/review') ? report : {url:'https://fixture.invalid/preview',expires_at:'later'};
  await page.onLoad({id:'artifact'});assert.equal(page.data.reviewLabel,'待审核');await page.saveVideo();assert.equal(downloads.length,0);
  page.videoTime({detail:{currentTime:1.25}});page.seekIssue({currentTarget:{dataset:{start:'1.25'}}});assert.deepEqual(seeks,[1.25]);
  page.reviewNote({detail:{value:'字幕需修订'}});page.addIssue();page.issueInput({currentTarget:{dataset:{index:0,field:'message'}},detail:{value:'修订这句'}});
  assert.equal(page.data.form.issues[0].start,'1.250');
  handler=async()=>{throw new ClientError('review_revision_conflict','Other page reviewed',409);};
  await page.submitReview({currentTarget:{dataset:{decision:'changes_requested'}}});
  assert.equal(page.data.form.note,'字幕需修订');assert.equal(state.readReviewState('artifact').pending,null);assert.equal(page.data.reviewConflict,true);
  const before=calls.length;await page.submitReview({currentTarget:{dataset:{decision:'approved'}}});assert.equal(calls.length,before);
  report={...report,latest:{id:'other',number:1,decision:'approved',note:'Other',issues:[]}};
  handler=async()=>report;await page.loadReview();page.removeIssue({currentTarget:{dataset:{index:0}}});page.reviewNote({detail:{value:'重新审阅通过'}});
  handler=async()=>{throw new ClientError('submission_unknown','Disconnected',0,true);};
  await page.submitReview({currentTarget:{dataset:{decision:'approved'}}});const pending=state.readReviewState('artifact').pending;const original=calls.at(-1);
  assert.equal(pending.body.base_review_id,'other');assert.equal(pending.body.note,'重新审阅通过');
  await assert.rejects(state.sendReview(state.reviewOperation('artifact',{...pending.body,note:'different'}),page.data.form),error=>error.code==='review_unconfirmed');
  handler=async()=>({found:false,review:null,current:report});await page.recoverReview({currentTarget:{dataset:{}}});assert.equal(page.data.pending.key,pending.key);
  report={...report,status:'approved',can_download:true,latest:{id:'saved',number:2,decision:'approved',note:'重新审阅通过',issues:[]}};
  handler=async(...args)=>{assert.deepEqual(args,original);return{review:report.latest,current:report};};await page.recoverReview({currentTarget:{dataset:{repeat:true}}});
  assert.equal(page.data.pending,null);assert.equal(page.data.report.latest.id,'saved');assert.equal(page.data.form.note,'');
  handler=async()=>({url:'https://fixture.invalid/download?short-signature'});await page.saveVideo();assert.equal(albums.length,1);assert.equal(downloads[0].header,undefined);
  assert.ok(!JSON.stringify([...values.values()]).match(/https:|short-signature|wxfile:|access_token/));
  page.reviewNote({detail:{value:'Quota draft'}});const previousSet=wx.setStorageSync;wx.setStorageSync=()=>{throw new Error('Quota');};
  const beforeQuota=calls.length;await page.submitReview({currentTarget:{dataset:{decision:'approved'}}});assert.equal(calls.length,beforeQuota);assert.equal(page.data.form.note,'Quota draft');wx.setStorageSync=previousSet;
  let finishDownload;wx.downloadFile=options=>{finishDownload=options.success;};const saving=page.saveVideo();
  await new Promise(resolve=>setImmediate(resolve));page.onHide();finishDownload({statusCode:200,tempFilePath:'wxfile://late'});await saving;assert.equal(albums.length,1);
  personalSession.profile={id:'other-user',tenant_id:'other-tenant'};await page.onShow();assert.equal(page.data.report,null);assert.equal(page.data.url,'');
  // Native scene quality evidence has independent same-key receipts and a frozen media binding.
  personalSession.profile={id:'owner',tenant_id:'tenant'};const Scenario=require(path.join(output,'vendor/scenario-review-form.js')),binding={id:'product_showcase',version:'v1',content_hash:'b'.repeat(64),prompt_version:'prompt-v1'},profile={...binding,source_policy:'real_first',preserve_speech_meaning:false};
  const quality={artifact_id:'artifact',output_sha256:'c'.repeat(64),scenario:binding,required_dimensions:['facts','technical','narrative','visual','audio'],has_audio:false,technical_findings:[{code:'blur_detected',severity:'warning',message:'请核对清晰度'}],evidence_ids:['artifact'],reviews:[]};report={...report,project_id:'project',status:'pending',can_download:false,binding_hash:'d'.repeat(64)};
  handler=async endpoint=>endpoint.endsWith('/review')?report:endpoint.endsWith('/quality/scenarios')?{profiles:[profile]}:endpoint.includes('/quality/artifacts/')?quality:{url:'https://fixture.invalid/preview',expires_at:'later'};await page.loadReview();assert.ok(page.data.qualityForm.dimensions.every(row=>row.status==='unreviewed'&&row.score===''));assert.equal(page.data.qualityFields.length,5);assert.equal(page.data.qualityData.technical_findings[0].code,'blur_detected');
  const beforeInvalidScore=calls.length;page.qualityInput({currentTarget:{dataset:{index:0,field:'score'}},detail:{value:'5'}});await page.saveQuality();assert.equal(calls.length,beforeInvalidScore,'an unreviewed dimension cannot carry an invented score');page.qualityInput({currentTarget:{dataset:{index:0,field:'score'}},detail:{value:''}});page.qualityStatus({currentTarget:{dataset:{index:0}},detail:{value:'1'}});page.qualityInput({currentTarget:{dataset:{index:0,field:'score'}},detail:{value:'4.5'}});page.qualityInput({currentTarget:{dataset:{index:0,field:'note'}},detail:{value:'逐帧核对主体与事实'}});page.qualityStatus({currentTarget:{dataset:{index:4}},detail:{value:'3'}});page.qualityInput({currentTarget:{dataset:{index:4,field:'note'}},detail:{value:'测量为无音轨，场景无需口播'}});
  handler=async()=>{throw new ClientError('submission_unknown','Disconnected',0,true);};await page.saveQuality();const originalQuality=calls.at(-1),qualityPending=state.readQualityState('project','artifact').pending;assert.ok(qualityPending);assert.equal(originalQuality[2].dimensions[0].score,4.5);assert.equal(originalQuality[2].dimensions[1].status,'unreviewed');assert.equal(originalQuality[2].reviewer_id,undefined);assert.equal(page.data.qualityPending.key,qualityPending.key);const beforeBlocked=calls.length;await page.submitReview({currentTarget:{dataset:{decision:'approved'}}});await page.saveVideo();assert.equal(calls.length,beforeBlocked);
  page.setData({reviewLoading:true});const beforeLoadingReplay=calls.length;await page.recoverQuality({currentTarget:{dataset:{repeat:true}}});assert.equal(calls.length,beforeLoadingReplay,'refresh and recovery writes cannot overlap');page.setData({reviewLoading:false});
  handler=async()=>{throw new ClientError('quality_request_conflict','Repeat rejected',409);};await page.recoverQuality({currentTarget:{dataset:{repeat:true}}});assert.deepEqual(calls.at(-1),originalQuality);assert.equal(state.readQualityState('project','artifact').pending.key,qualityPending.key);
  handler=async()=>({found:false,result:null});await page.recoverQuality({currentTarget:{dataset:{}}});assert.ok(calls.at(-1)[0].endsWith('/reviews/requests/'+encodeURIComponent(qualityPending.key)));assert.equal(page.data.qualityPending.key,qualityPending.key);
  const qualityRecord={id:'quality-record',artifact_id:'artifact',review:qualityPending.body,assessment:{verdict:'needs_review'},created_at:'now',stale:false};handler=async(endpoint,...args)=>{if(endpoint.endsWith('/quality/reviews')){assert.deepEqual([endpoint,...args],originalQuality);return qualityRecord;}if(endpoint.endsWith('/review'))throw new ClientError('offline','Report refresh failed');throw new Error('Unexpected '+endpoint);};await page.recoverQuality({currentTarget:{dataset:{repeat:true}}});assert.equal(page.data.qualityPending,null);assert.equal(page.data.qualityStaleReport,true);const beforeStale=calls.length;await page.submitReview({currentTarget:{dataset:{decision:'approved'}}});assert.equal(calls.length,beforeStale);
  report={...report,binding_hash:'e'.repeat(64)};handler=async endpoint=>endpoint.endsWith('/review')?report:endpoint.endsWith('/quality/scenarios')?{profiles:[profile]}:{...quality,reviews:[qualityRecord]};await page.loadReview();assert.equal(page.data.report.binding_hash,'e'.repeat(64));assert.equal(page.data.qualityStaleReport,false);assert.equal(page.data.qualityLatest.label,'仍需审阅');assert.equal(page.data.report.status,'pending');
  const oldSet=wx.setStorageSync;wx.setStorageSync=()=>{throw new Error('Quota');};const beforeQualityQuota=calls.length;await page.saveQuality();assert.equal(calls.length,beforeQualityQuota);assert.equal(page.data.qualityPending,null);wx.setStorageSync=oldSet;
  let qualityCacheWrites=0;const qualityStore=wx.setStorageSync;wx.setStorageSync=(key,value)=>{qualityCacheWrites++;if(qualityCacheWrites===2)throw new Error('Quota after quality POST');qualityStore(key,value);};handler=async endpoint=>endpoint.endsWith('/quality/reviews')?qualityRecord:endpoint.endsWith('/review')?report:endpoint.endsWith('/quality/scenarios')?{profiles:[profile]}:{...quality,reviews:[qualityRecord]};await page.saveQuality();assert.ok(page.data.qualityPending,'confirmed write with failed cleanup remains recoverable');wx.setStorageSync=qualityStore;handler=async endpoint=>endpoint.includes('/reviews/requests/')?{found:true,result:qualityRecord}:endpoint.endsWith('/review')?report:endpoint.endsWith('/quality/scenarios')?{profiles:[profile]}:{...quality,reviews:[qualityRecord]};await page.recoverQuality({currentTarget:{dataset:{}}});assert.equal(page.data.qualityPending,null);
  handler=async endpoint=>endpoint.endsWith('/review')?report:endpoint.endsWith('/quality/scenarios')?{profiles:[profile]}:{...quality,output_sha256:'f'.repeat(64),reviews:[]};await page.loadReview();assert.ok(page.data.qualityForm.dimensions.every(row=>row.status==='unreviewed'&&row.score===''),'a changed file cannot inherit old human judgments');
  let finishQuality;handler=()=>new Promise(resolve=>finishQuality=resolve);const late=page.saveQuality();await new Promise(resolve=>setImmediate(resolve));const lateKey=state.readQualityState('project','artifact').pending.key;page.onHide();activeProject='other-project';finishQuality(qualityRecord);await late;assert.equal(page.data.qualityData.output_sha256,'f'.repeat(64),'late receipt cannot replace the current view');activeProject='project';assert.equal(state.readQualityState('project','artifact').pending.key,lateKey,'late receipt cannot clear the original recovery metadata');assert.ok(!JSON.stringify([...values.values()]).match(/short-signature|access_token/));
  page.onUnload();console.log('native review state/receipts/save lifecycle: PASS (offline; no WeChat/device/media execution)');
})().catch(error=>{console.error(error.stdout?.toString()||error);process.exitCode=1;}).finally(()=>{
  clearTimeout(watchdog);if(!path.resolve(output).startsWith(path.resolve(os.tmpdir())+path.sep+'video-ai-mini-review-'))throw new Error('Unsafe temporary path');
  fs.rmSync(output,{recursive:true,force:true});
});
