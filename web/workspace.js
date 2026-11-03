// Shared classic-script scope; loaded in web/manifest.json order.
function templateById(id) { return workspaceState.templates.find(template => template.id === id) || BUILTIN_TEMPLATES.find(template => template.id === id); }

function renderStudioTemplates() {
  const grid = document.querySelector('#studioTemplateGrid');
  const summary = document.querySelector('#templateSummary');
  const badge = document.querySelector('#templateBadge');
  if (!grid) return;
  if (!workspaceState.templates.length) {
    grid.innerHTML = '<div class="empty-state">暂无模板，请检查后端连接。</div>';
    if (summary) summary.textContent = '暂无可用模板';
    return;
  }
  grid.innerHTML = workspaceState.templates.map((template, index) => {
    const builtin = BUILTIN_TEMPLATES.find(item => item.id === template.content?.id || item.id === template.id);
    const art = builtin?.art || ['reel', 'brand', 'podcast'][index % 3];
    const selected = template.id === workspaceState.selectedTemplateId;
    const slotCount = Array.isArray(template.content?.slots) ? template.content.slots.length : 0;
    return `<button class="studio-template-card${selected ? ' selected' : ''}" data-template-id="${esc(template.id)}" type="button" ${template.unavailable ? 'disabled' : ''}><div class="studio-template-art ${art}"><span>${esc(template.version || '1.0')}</span><b>${esc((template.name || '模板').toUpperCase())}</b></div><strong>${esc(template.name)}</strong><small>${esc(template.unavailable ? '等待模板服务同步' : builtin?.description || `${slotCount || 1} 个镜头槽位`)}</small></button>`;
  }).join('');
  grid.querySelectorAll('[data-template-id]').forEach(card => card.addEventListener('click', () => {
    workspaceState.selectedTemplateId = card.dataset.templateId;
    localStorage.setItem(WORKSPACE_KEY, JSON.stringify({templateId: workspaceState.selectedTemplateId}));
    renderStudioTemplates();
    setFlowStep('template');
    showToast(`已选择“${templateById(workspaceState.selectedTemplateId)?.name || '模板'}”`);
  }));
  const selected = templateById(workspaceState.selectedTemplateId);
  if (summary) summary.textContent = selected ? `${selected.name} · v${selected.version || '1'}` : '选择一个模板开始';
  if (badge) badge.textContent = selected ? '已选择' : '未选择';
  updateWorkspaceControls();
}

function updateWorkspaceControls() {
  const planButton = document.querySelector('#createPlanBtn');
  const hasReadySelection = [...workspaceState.selectedAssetIds].some(id => {
    const asset = workspaceState.assetCache[id] || workspaceState.assets.find(row => row.id === id);
    return asset && assetIsReady(asset) && ['video', 'image'].includes(assetType(asset));
  });
  if (planButton) planButton.disabled = !selectedProjectId || !workspaceState.selectedTemplateId || templateById(workspaceState.selectedTemplateId)?.unavailable || !hasReadySelection || planEditor.busy || pendingOperations().length > 0 || Boolean(editorMatches() && planEditor.draft);
  const hasPlan = Boolean(workspaceState.planJob && ['succeeded', 'completed', 'done'].includes(normalizedStatus(workspaceState.planJob.status)) && Array.isArray(workspaceState.planJob.payload?.plan?.selected) && workspaceState.planJob.payload.plan.selected.length);
  const planBudget = budgetSnapshot(workspaceState.planJob);
  const budgetBlocked = Boolean(planBudget?.blocked);
  const previewButton = document.querySelector('#previewBtn'); const exportButton = document.querySelector('#exportBtn');
  const previewActive = ACTIVE_JOB_STATUSES.has(normalizedStatus(workspaceState.previewJob?.status));
  const exportActive = ACTIVE_JOB_STATUSES.has(normalizedStatus(workspaceState.exportJob?.status));
  const previewTerminalBlocked = ['failed', 'cancelled'].includes(normalizedStatus(workspaceState.previewJob?.status)) && !canRetryJob(workspaceState.previewJob);
  const exportTerminalBlocked = ['failed', 'cancelled'].includes(normalizedStatus(workspaceState.exportJob?.status)) && !canRetryJob(workspaceState.exportJob);
  if (previewButton) { previewButton.disabled = !hasPlan || budgetBlocked || previewActive || previewTerminalBlocked; previewButton.title = budgetBlocked ? '当前任务已超出预算，请调整预算后重试' : previewActive ? '预览任务正在处理中' : previewTerminalBlocked ? (jobFailureMessage(workspaceState.previewJob) || '该任务不可重试') : ''; }
  if (exportButton) { exportButton.disabled = !hasPlan || budgetBlocked || exportActive || exportTerminalBlocked; exportButton.title = budgetBlocked ? '当前任务已超出预算，请调整预算后重试' : exportActive ? '高清任务正在处理中' : exportTerminalBlocked ? (jobFailureMessage(workspaceState.exportJob) || '该任务不可重试') : ''; }
  if (editorBlocked() || (workspaceState.planJob?.payload?.edit_document_id && !editorMatches())) {
    if (previewButton) previewButton.disabled = true;
    if (exportButton) exportButton.disabled = true;
  }
  const support = editorMatches() && planEditor.revision.editor?.advanced_support;
  if (support?.supported === false) for (const [button, job] of [[previewButton, workspaceState.previewJob], [exportButton, workspaceState.exportJob]]) {
    if (button && !job) {button.disabled = true; button.title = support.reason;}
  }
  if (typeof updateNewRenderQuotes === 'function') updateNewRenderQuotes();
}

