// Actual browser controller/shared rules, offline transport and DOM.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const Forms=require('../web/shot-matching-form.js'),Story=require('../web/storyboard-form.js');
assert.equal(fs.readFileSync(require.resolve('../web/shot-matching-form.js'),'utf8'),fs.readFileSync(require.resolve('../miniprogram/src/vendor/shot-matching-form.js'),'utf8'));
assert.match(Forms.reason('visual_description_differs:subject'),/需人工比较/);assert.deepEqual(Forms.tags('蓝色、商品，特写'),['蓝色','商品','特写']);
const safeOverrides={};assert.equal(Forms.overrideFor(safeOverrides,'__proto__'),null);Forms.setOverride(safeOverrides,'__proto__',Forms.override());assert.equal(Object.getPrototypeOf(safeOverrides),Object.prototype);assert.equal(Forms.overrideFor(safeOverrides,'__proto__').orientation,'any');
const annotation=Forms.annotationDraft(null,'whole');assert.throws(()=>Forms.annotationBody(annotation,null,Story.seconds));annotation.confirmed=true;assert.equal(Forms.annotationBody(annotation,null,Story.seconds).spans[0].start_ms,null);
const values=new Map(),elements=new Map(),calls=[],notices=[];let handler;
function element(key){if(!elements.has(key))elements.set(key,{hidden:false,innerHTML:'',addEventListener(){},querySelector:element,querySelectorAll:()=>[],play:async()=>{}});return elements.get(key);}
const ctx={console,JSON,Object,Array,Number,String,Math,Date,Set,Map,encodeURIComponent,StoryForms:Story,ShotMatchingForms:Forms,
  selectedProjectId:'project',owner:'person',document:{querySelector:element},window:{confirm:()=>true},
  localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)},
  esc:text=>String(text).replaceAll('<','&lt;'),friendlyApiError:error=>error.message,showToast:message=>notices.push(message),
  assetType:asset=>String(asset.kind||'').split('/')[0],storyPending:()=>null,loadStoryboards:async()=>{},
  api:async(path,options)=>{calls.push({path,options});return handler(path,options);}};
ctx.editorContext=()=>ctx.owner+':'+ctx.selectedProjectId;
vm.createContext(ctx);vm.runInContext(fs.readFileSync(require.resolve('../web/shot-matching.js'),'utf8')+'\nthis.ui={matchingState,matchingContext,matchingOperation,applyMatching,sendMatching,recoverMatching,matchingBody,renderShotMatching,openShotMatching,clearShotMatching};',ctx);
const ui=ctx.ui,story={document_id:'doc',revision_id:'story-1',project_id:'project',number:1,content:Story.empty()};story.content.shots[0].id='one';
ui.matchingState.context=ui.matchingContext('doc');ui.matchingState.story=story;
ui.matchingState.cache={context:ui.matchingState.context,assetIds:['a'],overrides:{},source:null,report:null,rows:[],removedUnlocks:[],allowReuse:false,pending:null};
const configuration=JSON.parse(JSON.stringify(ui.matchingBody()));
const report={id:'report',document_id:'doc',story_revision_id:'story-1',stale_inputs:false,created_at:'2026-10-18',result:{configuration,summary:{shots:1,with_candidates:1,without_candidates:0},items:[{shot_id:'one',requirement_hash:'hash',gap:'visual_review_required',excluded_counts:{},reshoot:{duration_ms:5000,intent:'展示商品',visual:{subject:'蓝色商品'},constraints:[],facts:[]},candidates:[{id:'candidate',asset_id:'a',name:'<img src=x onerror=alert(1)>',kind:'video',start_ms:0,end_ms:5000,verdict:'needs_review',semantic_provenance:'unavailable',observed_visual:{},observed_tags:[],reasons:['duration_sufficient'],review:['visual_unverified:subject'],diversity:{}}]}],excluded_assets:[]}};
(async()=>{
 const op=ui.matchingOperation('report','/storyboards/doc/match-reports','/storyboards/doc/match-requests/',configuration);
 handler=async()=>{throw Error('Disconnected');};await ui.sendMatching(op);const original=calls.at(-1);assert.equal(ui.matchingState.cache.pending.key,op.key);
 const before=calls.length;await ui.sendMatching({...op,key:'new'});assert.equal(calls.length,before);
 handler=async()=>({found:false});await ui.recoverMatching();assert.ok(ui.matchingState.cache.pending);
 handler=async path=>path.includes('/match-requests/')?{found:false}:report;await ui.recoverMatching(true);assert.deepEqual(calls.at(-1),original);assert.equal(ui.matchingState.cache.pending,null);assert.equal(ui.matchingState.cache.rows[0].mode,'unassigned');
 ui.renderShotMatching();assert.ok(!element('#shotMatchingPanel').innerHTML.includes('<img src=x'));assert.match(element('#shotMatchingPanel').innerHTML,/&lt;img/);
 ui.matchingState.cache.rows[0]={shot_id:'one',mode:'candidate',candidate_id:'candidate',reviewed:true,note:'自己的选择，需保留',locked:false};
 const sourceOp=ui.matchingOperation('source','/storyboards/doc/sourcing','/storyboards/doc/source-requests/',Forms.sourceBody(report,null,ui.matchingState.cache.rows));handler=async()=>{throw Error('Disconnected');};await ui.sendMatching(sourceOp);
 handler=async()=>({found:true,result:{revision_id:'s1',head_revision_id:'s2',number:1}});await ui.recoverMatching();assert.equal(ui.matchingState.cache.rows[0].note,'自己的选择，需保留');assert.equal(ui.matchingState.cache.source,null);
 ui.applyMatching('annotation',{asset_id:'a',revision_id:'annotation1',head_revision_id:'annotation1',number:1,content:{spans:[Forms.emptyAnnotation('whole')],note:'Confirmed user note'}});assert.equal(ui.matchingState.cache.annotation.head.revision_id,'annotation1');assert.equal(ui.matchingState.annotationDraft.confirmed,false);
 let complete;handler=()=>new Promise(resolve=>complete=resolve);const delayed=ui.openShotMatching('doc');ctx.selectedProjectId='other';ui.clearShotMatching();complete(story);await delayed;assert.equal(ui.matchingState.story,null);
 assert.ok([...values.values()].every(value=>!value.includes('access_token')&&!value.includes('temporary=')));
 console.log('PASS web matching original-request replay, no automatic adoption, old receipt preservation, escaping and context fencing');
})().catch(error=>{console.error(error);process.exitCode=1;});
