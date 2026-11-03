// Compile real native Page/shared state and exercise offline; no WeChat device.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),{execFileSync}=require('node:child_process');
const root=path.resolve(__dirname,'..'),output=fs.mkdtempSync(path.join(os.tmpdir(),'video-ai-mini-matching-')),values=new Map(),calls=[];let page,project='project',handler;
global.wx={getStorageSync:key=>values.get(key),setStorageSync:(key,value)=>values.set(key,structuredClone(value)),showModal:async()=>({confirm:true}),stopPullDownRefresh(){},setClipboardData(){}};
global.Page=definition=>{page=definition;page.setData=changes=>Object.assign(page.data,changes);};
(async()=>{
 execFileSync(process.execPath,[path.join(root,'miniprogram/node_modules/typescript/bin/tsc'),'--project',path.join(root,'miniprogram/tsconfig.json'),'--noEmit','false','--module','Node16','--moduleResolution','Node16','--rootDir',path.join(root,'miniprogram/src'),'--outDir',output],{stdio:'pipe'});
 fs.cpSync(path.join(root,'miniprogram/src/vendor'),path.join(output,'vendor'),{recursive:true});const apiPath=path.join(output,'shared/api.js'),session={profile:{id:'person'},activeProject:()=>project,restore:async()=>session.profile,request:async(...args)=>{calls.push(args);return handler(...args);}};
 require.cache[apiPath]={id:apiPath,filename:apiPath,loaded:true,exports:{personalSession:session,message:error=>error.message}};
 const State=require(path.join(output,'shared/shot-matching.js')),Forms=require(path.join(output,'vendor/shot-matching-form.js')),Story=require(path.join(output,'vendor/storyboard-form.js')),{ClientError}=require(path.join(output,'shared/contracts.js'));
 const story={document_id:'doc',revision_id:'story1',project_id:'project',number:1,content:Story.empty()};story.content.shots[0].id='one';
 const configuration={story_revision_id:'story1',asset_ids:['asset'],overrides:{},candidates_per_shot:3};
 const report={id:'report',document_id:'doc',story_revision_id:'story1',stale_inputs:false,result:{configuration,summary:{shots:1,with_candidates:1},excluded_assets:[],items:[{shot_id:'one',requirement_hash:'hash',gap:'visual_review_required',excluded_counts:{},reshoot:{duration_ms:5000,intent:'展示商品',visual:{subject:'蓝色商品'},constraints:[],facts:[]},candidates:[{id:'candidate',asset_id:'asset',name:'Source',kind:'video',start_ms:0,end_ms:5000,reasons:['duration_sufficient'],review:['visual_unverified:subject'],semantic_provenance:'unavailable',diversity:{}}]}]}};
 const cache=State.cache('doc');cache.assetIds=['asset'];State.persist('doc',cache);const op=State.operation('report','/storyboards/doc/match-reports','/storyboards/doc/match-requests/',configuration);
 handler=async()=>{throw new ClientError('submission_unknown','Disconnected',0,true);};await assert.rejects(State.send('doc',cache,op));const original=calls.at(-1);
 await assert.rejects(State.send('doc',State.cache('doc'),{...op,key:'different'}),error=>error.code==='operation_unconfirmed');
 require(path.join(output,'pages/shot-matching/index.js'));page.onLoad({documentId:'doc'});
 const source={revision_id:null,head_revision_id:null,story_revision_id:'story1',story_current:true,inputs_current:true,number:0,content:null};
 handler=async endpoint=>endpoint==='/storyboards/doc'?story:endpoint==='/storyboards/doc/sourcing'?source:endpoint.includes('/match-requests/')?{found:false}:endpoint==='/storyboards/doc/match-reports'?report:[];
 await page.onShow();await page.recover({currentTarget:{dataset:{resend:true}}});assert.deepEqual(calls.at(-1),original);assert.equal(page.data.items[0].choice.mode,'unassigned');
 page.candidate({currentTarget:{dataset:{shot:'one',id:'candidate'}}});page.sourceNote({currentTarget:{dataset:{id:'one'}},detail:{value:'已逐项核对商品细节'}});page.sourceReview({currentTarget:{dataset:{id:'one'}}});page.sourceLock({currentTarget:{dataset:{id:'one'}}});assert.ok(page.data.cache.rows[0].locked);
 page.sourceMode({currentTarget:{dataset:{id:'one',mode:'reshoot'}}});assert.equal(page.data.cache.rows[0].mode,'candidate');
 handler=async()=>{throw new ClientError('submission_unknown','Disconnected',0,true);};await page.save();assert.ok(page.data.pending);
 handler=async()=>({found:true,result:{revision_id:'s1',head_revision_id:'s2',number:1}});await page.recover({currentTarget:{dataset:{}}});assert.equal(page.data.cache.rows[0].note,'已逐项核对商品细节');assert.equal(page.data.cache.source,null);
 let complete;handler=async endpoint=>endpoint==='/storyboards/doc'?new Promise(resolve=>complete=resolve):endpoint.endsWith('/sourcing')?source:[];const delayed=page.load();project='changed';page.onHide();complete(story);await delayed;assert.equal(State.cache('doc').pending,null);assert.equal(State.cache('doc').report,null);
 assert.ok([...values.values()].every(value=>!JSON.stringify(value).includes('access_token')));
 console.log('PASS compiled native matching selection/lock, original-request recovery, old receipt preservation and context fencing; no WeChat device');
})().catch(error=>{console.error(error.stdout?.toString()||error);process.exitCode=1;}).finally(()=>{delete global.wx;delete global.Page;fs.rmSync(output,{recursive:true,force:true});});