function setFlowStep(step) {
  const order = ['assets', 'template', 'plan', 'render']; const index = order.indexOf(step);
  document.querySelectorAll('[data-flow-step]').forEach(node => {
    const current = order.indexOf(node.dataset.flowStep);
    node.classList.toggle('active', current === index);
    node.classList.toggle('done', current < index);
  });
}

function renderPlanResult(job) {
  const result = document.querySelector('#planResult'); const summary = document.querySelector('#planSummary'); const badge = document.querySelector('#planBadge'); const budgetNotice = document.querySelector('#planBudgetNotice');
  if (!result) return;
  const plan = job?.payload?.plan || {};
  const selected = Array.isArray(plan.selected) ? plan.selected : [];
  if (!job) {
    result.innerHTML = '<div class="plan-empty">计划生成后会展示入选镜头和质量提示。</div>';
    if (budgetNotice) { budgetNotice.hidden = true; budgetNotice.textContent = ''; budgetNotice.className = 'plan-budget-notice'; }
    if (summary) summary.textContent = '等待生成计划'; if (badge) badge.textContent = '未生成'; updateWorkspaceControls(); return;
  }
  const rows = selected.map((shot, index) => {
    const baseId = String(shot.asset_id || '').split('#')[0]; const asset = workspaceState.assetCache[baseId] || workspaceState.assets.find(item => item.id === baseId);
    const measured = Object.keys(shot.rationale || {}).length > 0;
    return `<div class="plan-shot"><span class="plan-shot-index">${String(index + 1).padStart(2, '0')}</span><span><strong>${esc(asset?.name || shot.asset_id || '未命名镜头')}</strong><small>${esc(shot.slot || '镜头')} · ${measured ? '已有特征的加权得分' : '按素材与镜头顺序匹配，未提供质量评分'}</small></span><span class="plan-score">${measured ? Number(shot.score).toFixed(3) : '—'}</span></div>`;
  }).join('');
  const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
  const failure = jobFailureMessage(job);
  const planRetry = canRetryJob(job) ? `<button class="job-retry-btn" type="button" data-retry-plan-id="${esc(job.id)}">重试计划</button>` : '';
  const failureNotice = failure ? `<div class="plan-failure" role="alert"><strong>计划未完成</strong><span>${esc(failure)}</span>${job.retryable ? '<small>该失败可重试，仍使用原任务上下文。</small>' : ''}${planRetry}</div>` : planRetry;
  result.innerHTML = `${failureNotice}${rows || '<div class="plan-empty">没有匹配到符合模板的镜头，请选择视频素材或重新分析。</div>'}${warnings.map(warning => `<div class="plan-warning">${esc(warning)}</div>`).join('')}`;
  result.querySelector('[data-retry-plan-id]')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = '正在重试…';
    const retried = await retryJob(button.dataset.retryPlanId);
    if (retried) {
      workspaceState.planJob = retried;
      renderPlanResult(retried);
    } else {
      button.disabled = false;
      button.textContent = '重试计划';
    }
  });
  const budget = budgetSnapshot(job);
  if (budgetNotice) {
    if (!budget) { budgetNotice.hidden = true; budgetNotice.textContent = ''; }
    else {
      const parts = [`预算：${budgetStatusLabel(budget)}`];
      const amountOptions = {micros: budget.micros};
      if (budget.used !== undefined && budget.used !== null && budget.limit !== undefined && budget.limit !== null) parts.push(`已用 ${formatBudgetAmount(budget.used, budget.currency, amountOptions)} / ${formatBudgetAmount(budget.limit, budget.currency, amountOptions)}`);
      if (budget.reserved !== undefined && budget.reserved !== null) parts.push(`预占 ${formatBudgetAmount(budget.reserved, budget.currency, amountOptions)}`);
      if (budget.remaining !== undefined && budget.remaining !== null) parts.push(`剩余 ${formatBudgetAmount(budget.remaining, budget.currency, amountOptions)}`);
      if (budget.message) parts.push(String(budget.message));
      budgetNotice.hidden = false;
      budgetNotice.className = `plan-budget-notice ${budget.blocked ? 'blocked' : budget.warning ? 'warning' : 'ok'}`;
      budgetNotice.textContent = parts.join(' · ');
    }
  }
  const status = statusLabel(job.status); if (summary) summary.textContent = `${status} · ${selected.length} 个镜头${job.id ? ` · ${job.id.slice(0, 8)}` : ''}`;
  if (badge) badge.textContent = ['succeeded', 'completed', 'done'].includes(normalizedStatus(job.status)) ? '已完成' : status;
  setFlowStep(['succeeded', 'completed', 'done'].includes(normalizedStatus(job.status)) ? 'plan' : 'template');
  updateWorkspaceControls();
}

