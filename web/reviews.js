// Private review state contains raw text and original request metadata only.
const workReview = {context: '', artifactId: '', report: null, form: {note: '', issues: []}, pending: null,
  busy: false, loading: false, conflict: false, generation: 0, history: [], historyOffset: 0, historyMore: false, showHistory: false, downloadUrl: ''};
const scenarioWorkReview={context:'',projectId:'',artifactId:'',data:null,profiles:[],form:null,pending:null,busy:false,loading:false,notice:'',staleReport:false};
const REVIEW_CATEGORIES = {picture: '画面', audio: '声音', subtitle: '字幕', content: '内容', other: '其他'};
const REVIEW_SEVERITIES = {error: '错误：需修改', warning: '提醒：请注意', info: '说明'};
function reviewLabel(status) {return ({pending: '待审核', approved: '已通过', changes_requested: '已退回', stale: '内容或证据已变化，需重新审核'})[status] || '待核对';}
function reviewMatches() {return Boolean(workReview.artifactId && workReview.context === editorContext());}
function reviewCacheKey() {return 'video-ai-studio.review.v1.' + workReview.context + ':' + workReview.artifactId;}
function saveWorkReviewCache() {localStorage.setItem(reviewCacheKey(), JSON.stringify({form: workReview.form, pending: workReview.pending}));}
function clearWorkReview() {Object.assign(scenarioWorkReview,{context:'',projectId:'',artifactId:'',data:null,profiles:[],form:null,pending:null,busy:false,loading:false,notice:'',staleReport:false});workReview.generation += 1; Object.assign(workReview, {context: '', artifactId: '', report: null, form: {note: '', issues: []}, pending: null, busy: false, loading: false, downloadUrl: '', history: [], showHistory: false}); const panel = document.querySelector('#workReviewPanel'); if (panel) {panel.hidden = true; panel.innerHTML = '';}}
async function openWorkReview(artifactId) {
  clearWorkReview(); workReview.context = editorContext(); workReview.artifactId = artifactId;
  try {const saved = JSON.parse(localStorage.getItem(reviewCacheKey()) || '{}'); workReview.form = saved.form || {note: '', issues: []}; workReview.pending = saved.pending || null;} catch (_) { /* No draft is equivalent to empty text. */ }
  await refreshWorkReview(); return workReview.report;
}
async function refreshWorkReview() {
  if (!reviewMatches() || workReview.busy || scenarioWorkReview.busy) return;
  const context = workReview.context; const artifactId = workReview.artifactId; const generation = ++workReview.generation;
  workReview.loading = true; workReview.downloadUrl = ''; renderWorkReview();
  try {const report = await api(`/artifacts/${encodeURIComponent(artifactId)}/review`);
    if (generation !== workReview.generation || context !== editorContext() || artifactId !== workReview.artifactId) return;
    workReview.report = report; scenarioWorkReview.staleReport=false; workReview.conflict = false; workReview.history = []; workReview.showHistory = false; workReview.historyOffset = 0; await loadScenarioWorkReview(context,artifactId,generation);
  } catch (error) {if (generation === workReview.generation && reviewMatches()) showToast(friendlyApiError(error));}
  finally {if (generation === workReview.generation && reviewMatches()) {workReview.loading = false; renderWorkReview();}}
}
function reviewOptions(choices, selected) {return Object.entries(choices).map(([value,label]) => `<option value="${value}" ${value === selected ? 'selected' : ''}>${label}</option>`).join('');}
function renderReviewIssues(issues) {return issues.map(row => `<div class="review-issue review-${esc(row.severity)}"><span>${esc(REVIEW_CATEGORIES[row.category] || '其他')} · ${esc(row.message)}</span>${row.start !== null ? `<button type="button" class="small-action" data-review-seek="${Number(row.start)}" ${document.querySelector('#previewVideo')?.src ? '' : 'disabled'}>定位 ${Number(row.start).toFixed(3)} 秒</button>` : ''}</div>`).join('');}
function renderWorkReview() {
  const panel = document.querySelector('#workReviewPanel'); if (!panel) return;
  panel.hidden = !reviewMatches(); if (!reviewMatches()) return;
  const report = workReview.report; const blocked = workReview.busy || workReview.loading || Boolean(workReview.pending) || scenarioReviewBlocked();
  const disabled = blocked ? 'disabled' : ''; const changed = workReview.conflict ? 'disabled' : '';
  panel.innerHTML = `<div class="editor-heading"><strong>审阅与领取${report ? ' · ' + esc(reviewLabel(report.status)) : ''}</strong><button id="refreshWorkReviewBtn" class="small-action" type="button" ${workReview.busy||scenarioWorkReview.busy ? 'disabled' : ''}>载入当前审核状态</button></div>${!report ? '<p>正在核对成片、结构质检和版本…</p>' : `
    <p class="${report.quality_ready ? 'review-info' : 'review-error'}">${esc(report.revision_number ? '编辑版本 v' + report.revision_number : '原始输入清单')} · ${esc(report.quality_scope)}</p><p>${esc(report.reason)}</p>
    <p class="muted">${report.duration === null ? '' : Number(report.duration).toFixed(3) + ' 秒'} ${esc(report.measurements.width || '')}×${esc(report.measurements.height || '')}。审核只适用于这份成片，新生成的成片需要重新审核。</p>
    ${renderScenarioWorkReview()}
    ${report.latest ? `<details open><summary>最近审核意见 · 第 ${report.latest.number} 次</summary><p class="review-text">${esc(report.latest.note || '未填写附加意见')}</p>${renderReviewIssues(report.latest.issues)}</details>` : '<p>尚无人工审核记录。</p>'}
    ${workReview.conflict ? '<p class="review-error">审核状态已变化，原意见保留。请载入当前状态后重新审阅。</p>' : ''}
    ${workReview.pending ? '<div class="editor-draft"><p>审核结果尚待核对，原操作标识和意见已保留。</p><button id="checkWorkReviewBtn" type="button" class="small-action">核对原提交</button><button id="repeatWorkReviewBtn" type="button" class="small-action">沿用原提交继续</button></div>' : ''}
    ${report.can_review ? `<form id="workReviewForm"><label>本次审核意见<textarea id="workReviewNote" maxlength="4000" rows="3" ${disabled}>${esc(workReview.form.note)}</textarea></label><div id="workReviewIssueForms">${workReview.form.issues.map((row,index) => `<div class="review-issue" data-review-issue="${index}"><div class="clip-inputs"><label>问题类型<select data-review-field="category" ${disabled}>${reviewOptions(REVIEW_CATEGORIES,row.category)}</select></label><label>程度<select data-review-field="severity" ${disabled}>${reviewOptions(REVIEW_SEVERITIES,row.severity)}</select></label></div><label>问题说明<textarea data-review-field="message" maxlength="2000" rows="2" ${disabled}>${esc(row.message)}</textarea></label><div class="clip-inputs"><label>起点秒数（可留空）<input data-review-field="start" value="${esc(row.start)}" ${disabled}></label><label>终点秒数（可留空）<input data-review-field="end" value="${esc(row.end)}" ${disabled}></label></div><button type="button" class="small-action" data-remove-review-issue="${index}" ${disabled}>移除此条</button></div>`).join('')}</div>
      <div class="editor-toolbar"><button type="button" id="addWorkReviewIssueBtn" class="small-action" ${disabled}>添加问题（可标记播放时间）</button><button type="button" id="approveWorkReviewBtn" class="small-action" ${disabled} ${changed} ${report.quality_ready ? '' : 'disabled'}>批准这份成片</button><button type="button" id="returnWorkReviewBtn" class="small-action" ${disabled} ${changed}>退回修改</button><button type="button" id="discardWorkReviewBtn" class="small-action" ${disabled}>放弃未保存意见</button></div></form>` : '<p class="muted">当前仅空间所有者可审核；可查看审核意见。</p>'}
    <div class="editor-toolbar">${report.origin_job_id ? '<button id="editReviewedWorkBtn" type="button" class="small-action">继续编辑当前计划</button>' : ''}<button id="workReviewHistoryBtn" type="button" class="small-action">查看审核历史</button><button id="downloadReviewedWorkBtn" type="button" class="small-action" ${disabled} ${report.can_download ? '' : 'disabled'}>领取已批准的视频</button></div>
    ${workReview.showHistory ? `<div>${workReview.history.map(row => `<article class="review-issue"><strong>第 ${row.number} 次 · ${esc(reviewLabel(row.decision))}</strong><small>${esc(row.created_at)}</small><p class="review-text">${esc(row.note)}</p>${renderReviewIssues(row.issues)}</article>`).join('') || '<p>尚无审核记录</p>'}${workReview.historyMore ? '<button id="moreWorkReviewsBtn" class="small-action" type="button">加载更早记录</button>' : ''}</div>` : ''}
    ${workReview.downloadUrl ? `<a href="${esc(workReview.downloadUrl)}" target="_blank" rel="noopener" download>打开下载地址（5分钟内有效）↗</a>` : ''}`}`;
  bindScenarioWorkReview(panel);
  panel.querySelector('#refreshWorkReviewBtn')?.addEventListener('click', refreshWorkReview);
  panel.querySelector('#workReviewForm')?.addEventListener('submit', event => event.preventDefault());
  panel.querySelectorAll('#workReviewNote, [data-review-field]').forEach(input => input.addEventListener('input', rememberWorkReview));
  panel.querySelector('#addWorkReviewIssueBtn')?.addEventListener('click', () => {if (blocked || workReview.form.issues.length >= 50) return; rememberWorkReview(); const video = document.querySelector('#previewVideo'); workReview.form.issues.push({category: 'content', severity: 'error', message: '', start: video?.src ? String(Number(video.currentTime || 0).toFixed(3)) : '', end: ''}); try {saveWorkReviewCache();} catch (_) {showToast('草稿暂时只能保留本页');} renderWorkReview();});
  panel.querySelectorAll('[data-remove-review-issue]').forEach(button => button.addEventListener('click', () => {if (blocked) return; rememberWorkReview(); workReview.form.issues.splice(Number(button.dataset.removeReviewIssue),1); try {saveWorkReviewCache();} catch (_) {showToast('草稿暂时只能保留本页');} renderWorkReview();}));
  panel.querySelectorAll('[data-review-seek]').forEach(button => button.addEventListener('click', () => {const video = document.querySelector('#previewVideo'); const time = Number(button.dataset.reviewSeek); if (video?.src && time >= 0 && time <= (report?.duration || 0)) video.currentTime = time;}));
  panel.querySelector('#approveWorkReviewBtn')?.addEventListener('click', () => submitWorkReview('approved'));
  panel.querySelector('#returnWorkReviewBtn')?.addEventListener('click', () => submitWorkReview('changes_requested'));
  panel.querySelector('#discardWorkReviewBtn')?.addEventListener('click', () => {if (blocked) return; workReview.form = {note: '', issues: []}; try {saveWorkReviewCache();} catch (_) {showToast('草稿存储暂不可用');} renderWorkReview();});
  panel.querySelector('#checkWorkReviewBtn')?.addEventListener('click', () => recoverWorkReview(false));
  panel.querySelector('#repeatWorkReviewBtn')?.addEventListener('click', () => recoverWorkReview(true));
  panel.querySelector('#workReviewHistoryBtn')?.addEventListener('click', () => loadWorkReviewHistory(false));
  panel.querySelector('#moreWorkReviewsBtn')?.addEventListener('click', () => loadWorkReviewHistory(true));
  panel.querySelector('#downloadReviewedWorkBtn')?.addEventListener('click', downloadReviewedWork);
  if(report&&typeof openDeliveryCenter==='function'){
    const button=document.createElement('button');button.className='small-action';button.type='button';button.textContent='交付文件与分享';button.disabled=blocked;button.addEventListener('click',()=>{if(!scenarioReviewBlocked())openDeliveryCenter(workReview.artifactId);});panel.appendChild(button);
    const compare=document.createElement('button');compare.className='small-action';compare.type='button';compare.textContent='比较另一版本';compare.addEventListener('click',()=>openWorkComparison(workReview.artifactId));panel.appendChild(compare);
  }
  panel.querySelector('#editReviewedWorkBtn')?.addEventListener('click', continueReviewedEditing);
}
function rememberWorkReview() {
  if (!reviewMatches() || workReview.pending || workReview.busy || scenarioReviewBlocked() || !document.querySelector('#workReviewNote')) return;
  workReview.form = {note: document.querySelector('#workReviewNote').value, issues: [...document.querySelectorAll('[data-review-issue]')].map(row => Object.fromEntries([...row.querySelectorAll('[data-review-field]')].map(input => [input.dataset.reviewField, input.value])))};
  try {saveWorkReviewCache();} catch (_) {showToast('审核草稿暂时只能保留本页，请检查存储后再离开');}
}
function workReviewBody(decision) {
  const time = raw => {if (!raw.trim()) return null; const value = Number(raw); if (!Number.isFinite(value) || value < 0) throw new Error('问题时间需为非负秒数'); return value;};
  const issues = workReview.form.issues.map(row => {if (!row.message.trim()) throw new Error('请填写每条问题说明，或移除空条目'); return {...row, start: time(row.start), end: time(row.end)};});
  if (decision === 'changes_requested' && !workReview.form.note.trim() && !issues.length) throw new Error('退回时请填写意见或添加问题');
  if (decision === 'approved' && issues.some(row => row.severity === 'error')) throw new Error('仍有错误级问题，请退回修改或移除已解决的问题');
  return {binding_hash: workReview.report.binding_hash, base_review_id: workReview.report.latest?.id || null, decision, note: workReview.form.note, issues};
}
async function submitWorkReview(decision) {
  if (!reviewMatches() || !workReview.report?.can_review || workReview.pending || workReview.busy || workReview.loading || workReview.conflict || scenarioReviewBlocked()) return;
  try {rememberWorkReview(); const body = workReviewBody(decision); const row = {context: workReview.context, artifactId: workReview.artifactId, body, key: 'review:' + globalThis.sha256(JSON.stringify(body))}; await sendWorkReview(row);} catch (error) {showToast(friendlyApiError(error));}
}
async function sendWorkReview(row) {
  if (!reviewMatches() || row.context !== workReview.context || row.artifactId !== workReview.artifactId || workReview.busy || scenarioReviewBlocked()) return;
  if (workReview.pending && workReview.pending.key !== row.key) throw new Error('请先核对原审核提交');
  const previous = workReview.pending; workReview.pending = row; try {saveWorkReviewCache();} catch (error) {workReview.pending = previous; throw error;} const generation = workReview.generation; workReview.busy = true; workReview.downloadUrl = ''; renderWorkReview();
  try {const result = await api(`/artifacts/${encodeURIComponent(row.artifactId)}/reviews`, {method:'POST', headers:{'Idempotency-Key':row.key}, body:JSON.stringify(row.body)});
    if (!reviewMatches() || generation !== workReview.generation) return;
    workReview.report = result.current; workReview.form = {note:'',issues:[]}; workReview.pending = null; saveWorkReviewCache(); workReview.conflict = false; workReview.history = []; workReview.showHistory = false; showToast('审核决定已保存');
  } catch (error) {if (reviewMatches() && generation === workReview.generation) {if (error.status >= 400 && error.status < 500) {workReview.pending = null; workReview.conflict = error.status === 409 || error.status === 403; saveWorkReviewCache();} showToast(friendlyApiError(error));}}
  finally {if (reviewMatches() && generation === workReview.generation) {workReview.busy = false; renderWorkReview();}}
}
async function recoverWorkReview(repeat) {
  const row = workReview.pending; if (!row || workReview.busy || scenarioWorkReview.busy || scenarioWorkReview.loading || !reviewMatches()) return;
  if (repeat) return sendWorkReview(row);
  const generation = workReview.generation; workReview.busy = true; renderWorkReview();
  try {const result = await api(`/artifacts/${encodeURIComponent(row.artifactId)}/review-requests/${encodeURIComponent(row.key)}`);
    if (!reviewMatches() || generation !== workReview.generation) return;
    workReview.report = result.current; if (result.found) {workReview.pending = null; workReview.form = {note:'',issues:[]}; saveWorkReviewCache();} showToast(result.found ? '已核对原审核提交' : '尚未找到原回执，可沿用原提交继续');
  } catch (error) {if (reviewMatches()) showToast(friendlyApiError(error));}
  finally {if (reviewMatches() && generation === workReview.generation) {workReview.busy = false; renderWorkReview();}}
}
async function loadWorkReviewHistory(more) {
  if (!reviewMatches()) return; const generation = workReview.generation; const offset = more ? workReview.historyOffset : 0;
  try {const rows = await api(`/artifacts/${encodeURIComponent(workReview.artifactId)}/reviews?skip=${offset}&limit=20`); if (!reviewMatches() || generation !== workReview.generation) return;
    const all = [...(more ? workReview.history : []), ...rows]; workReview.history = all.filter((row,index) => all.findIndex(item => item.id === row.id) === index); workReview.showHistory = true; workReview.historyOffset = offset + rows.length; workReview.historyMore = rows.length === 20; renderWorkReview();
  } catch (error) {if (reviewMatches()) showToast(friendlyApiError(error));}
}
async function downloadReviewedWork() {
  if (!reviewMatches() || !workReview.report?.can_download || workReview.pending || workReview.busy || workReview.loading || scenarioReviewBlocked()) return; const generation = workReview.generation;
  try {const access = await api(`/artifacts/${encodeURIComponent(workReview.artifactId)}/access`, {method:'POST',body:JSON.stringify({purpose:'download'})}); if (!reviewMatches() || generation !== workReview.generation) return; workReview.downloadUrl = safeRemoteUri(access.url); renderWorkReview();} catch (error) {if (reviewMatches()) showToast(friendlyApiError(error));}
}
async function continueReviewedEditing() {
  if (!reviewMatches() || !workReview.report?.origin_job_id) return;
  if (planEditor.draft || planEditor.busy || pendingOperations().length) return showToast('请先处理编辑器中未保存的修改或原提交');
  const generation = workReview.generation; const job = await api(`/jobs/${encodeURIComponent(workReview.report.origin_job_id)}`); if (!reviewMatches() || generation !== workReview.generation) return;
  workspaceState.planJob = job; workspaceState.openPlanJobId = job.id; renderPlanResult(job); await loadPlanEditor(job,true); document.querySelector('#planEditor')?.scrollIntoView({behavior:'smooth',block:'start'});
}

