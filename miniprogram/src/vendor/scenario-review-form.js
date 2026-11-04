(function(root,factory){const value=factory();if(typeof module==='object'&&module.exports)module.exports=value;else root.ScenarioReviewForms=value;})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const dimensions=[{id:'facts',label:'事实准确与人物一致'},{id:'technical',label:'技术质量'},{id:'narrative',label:'叙事与镜头衔接'},{id:'visual',label:'画面效果'},{id:'audio',label:'声音与语义'}];
  const statuses=[{id:'unreviewed',label:'尚未审阅'},{id:'passed',label:'通过'},{id:'failed',label:'需修改'},{id:'not_applicable',label:'不适用（须说明）'}];
  const rubricVersion='human-review-v1';
  function label(id){return dimensions.find(row=>row.id===id)?.label||id;}
  function statusLabel(id){return statuses.find(row=>row.id===id)?.label||id;}
  function identity(context){return JSON.stringify([context?.artifact_id,context?.output_sha256,context?.scenario]);}
  function fields(context){const required=context?.required_dimensions;return dimensions.filter(row=>Array.isArray(required)&&required.includes(row.id));}
  function blank(context){return {identity:identity(context),dimensions:fields(context).map(row=>({dimension:row.id,status:'unreviewed',score:'',note:''}))};}
  function draft(value,context){if(!value||value.identity!==identity(context)||!Array.isArray(value.dimensions))return blank(context);return {identity:value.identity,dimensions:fields(context).map(spec=>{const row=value.dimensions.find(row=>row.dimension===spec.id)||{};return {dimension:spec.id,status:statuses.some(item=>item.id===row.status)?row.status:'unreviewed',score:typeof row.score==='string'?row.score:'',note:typeof row.note==='string'?row.note:''};})};}
  function audioNotApplicable(context,profiles){const binding=context?.scenario,profile=(profiles||[]).find(row=>row.id===binding?.id&&row.version===binding?.version&&row.content_hash===binding?.content_hash&&row.prompt_version===binding?.prompt_version);return context?.has_audio===false&&Boolean(profile)&&profile.source_policy!=='speech_first'&&profile.preserve_speech_meaning===false;}
  function choices(context,profiles,dimension){return statuses.filter(row=>row.id!=='not_applicable'||dimension==='audio'&&audioNotApplicable(context,profiles));}
  function body(context,value,profiles=[]){
    if(!context?.scenario||value.identity!==identity(context))throw new Error('成片或场景版本已变化，请刷新后重新审阅');
    if(!/^[a-f0-9]{64}$/.test(context.output_sha256)||!context.scenario.content_hash||!context.scenario.prompt_version)throw new Error('缺少实际文件或场景核验信息，请先刷新');
    if(!fields(context).length)throw new Error('当前场景缺少评审维度，请先核对场景配置');
    if(!context.evidence_ids?.includes(context.artifact_id))throw new Error('缺少当前成片的证据引用，请先刷新');
    const rows=fields(context).map(spec=>{const row=value.dimensions.find(item=>item.dimension===spec.id);if(!row||!choices(context,profiles,spec.id).some(item=>item.id===row.status))throw new Error(label(spec.id)+'的评审状态不适用');const score=String(row.score??'').trim();if(['unreviewed','not_applicable'].includes(row.status)&&score)throw new Error('尚未审阅或不适用的维度不能填写评分');if(row.status==='not_applicable'&&!row.note.trim())throw new Error('请说明声音不适用的依据');if(row.note.length>2000)throw new Error('单项评审意见不能超过2000字');const result={dimension:spec.id,status:row.status,evidence_ids:row.status==='unreviewed'?[]:[context.artifact_id],note:row.note};if(score){const numeric=Number(score);if(!Number.isFinite(numeric)||numeric<0||numeric>5)throw new Error('评分需为0至5的数字，也可留空');result.score=numeric;}return result;});
    return {artifact_id:context.artifact_id,output_sha256:context.output_sha256,scenario:{...context.scenario},rubric_version:rubricVersion,dimensions:rows};
  }
  function verdict(value){return ({passed:'场景评审通过',failed:'场景评审需修改',needs_review:'仍需审阅'})[value]||'待核对';}
  return {dimensions,statuses,rubricVersion,label,statusLabel,identity,fields,blank,draft,audioNotApplicable,choices,body,verdict};
});