async function loadWorkspace() {
  const requestSequence = ++workspaceState.loadSequence;
  const requestTenant = tenantId;
  const requestProject = selectedProjectId;
  const isCurrentWorkspace = () => requestSequence === workspaceState.loadSequence && requestTenant === tenantId && requestProject === selectedProjectId;
  const preservingWorkspace = workspaceState.tenantId === requestTenant && workspaceState.projectId === requestProject;
  if (!preservingWorkspace) {
    workspaceState.assetCache = {};
    workspaceState.assets = []; workspaceState.templates = []; workspaceState.selectedAssetIds = new Set(); workspaceState.selectedTemplateId = null;
    workspaceState.planJob = null; workspaceState.timelineId = null; workspaceState.previewJob = null; workspaceState.exportJob = null;
  }
  if (!selectedProjectId || !tenantId) {
    workspaceState.tenantId = null; workspaceState.projectId = null;
    workspaceState.templates = BUILTIN_TEMPLATES.map(item => ({...item}));
    renderAssetList(); renderStudioTemplates(); updateWorkspaceControls(); return;
  }
  try {
    const params = new URLSearchParams({project_id: requestProject});
    if (workspaceState.assetQuery) params.set('q', workspaceState.assetQuery);
    if (workspaceState.assetTag) params.set('tag', workspaceState.assetTag);
    if (workspaceState.assetKind) params.set('media_type', workspaceState.assetKind);
    const assets = await api(`/assets?${params}`);
    if (!isCurrentWorkspace()) return;
    workspaceState.assets = assets;
    for (const asset of assets) workspaceState.assetCache[asset.id] = asset;
  } catch (_) { if (!isCurrentWorkspace()) return; if (!preservingWorkspace) workspaceState.assets = []; }
  try {
    const templates = await api(`/templates?project_id=${encodeURIComponent(requestProject)}`);
    if (!isCurrentWorkspace()) return;
    workspaceState.templates = templates;
    if (!workspaceState.templates.length) {
      const seeded = await api(`/projects/${encodeURIComponent(requestProject)}/template-presets`, {method: 'POST'});
      if (!isCurrentWorkspace()) return;
      workspaceState.templates = seeded;
    }
  } catch (_) {
    if (!isCurrentWorkspace()) return;
    if (!preservingWorkspace || !workspaceState.templates.length) workspaceState.templates = BUILTIN_TEMPLATES.map(item => ({...item, project_id: requestProject, unavailable: true}));
  }
  if (!isCurrentWorkspace()) return;
  workspaceState.tenantId = requestTenant;
  workspaceState.projectId = requestProject;
  const saved = JSON.parse(localStorage.getItem(WORKSPACE_KEY) || '{}');
  const preferredTemplateId = preservingWorkspace && workspaceState.templates.some(item => item.id === workspaceState.selectedTemplateId) ? workspaceState.selectedTemplateId : saved.templateId;
  workspaceState.selectedTemplateId = workspaceState.templates.some(item => item.id === preferredTemplateId) ? preferredTemplateId : workspaceState.templates[0]?.id || null;
  if (!preservingWorkspace) workspaceState.selectedAssetIds = new Set(workspaceState.assets.filter(assetIsReady).map(asset => asset.id));
  // ``loadJobs`` can legitimately be stale during a project switch or a
  // network outage.  Filter defensively before restoring plan/render state.
  const workspaceJobs = loadedJobs.filter(job => (!job.project_id || job.project_id === requestProject) && (!job.tenant_id || job.tenant_id === requestTenant));
  const priorPlan = workspaceJobs.find(job => job.type === 'auto_edit.plan' && job.id === workspaceState.openPlanJobId) || workspaceJobs.filter(job => job.type === 'auto_edit.plan').sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))[0];
  if (priorPlan) {
    workspaceState.planJob = priorPlan;
    workspaceState.timelineId = priorPlan.payload?.timeline_id || null;
  } else if (!preservingWorkspace) {
    workspaceState.planJob = null;
    workspaceState.timelineId = null;
  }
  const latestRender = (types) => workspaceJobs
    .filter(job => types.includes(job.type) && renderBelongsToEditor(job))
    .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))[0] || null;
  const previewJob = latestRender(['render.draft']);
  const exportJob = latestRender(['render.standard', 'render.high', 'render.studio']);
  if (previewJob || !preservingWorkspace) workspaceState.previewJob = previewJob;
  if (exportJob || !preservingWorkspace) workspaceState.exportJob = exportJob;
  renderAssetList(); renderStudioTemplates(); renderPlanResult(workspaceState.planJob); setFlowStep(workspaceState.planJob ? 'plan' : 'assets');
  if (workspaceState.previewJob) {
    renderWorkspaceStatus(workspaceState.previewJob, 'draft');
    const status = normalizedStatus(workspaceState.previewJob.status);
    if (ACTIVE_JOB_STATUSES.has(status)) pollWorkspaceRender(workspaceState.previewJob.id, 'draft');
    else if (['succeeded', 'completed', 'done'].includes(status)) loadWorkspaceArtifact(workspaceState.previewJob, 'draft');
  }
  if (workspaceState.exportJob) {
    renderWorkspaceStatus(workspaceState.exportJob, 'high');
    const status = normalizedStatus(workspaceState.exportJob.status);
    if (ACTIVE_JOB_STATUSES.has(status)) pollWorkspaceRender(workspaceState.exportJob.id, 'high');
    else if (['succeeded', 'completed', 'done'].includes(status)) loadWorkspaceArtifact(workspaceState.exportJob, 'high');
  }
  await discoverPendingUploads(requestProject, requestTenant);
  if (isCurrentWorkspace()) await loadPlanEditor(workspaceState.planJob);
}