function scenarioReviewBlocked(){const state=scenarioWorkReview;return state.busy||state.loading||Boolean(state.pending)||state.staleReport;}
function scenarioReviewCacheKey(){return 'video-ai-studio.scenario-review.v1.'+scenarioWorkReview.context+':'+scenarioWorkReview.artifactId;}
function saveScenarioWorkReview(){localStorage.setItem(scenarioReviewCacheKey(),JSON.stringify({form:scenarioWorkReview.form,pending:scenarioWorkReview.pending}));}
function scenarioReviewMatches(context,artifactId,generation){return reviewMatches()&&context===workReview.context&&artifactId===workReview.artifactId&&generation===workReview.generation&&scenarioWorkReview.context===context&&scenarioWorkReview.artifactId===artifactId&&scenarioWorkReview.projectId===selectedProjectId;}
async function loadScenarioWorkReview(context,artifactId,generation){
  const state=scenarioWorkReview,projectId=workReview.report?.project_id;if(!projectId||projectId!==selectedProjectId)return;
  if(state.context!==context||state.artifactId!==artifactId){Object.assign(state,{context,projectId,artifactId,data:null,profiles:[],form:null,pending:null,notice:'',busy:false});try{const saved=JSON.parse(localStorage.getItem(scenarioReviewCacheKey())||'{}');state.form=saved.form||null;const row=saved.pending;state.pending=row?.context===context&&row.artifactId===artifactId&&row.projectId===projectId&&row.body?.artifact_id===artifactId?row:null;}catch(_){}}
  state.loading=true;
  try{const base='/projects/'+encodeURIComponent(projectId)+'/quality';const results=await Promise.allSettled([api(base+'/artifacts/'+encodeURIComponent(artifactId)),api(base+'/scenarios')]);if(!scenarioReviewMatches(context,artifactId,generation))return;const [media,profiles]=results;if(media.status==='rejected')throw media.reason;if(media.value.artifact_id!==artifactId)throw new Error('成片质量回执不匹配，请重新载入');state.data=media.value;state.profiles=profiles.status==='fulfilled'?profiles.value.profiles||[]:[];if(state.data.scenario)state.form=ScenarioReviewForms.draft(state.form,state.data);else state.form=null;state.notice='';}
  catch(error){if(scenarioReviewMatches(context,artifactId,generation)&&(state.data?.scenario||state.pending))state.notice='场景评审暂不可用：'+friendlyApiError(error);}
  finally{if(scenarioReviewMatches(context,artifactId,generation))state.loading=false;}
}
function renderScenarioWorkReview(){
  const state=scenarioWorkReview,data=state.data;if(!data?.scenario&&!state.pending)return '';
  const blocked=workReview.busy||workReview.loading||Boolean(workReview.pending)||state.busy||state.loading||Boolean(state.pending),latest=data?.reviews?.[0];
  return `<section class="card scenario-review"><h3>场景质量评审</h3><p class="muted">逐项播放并作出真实人工判断；初始均为尚未审阅，评分可留空。每项证据绑定当前成片。保存评审后仍需单独批准成片；此记录不代表商业许可或发布准入。</p>${data?.scenario?`<p>场景 ${esc(data.scenario.id)} · ${esc(data.scenario.version)} · 提示版本 ${esc(data.scenario.prompt_version)}</p><p class="small muted">文件指纹 ${esc(data.output_sha256)}</p><p class="muted">${data.has_audio===true?'已测得音轨':data.has_audio===false?'已测得无音轨':'音轨状态尚未核实'}</p>`:''}<p class="status">${esc(state.notice)}</p>${state.staleReport?'<p class="review-error">场景评审已保存，请刷新当前审核状态后再批准或领取。</p>':''}${(data?.technical_findings||[]).map(row=>`<p class="review-${row.severity==='error'?'error':'info'}">技术问题 · ${esc(row.code)}：${esc(row.message)}</p>`).join('')}${latest?`<details><summary>最近场景评审 · ${esc(ScenarioReviewForms.verdict(latest.assessment?.verdict))}${latest.stale?' · 文件已变化，记录失效':''}</summary><p>${esc(latest.created_at||'')}</p>${(latest.review?.dimensions||[]).map(row=>`<p>${esc(ScenarioReviewForms.label(row.dimension))}：${esc(ScenarioReviewForms.statusLabel(row.status))}${row.score!=null?' · '+esc(row.score)+' / 5':''} · ${esc(row.note||'')}</p>`).join('')}</details>`:'<p class="muted">尚无场景人工评审记录。</p>'}${state.pending?'<div class="editor-draft"><p>场景评审结果待确认，原键、文件指纹和正文已保存。</p><button id="checkScenarioReviewBtn" class="small-action" '+(state.busy?'disabled':'')+'>核对原场景评审</button> <button id="repeatScenarioReviewBtn" class="small-action" '+(state.busy?'disabled':'')+'>重放原场景评审</button></div>':''}${data?.scenario&&state.form&&workReview.report?.can_review?state.form.dimensions.map((row,index)=>`<div class="review-issue" data-scenario-dimension="${index}"><label>${esc(ScenarioReviewForms.label(row.dimension))}<select data-scenario-field="status" ${blocked?'disabled':''}>${ScenarioReviewForms.choices(data,state.profiles,row.dimension).map(option=>`<option value="${option.id}" ${option.id===row.status?'selected':''}>${esc(option.label)}</option>`).join('')}</select></label><label>人工评分（0至5，可留空）<input data-scenario-field="score" value="${esc(row.score)}" inputmode="decimal" ${blocked||['unreviewed','not_applicable'].includes(row.status)?'disabled':''}></label><label>判断依据或修改建议<textarea data-scenario-field="note" maxlength="2000" ${blocked?'disabled':''}>${esc(row.note)}</textarea></label></div>`).join('')+`<button id="saveScenarioReviewBtn" class="small-action" ${blocked?'disabled':''}>保存场景评审（不批准成片）</button>`:''}</section>`;
}
function rememberScenarioWorkReview(){const state=scenarioWorkReview;if(!reviewMatches()||state.busy||state.pending||workReview.busy||workReview.loading||workReview.pending||!state.form)return;const panel=document.querySelector('#workReviewPanel');for(const node of panel.querySelectorAll('[data-scenario-dimension]')){const index=Number(node.dataset.scenarioDimension),row=state.form.dimensions[index];if(!row)continue;for(const field of node.querySelectorAll('[data-scenario-field]'))row[field.dataset.scenarioField]=field.value;if(['unreviewed','not_applicable'].includes(row.status))row.score='';}try{saveScenarioWorkReview();}catch(_){state.notice='评审草稿暂时只能保留本页，请检查存储后再提交';}}
function bindScenarioWorkReview(panel){panel.querySelectorAll('[data-scenario-field]').forEach(input=>{input.addEventListener('input',rememberScenarioWorkReview);input.addEventListener('change',()=>{rememberScenarioWorkReview();if(input.dataset.scenarioField==='status')renderWorkReview();});});panel.querySelector('#saveScenarioReviewBtn')?.addEventListener('click',submitScenarioWorkReview);panel.querySelector('#checkScenarioReviewBtn')?.addEventListener('click',()=>recoverScenarioWorkReview(false));panel.querySelector('#repeatScenarioReviewBtn')?.addEventListener('click',()=>recoverScenarioWorkReview(true));}
async function submitScenarioWorkReview(){const state=scenarioWorkReview;if(!reviewMatches()||!workReview.report?.can_review||workReview.busy||workReview.loading||workReview.pending||state.busy||state.loading||state.pending)return;try{rememberScenarioWorkReview();const body=ScenarioReviewForms.body(state.data,state.form,state.profiles);await sendScenarioWorkReview({context:state.context,projectId:state.projectId,artifactId:state.artifactId,key:'quality:'+(globalThis.crypto?.randomUUID?.()||Date.now()+'-'+Math.random().toString(36).slice(2)),body});}catch(error){showToast(friendlyApiError(error));}}
async function sendScenarioWorkReview(row,repeat=false){
  const state=scenarioWorkReview,context=workReview.context,artifactId=workReview.artifactId,generation=workReview.generation;if(!scenarioReviewMatches(context,artifactId,generation)||row.context!==context||row.artifactId!==artifactId||row.projectId!==state.projectId||state.busy||state.loading||workReview.busy||workReview.loading||workReview.pending)return;
  if(state.pending&&(!repeat||state.pending.key!==row.key||JSON.stringify(state.pending.body)!==JSON.stringify(row.body)))throw new Error('请先核对原场景评审提交');const previous=state.pending;state.pending=row;try{saveScenarioWorkReview();}catch(error){state.pending=previous;throw error;}state.busy=true;state.notice='';workReview.downloadUrl='';renderWorkReview();let confirmed=false;
  try{await api('/projects/'+encodeURIComponent(row.projectId)+'/quality/reviews',{method:'POST',headers:{'Idempotency-Key':row.key},body:JSON.stringify(row.body)});if(!scenarioReviewMatches(context,artifactId,generation))return;state.staleReport=true;confirmed=true;state.pending=null;state.notice='场景评审已保存，请核对更新后的成片审核状态';try{saveScenarioWorkReview();}catch(_){state.pending=row;state.notice='服务已保存评审，本地回执暂未更新，请核对原提交';}}
  catch(error){if(scenarioReviewMatches(context,artifactId,generation)){if(!repeat&&error.status>=400&&error.status<500){state.pending=null;saveScenarioWorkReview();}state.notice=state.pending?'结果尚未确认，保留原键与正文。'+friendlyApiError(error):friendlyApiError(error);}}
  finally{if(scenarioReviewMatches(context,artifactId,generation)){state.busy=false;renderWorkReview();}}
  if(confirmed&&scenarioReviewMatches(context,artifactId,generation))await refreshWorkReview();
}
async function recoverScenarioWorkReview(repeat=false){
  const state=scenarioWorkReview,row=state.pending,context=workReview.context,artifactId=workReview.artifactId,generation=workReview.generation;if(!row||state.busy||workReview.busy||workReview.pending||!scenarioReviewMatches(context,artifactId,generation))return;if(repeat)return sendScenarioWorkReview(row,true);state.busy=true;renderWorkReview();let confirmed=false;
  try{const result=await api('/projects/'+encodeURIComponent(row.projectId)+'/quality/reviews/requests/'+encodeURIComponent(row.key));if(!scenarioReviewMatches(context,artifactId,generation))return;if(result.found&&result.result){state.staleReport=true;confirmed=true;state.pending=null;try{saveScenarioWorkReview();}catch(_){state.pending=row;state.notice='服务已保存评审，本地回执暂未更新，请核对原提交';}}state.notice=confirmed?'已核对原场景评审，请核对当前成片审核状态':'尚未找到原回执，可重放原键与原正文';}
  catch(error){if(scenarioReviewMatches(context,artifactId,generation))state.notice=friendlyApiError(error);}
  finally{if(scenarioReviewMatches(context,artifactId,generation)){state.busy=false;renderWorkReview();}}
  if(confirmed&&scenarioReviewMatches(context,artifactId,generation))await refreshWorkReview();
}
