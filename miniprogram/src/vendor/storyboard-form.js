/* Platform-neutral storyboard authoring. No provider or media execution. */
(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.StoryForms=value;})(typeof globalThis==='object'?globalThis:this,function(){
  'use strict';
  const visualFields=['subject','action','scene','composition','camera','lighting','style'];
  const copy=value=>JSON.parse(JSON.stringify(value));
  const id=()=> 's'+Date.now().toString(36)+Math.random().toString(36).slice(2,12);
  function shot(duration=5000,span=null){return {id:id(),duration_ms:duration,intent:'',visual:Object.fromEntries(visualFields.map(key=>[key,''])),constraints:[],voiceover:'',screen_text:'',sound_notes:'',reference_span_id:span,locked:false};}
  function empty(){return {schema_version:'storyboard/1',title:'新分镜',target_duration_ms:5000,facts:[],shots:[shot()],status:'draft'};}
  function fromReference(reference){const spans=reference.evidence.spans;if(!reference.source_current)throw Error('参考对象已变化，请重新登记');if(!spans.length||spans.some(row=>row.end_ms-row.start_ms<100||row.end_ms-row.start_ms>120000))throw Error('参考区间含过短或超过120秒的镜头，请手工创建并拆分');const content=empty();content.title=reference.asset_name+' · 分镜';content.shots=spans.map(row=>shot(row.end_ms-row.start_ms,row.id));return total(content);}
  function total(content){content.target_duration_ms=content.shots.reduce((sum,row)=>sum+row.duration_ms,0);return content;}
  function seconds(value){const text=String(value).trim();if(!/^\d+(?:\.\d{1,3})?$/.test(text))throw Error('时间须为非负秒数，最多三位小数');const parts=text.split('.');const ms=Number(parts[0])*1000+Number((parts[1]||'').padEnd(3,'0'));if(!Number.isSafeInteger(ms)||ms>7200000)throw Error('时间超出范围');return ms;}
  function validate(content){if(!content.title.trim())throw Error('请填写分镜名称');if(!content.shots.length||content.shots.length>500)throw Error('分镜须有1至500个镜头');if(content.shots.some(row=>!Number.isSafeInteger(row.duration_ms)||row.duration_ms<100||row.duration_ms>120000))throw Error('每个镜头须为0.1至120秒');if(content.target_duration_ms!==content.shots.reduce((sum,row)=>sum+row.duration_ms,0)||content.target_duration_ms>7200000)throw Error('目标时长与镜头之和不一致或超过2小时');if(content.status==='confirmed'&&(content.shots.some(row=>!row.intent.trim()||!row.visual.subject.trim())||content.facts.some(row=>!row.confirmed)))throw Error('确认前请补齐镜头意图、主体并确认资料事实');return content;}
  function body(content,base){const value=validate(copy(content));return {base_revision_id:base.revision_id,content:value,unlock_shot_ids:base.content.shots.filter(row=>row.locked&&!value.shots.find(item=>item.id===row.id)?.locked).map(row=>row.id),unlock_fact_ids:base.content.facts.filter(row=>row.locked&&!value.facts.find(item=>item.id===row.id)?.locked).map(row=>row.id)};}
  return {visualFields,copy,id,shot,empty,fromReference,total,seconds,validate,body};
});