async function createAutoEditPlan() {
  if (!selectedProjectId) return showToast('请先选择或创建一个项目');
  const template = templateById(workspaceState.selectedTemplateId); if (!template) return showToast('请先选择风格模板');
  const assetIds = [...workspaceState.selectedAssetIds].filter(id => {
    const asset = workspaceState.assetCache[id] || workspaceState.assets.find(row => row.id === id);
    return asset && assetIsReady(asset) && ['video', 'image'].includes(assetType(asset));
  });
  if (!assetIds.length) return showToast('当前自动成片需要至少一个视频或图片素材');
  const button = document.querySelector('#createPlanBtn'); if (button) { button.disabled = true; button.dataset.label = button.textContent; button.textContent = '正在生成…'; }
  const requestProject = selectedProjectId; const requestTenant = tenantId;
  try {
    if (editorMatches() && planEditor.draft) throw new Error('请先保存或放弃当前未保存修改');
    const body = {project_id: requestProject, template_id: template.id, asset_ids: assetIds.slice().sort(),
      target_duration: parsedDuration(document.querySelector('#planTargetDuration')?.value || ''),
      aspect_ratio: document.querySelector('#planAspectRatio')?.value || '9:16', quality: 'draft', max_attempts: 3};
    await openPreparation('plan', body);
    if (requestProject === selectedProjectId && requestTenant === tenantId) showToast('请核对综合报价后确认生成计划。');
  } catch (error) { showToast(`计划创建失败：${friendlyApiError(error)}`); }
  finally { if (button) { button.disabled = false; button.textContent = button.dataset.label || '生成剪辑计划 →'; updateWorkspaceControls(); } }
}

function friendlyApiError(error) {
  const message = String(error?.userMessage || error?.message || error || '未知错误');
  return sanitizeFailureText(message.replace(/^\d+\s+/, '')).slice(0, 120) || '请求失败';
}

async function renderWorkspace(quality) {
  const planJob = workspaceState.planJob;
  if (!planJob) return showToast('请先生成剪辑计划');
  if (!['succeeded', 'completed', 'done'].includes(normalizedStatus(planJob.status))) return showToast(jobFailureMessage(planJob) || '剪辑计划仍在处理中，请稍后再试');
  if (budgetSnapshot(planJob)?.blocked) return showToast('当前任务已超预算，请调整预算后重试');
  const status = document.querySelector('#renderStatus'); const remote = document.querySelector('#remoteArtifact');
  if (remote) { remote.hidden = true; remote.innerHTML = ''; }
  if (status) { status.className = 'render-status'; status.textContent = quality === 'draft' ? '正在准备预览…' : '正在准备高清成片…'; }
  try {
    if (editorBlocked()) throw new Error('请先处理未保存修改、缺少的镜头或待核对提交');
    if (planJob.payload?.edit_document_id && !editorMatches()) throw new Error('请先载入编辑版本');
    if (editorMatches()) {
      const existing = quality === 'draft' ? workspaceState.previewJob : workspaceState.exportJob;
      const support = planEditor.revision.editor?.advanced_support;
      if (support?.supported === false && !existing) throw new Error(support.reason);
      const revision = planEditor.revision; const renderQuality = quality === 'draft' ? 'draft' : 'high';
      await submitTrackedOperation({kind: 'render', documentId: revision.document_id, revisionId: revision.revision_id,
        jobId: planJob.id, key: `render:${renderQuality}:${revision.revision_id}`, label: `生成 v${revision.number} ${quality === 'draft' ? '预览' : '高清成片'}`,
        body: {quality: renderQuality, output: {format: 'mp4'}, options: {source: 'studio-flow'}, max_attempts: 3}});
      return;
    }
    if (!workspaceState.timelineId) {
      const compiled = await api(`/jobs/${encodeURIComponent(planJob.id)}/timeline`);
      workspaceState.timelineId = compiled.id;
    }
    const renderQuality = quality === 'draft' ? 'draft' : 'high';
    const key = `render:${renderQuality}:${planJob.id}`;
    await submitTrackedOperation({kind: 'render', jobId: planJob.id, key, label: '生成原计划成片',
      body: {quality: renderQuality, output: {format: 'mp4'}, options: {source: 'studio-flow'}, max_attempts: 3}});
  } catch (error) { if (status) { status.className = 'render-status error'; status.textContent = `渲染失败：${friendlyApiError(error)}`; } showToast(`渲染失败：${friendlyApiError(error)}`); }
}

function renderWorkspaceStatus(job, quality) {
  const status = document.querySelector('#renderStatus'); if (!status || !job) return;
  const normalized = normalizedStatus(job.status);
  const done = ['succeeded', 'completed', 'done'].includes(normalized); const failed = ['failed', 'cancelled'].includes(normalized);
  status.className = `render-status${done ? ' success' : failed ? ' error' : ''}`;
  const provider = providerSnapshot(job);
  const budget = budgetSnapshot(job);
  const failure = jobFailureMessage(job);
  const parts = [`${quality === 'draft' ? '预览' : '高清成片'}：${statusLabel(job.status)}`, job.id ? `任务 ${job.id.slice(0, 8)}` : '', provider.provider ? `Provider ${provider.provider}` : '', provider.externalJobId ? `远程任务 ${String(provider.externalJobId).slice(0, 14)}` : '', formatUsage(provider.usage), formatCost(provider.cost), budget ? `预算：${budgetStatusLabel(budget)}` : '', failed && failure ? `失败：${failure}` : ''].filter(Boolean);
  status.innerHTML = parts.map(part => `<span>${esc(part)}</span>`).join('<b> · </b>');
  if (budget?.blocked) status.classList.add('budget-blocked'); else if (budget?.warning) status.classList.add('budget-warning');
  const button = quality === 'draft' ? document.querySelector('#previewBtn') : document.querySelector('#exportBtn');
  if (button && ACTIVE_JOB_STATUSES.has(normalized)) {
    button.disabled = true;
    button.textContent = quality === 'draft' ? '正在生成预览…' : '正在导出高清…';
  }
  if (done || failed) {
    if (button) {
      const retryable = canRetryJob(job);
      button.textContent = quality === 'draft' ? (failed ? (retryable ? '重试预览' : '预览失败') : '查看已有预览') : (failed ? (retryable ? '重试导出' : '导出失败') : '审阅已有成片');
      button.dataset.retryJobId = failed && retryable ? job.id : '';
      button.disabled = failed && !retryable;
      button.title = failed && !retryable ? (jobFailureMessage(job) || '该任务不可重试') : '';
    }
  }
  updateWorkspaceControls();
}

async function handleRenderAction(quality) {
  const current = quality === 'draft' ? workspaceState.previewJob : workspaceState.exportJob;
  const status = normalizedStatus(current?.status);
  if (status === 'failed' || status === 'cancelled') {
    if (!canRetryJob(current)) return showToast(jobFailureMessage(current) || '该任务不可重试');
    await retryJob(current.id, {quality});
    return;
  }
  if (current && normalizedStatus(current.status)==='succeeded' && renderBelongsToEditor(current)) { await loadWorkspaceArtifact(current, quality); return; }
  if (typeof openRenderQuote === 'function') { await openRenderQuote(quality); return; }
  await renderWorkspace(quality);
}

function stopWorkspacePoll(jobId) {
  const state = workspacePolls.get(jobId);
  if (state?.timer) clearTimeout(state.timer);
  workspacePolls.delete(jobId);
}
function stopWorkspacePolls() {
  workspacePolls.forEach(state => { if (state.timer) clearTimeout(state.timer); });
  workspacePolls.clear();
  clearTimeout(workspaceRenderTimer);
  workspaceRenderTimer = null;
}
function pollWorkspaceRender(jobId, quality, attempt = 0) {
  if (!jobId) return;
  stopWorkspacePoll(jobId);
  const delay = Math.min(15000, Math.round(3500 * Math.pow(1.25, Math.min(attempt, 8))));
  const state = {jobId, quality, attempt, tenantId, projectId: selectedProjectId, timer: null};
  workspacePolls.set(jobId, state);
  // Keep the job registered while the tab is backgrounded. The visibility
  // handler will attach a timer as soon as the user returns.
  if (document.hidden) return;
  state.timer = setTimeout(async () => {
    if (workspacePolls.get(jobId) !== state) return;
    if (state.tenantId !== tenantId || state.projectId !== selectedProjectId) {
      workspacePolls.delete(jobId);
      return;
    }
    try {
      const job = await api(`/jobs/${encodeURIComponent(jobId)}`);
      if (workspacePolls.get(jobId) !== state) return;
      if (state.tenantId !== tenantId || state.projectId !== selectedProjectId) {
        workspacePolls.delete(jobId);
        return;
      }
      if (!renderBelongsToEditor(job)) {workspacePolls.delete(jobId); return;}
      if (quality === 'draft') workspaceState.previewJob = job; else workspaceState.exportJob = job;
      renderWorkspaceStatus(job, quality);
      const status = normalizedStatus(job.status);
      if (TERMINAL_JOB_STATUSES.has(status)) {
        workspacePolls.delete(jobId);
        if (['succeeded', 'completed', 'done'].includes(status)) await loadWorkspaceArtifact(job, quality);
      } else {
        pollWorkspaceRender(jobId, quality, 0);
      }
    } catch (_) {
      if (workspacePolls.get(jobId) !== state) return;
      if (state.tenantId !== tenantId || state.projectId !== selectedProjectId) {
        workspacePolls.delete(jobId);
        return;
      }
      const nextAttempt = attempt + 1;
      // A temporary gateway/network failure must not make a running job look
      // failed. Retry with bounded backoff, then leave a manual-refresh hint.
      if (nextAttempt <= 8) {
        const status = document.querySelector('#renderStatus');
        if (status) status.innerHTML = '<span>暂时无法同步，正在自动重试…</span>';
        pollWorkspaceRender(jobId, quality, nextAttempt);
      } else {
        workspacePolls.delete(jobId);
        const status = document.querySelector('#renderStatus');
        if (status) { status.className = 'render-status error'; status.textContent = '暂时无法同步任务状态，请点击刷新或稍后重试。'; }
      }
    }
  }, delay);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(jobsRefreshTimer);
    workspacePolls.forEach(state => { if (state.timer) clearTimeout(state.timer); state.timer = null; });
    return;
  }
  scheduleJobsRefresh();
  loadJobs();
  [...workspacePolls.values()].forEach(state => pollWorkspaceRender(state.jobId, state.quality, state.attempt));
});

function renderProjectWorks(artifacts) {
  const target = document.querySelector('#workList'); if (!target) return;
  const rows = artifacts.filter(a => a.kind === 'render' && !['deleted', 'revoked', 'archived_duplicate'].includes(a.delivery_status));
  const count = document.querySelector('#renderCount'); if (count) count.textContent = String(rows.length);
  target.innerHTML = rows.length ? rows.slice().reverse().map(a => {
    const qc = a.metadata?.quality_control || {};
    const allowed = qc.status === 'passed' && a.job_status === 'succeeded' && !a.job_policy_blocked;
    const label = (a.edit_revision_number ? `v${a.edit_revision_number} · ` : '') + (a.metadata?.quality === 'draft' ? '预览视频' : '高清成片');
    const measured = qc.duration_seconds ? `${Number(qc.duration_seconds).toFixed(1)}秒 · ${qc.width}×${qc.height}` : '媒体规格尚未同步';
    return `<article class="work-card"><div><strong>${label}</strong><small>${esc(measured)} · ${esc(relativeDate(a.created_at))}</small></div><button class="small-action" type="button" data-work-id="${esc(a.id)}" ${allowed ? '' : 'disabled'}>${allowed ? '打开审阅' : '等待处理'}</button></article>`;
  }).join('') : '<div class="empty-state">当前项目还没有视频记录。</div>';
  target.querySelectorAll('[data-work-id]').forEach(button => button.addEventListener('click', async () => {
    const item = rows.find(a => a.id === button.dataset.workId);
    const requestTenant = tenantId; const requestProject = selectedProjectId;
    try {
      const job = loadedJobs.find(j => j.id === item?.job_id) || await api(`/jobs/${encodeURIComponent(item.job_id)}`);
      if (requestTenant !== tenantId || requestProject !== selectedProjectId) return;
      await loadWorkspaceArtifact(job, item.metadata?.quality === 'draft' ? 'draft' : 'high', item.id);
      document.querySelector('#previewVideo')?.scrollIntoView({behavior: 'smooth', block: 'center'});
    } catch (error) {showToast(`成片暂不可用：${friendlyApiError(error)}`);}
  }));
}

async function loadWorkspaceArtifact(job, quality, artifactId = null) {
  const view = workspaceState.artifactViewGeneration = (workspaceState.artifactViewGeneration || 0) + 1;
  const requestTenant = tenantId;
  const requestProject = selectedProjectId;
  if (job?.project_id && requestProject && job.project_id !== requestProject) return;
  if (!artifactId && !renderBelongsToEditor(job)) return;
  try {
    const artifacts = await api(`/artifacts?job_id=${encodeURIComponent(job.id)}`);
    const artifact = artifactId ? artifacts.find(item => item.id === artifactId) : artifacts.find(item => String(item.kind || '').includes('video')) || artifacts[0];
    if (view !== workspaceState.artifactViewGeneration || requestTenant !== tenantId || requestProject !== selectedProjectId || (job?.project_id && selectedProjectId && job.project_id !== selectedProjectId)) return;
    if (!artifact?.id) return;
    const video = document.querySelector('#previewVideo');
    if (video) {video.pause?.(); video.removeAttribute?.('src'); video.hidden = true;}
    const review = typeof openWorkReview === 'function' ? await openWorkReview(artifact.id) : null;
    if (view !== workspaceState.artifactViewGeneration || requestTenant !== tenantId || requestProject !== selectedProjectId) return;
    let playbackUri = '';
    let accessFailure = '';
    try {
      if (review && !review.can_preview) throw new Error(review.reason);
      const access = await api(`/artifacts/${encodeURIComponent(artifact.id)}/access`, {method: 'POST',
        body: JSON.stringify({purpose: 'preview'})});
      playbackUri = access.url;
    } catch (error) { accessFailure = friendlyApiError(error); }
    if (view !== workspaceState.artifactViewGeneration || requestTenant !== tenantId || requestProject !== selectedProjectId) return;
    if (!artifactId && !renderBelongsToEditor(job)) return;
    const browserPlayable = /^https:\/\//.test(playbackUri);
    if (video && browserPlayable) { video.src = playbackUri; video.hidden = false; }
    if (typeof renderWorkReview === 'function') renderWorkReview();
    const status = document.querySelector('#renderStatus');
    const provider = providerSnapshot(job, artifact);
    const details = [browserPlayable ? '可播放' : '正在准备领取', formatUsage(provider.usage), formatCost(provider.cost)].filter(Boolean);
    if (status) {
      const current = quality === 'draft' ? workspaceState.previewJob : workspaceState.exportJob;
      if (current?.id === job.id) renderWorkspaceStatus(job, quality);
      else status.textContent = `${quality === 'draft' ? '预览视频' : '高清成片'}：${statusLabel(job.status)}`;
      status.innerHTML = `${status.innerHTML}${details.length ? `<b> · </b>${details.map(item => `<span>${esc(item)}</span>`).join('<b> · </b>')}` : ''}`;
    }
    const remote = document.querySelector('#remoteArtifact');
    if (remote) {
      const uri = safeRemoteUri(playbackUri);
      remote.hidden = false;
      remote.innerHTML = `<div><strong>${quality === 'draft' ? '预览视频' : '高清成片'}</strong><small>链接过期后可重新获取</small></div><div class="remote-artifact-meta">${uri ? `<a href="${esc(uri)}" target="_blank" rel="noopener">打开播放（5分钟内有效）↗</a>` : `<span>${esc(accessFailure || '视频正在归档或等待质检')}</span>`}<button class="small-action" type="button" id="renewArtifactAccess">刷新访问授权</button>${renderProviderMeta(provider)}</div>`;
      remote.querySelector('#renewArtifactAccess')?.addEventListener('click', () => loadWorkspaceArtifact(job, quality, artifact.id));
    }
  } catch (_) { /* artifact listing is optional for local file outputs */ }
}
