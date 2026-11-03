// Shared classic-script scope; loaded in web/manifest.json order.
const JOB_STATUS_LABELS = {queued: '等待中', running: '处理中', processing: '处理中', succeeded: '已完成', completed: '已完成', done: '已完成', failed: '失败', cancelled: '已取消'};
const STEP_STATUS_LABELS = {queued: '等待中', running: '执行中', succeeded: '已完成', failed: '失败', cancelled: '已取消', skipped: '已跳过'};
function jobLabel(type) {
  return ({'asset.analyze': '素材分析', 'auto_edit.plan': '自动剪辑计划', 'render.draft': '生成预览', 'render.standard': '标准渲染', 'render.high': '高清渲染', 'render.studio': '棚拍渲染', 'voice.tts': 'AI 配音', 'avatar.lip_sync': '口型同步', 'avatar.motion_transfer': '动作复刻', 'avatar.face_replace': '人脸替换', 'video.generate': 'AI 视频生成', 'enhancement.restore': '视频增强'})[type] || type || '内容任务';
}
function normalizedStatus(status) { return String(status || '').trim().toLowerCase(); }
function statusLabel(status) { const normalized = normalizedStatus(status); return JOB_STATUS_LABELS[normalized] || status || '未知状态'; }
function normalizedProgress(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  // JobStepRead uses a 0..1 fraction. Accept whole percentages from an
  // external Provider as a defensive compatibility measure.
  return Math.max(0, Math.min(1, numeric > 1 ? numeric / 100 : numeric));
}
function progressPercent(job) {
  const steps = Array.isArray(job.steps) ? job.steps : [];
  if (steps.length) return Math.round(steps.reduce((sum, step) => sum + normalizedProgress(step.progress), 0) / steps.length * 100);
  const status = normalizedStatus(job.status);
  if (['succeeded', 'completed', 'done'].includes(status)) return 100;
  return 0; // No reported measurements means no invented progress.
}
function stepStatusLabel(status) { const normalized = normalizedStatus(status); return STEP_STATUS_LABELS[normalized] || status || '未知'; }
function sanitizeFailureText(value) {
  if (!value) return '';
  return String(value)
    .replace(/(api[-_ ]?key|authorization|bearer|token|secret|password)\s*[:=]\s*[^\s,;]+/gi, '$1=[已隐藏]')
    .replace(/[\r\n]+/g, ' ')
    .trim()
    .slice(0, 240);
}
function jobFailureMessage(job) {
  if (normalizedStatus(job?.status) !== 'failed') return '';
  const payload = job?.payload || {};
  const providerFailure = payload.provider_failure || payload.provider_error || {};
  const details = providerFailure.details && typeof providerFailure.details === 'object' ? providerFailure.details : {};
  const failure = job?.failure && typeof job.failure === 'object' ? job.failure : {};
  return sanitizeFailureText(failure.message || job?.error || providerFailure.message || providerFailure.error || details.message || details.error || job?.failure_code || '');
}
function providerSnapshot(job, artifact) {
  const payload = job?.payload || {};
  const result = payload.provider_result || {};
  const callback = payload.provider_callback || {};
  const failure = payload.provider_failure || {};
  const publicFailure = job?.failure && typeof job.failure === 'object' ? job.failure : {};
  const failureDetails = failure.details && typeof failure.details === 'object' ? failure.details : {};
  const publicFailureDetails = publicFailure.details && typeof publicFailure.details === 'object' ? publicFailure.details : {};
  const metadata = artifact?.metadata || {};
  const model = result.model || metadata.model || {};
  const failed = normalizedStatus(job?.status) === 'failed';
  return {
    provider: result.provider || callback.provider || metadata.provider || publicFailure.provider || failure.provider || '',
    model: result.model_id || model.model_id || model.model || metadata.model_id || '',
    version: result.model_version || model.version || metadata.model_version || '',
    externalJobId: result.external_job_id || callback.external_job_id || payload.provider_job_id || publicFailureDetails.provider_job_id || failureDetails.provider_job_id || '',
    status: result.status || callback.status || metadata.status || '',
    usage: result.usage || callback.usage || metadata.usage || {},
    cost: result.cost || callback.cost || metadata.cost || {},
    artifactUri: result.artifact_uri || callback.artifact_uri || metadata.artifact_uri || artifact?.uri || '',
    warnings: result.warnings || metadata.warnings || [],
    failure: failed ? publicFailure.message || failure.message || failure.error || failureDetails.message || failureDetails.error || '' : '',
    failureCode: failed ? publicFailure.code || failure.code || publicFailureDetails.code || failureDetails.code || job?.failure_code || '' : '',
    failureRetryable: failed && (publicFailure.retryable ?? job?.retryable ?? false),
    failureCapability: failed ? publicFailure.capability || publicFailureDetails.capability || '' : '',
  };
}

function canRetryJob(job) {
  if (!job || normalizedStatus(job.status) !== 'failed' || job.policy_blocked) return false;
  const attempts = Number(job.attempt_count);
  const maxAttempts = Number(job.max_attempts);
  if (Number.isFinite(attempts) && Number.isFinite(maxAttempts) && attempts >= maxAttempts) return false;
  return Boolean(job.retryable || job.failure?.retryable);
}

async function retryJob(jobId, {quality = null, onComplete = null} = {}) {
  if (!jobId) return null;
  const retryButton = quality === 'draft' ? document.querySelector('#previewBtn') : quality ? document.querySelector('#exportBtn') : null;
  if (retryButton) {
    retryButton.disabled = true;
    retryButton.textContent = quality === 'draft' ? '正在重试预览…' : '正在重试导出…';
  }
  try {
    const job = typeof recoverOriginalTask === 'function' ? await recoverOriginalTask(jobId) : await api(`/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
    if (!job) throw new Error('请先核对原任务操作，再查看最新状态');
    const normalized = normalizedStatus(job?.status);
    if (quality) {
      if (quality === 'draft') workspaceState.previewJob = job; else workspaceState.exportJob = job;
      renderWorkspaceStatus(job, quality);
      if (ACTIVE_JOB_STATUSES.has(normalized)) pollWorkspaceRender(job.id, quality);
    }
    await loadJobs();
    if (typeof onComplete === 'function') onComplete(job);
    showToast('已重新排队，将继续使用原任务处理');
    return job;
  } catch (error) {
    if (retryButton) {
      retryButton.disabled = false;
      retryButton.textContent = quality === 'draft' ? '重试预览' : '重试导出';
      updateWorkspaceControls();
    }
    showToast(`任务重试失败：${friendlyApiError(error)}`);
    return null;
  }
}
function formatUsage(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const labels = {input_tokens: '输入 token', output_tokens: '输出 token', total_tokens: '总 token', duration_seconds: '媒体秒数', input_seconds: '输入秒数', output_seconds: '输出秒数', characters: '字符数', requests: '请求数'};
  const values = Object.entries(usage).filter(([key, value]) => labels[key] && value !== null && value !== undefined && value !== '');
  if (!values.length) return '';
  return values.slice(0, 3).map(([key, value]) => `${labels[key]} ${esc(Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 2}) : value)}`).join(' · ');
}
function formatCost(cost) {
  if (cost === null || cost === undefined || cost === '') return '';
  const label = typeof cost === 'object' && (cost.bill_confirmed === false || cost.status === 'calculated') ? '估算费用' : '费用';
  const micros = typeof cost === 'object' ? (cost.amount_micros ?? cost.total_micros ?? cost.micros) : null;
  const value = typeof cost === 'number' ? cost : cost.amount ?? cost.total ?? cost.value ?? cost.cost;
  if (micros !== null && micros !== undefined && micros !== '') return `${label} ${formatBudgetAmount(micros, typeof cost === 'object' ? cost.currency : 'USD', {micros: true})}`;
  if (value === null || value === undefined || value === '') return '';
  const currency = typeof cost === 'object' ? (cost.currency || cost.unit || 'USD') : 'USD';
  const numeric = Number(value);
  return `${label} ${esc(Number.isFinite(numeric) ? `${currency} ${numeric.toFixed(4)}` : `${value} ${currency}`)}`;
}
function budgetObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}
function firstBudgetObject(...values) {
  return values.map(budgetObject).find(value => value && Object.keys(value).length > 0) || null;
}
function boolish(value) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') return ['1', 'true', 'yes', 'blocked', 'exceeded', 'over_budget'].includes(value.toLowerCase());
  return false;
}
function budgetValue(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'object') return value.amount ?? value.total ?? value.value ?? value.limit ?? value.remaining ?? value.amount_micros ?? null;
  return value;
}
function budgetCurrency(value) {
  const code = String(value || 'USD').trim().toUpperCase();
  return /^[A-Z]{3}$/.test(code) ? code : 'USD';
}
function formatBudgetAmount(value, currency = 'USD', {micros = false} = {}) {
  const amount = budgetValue(value);
  if (amount === null || amount === undefined || amount === '') return '';
  const numeric = Number(amount);
  if (!Number.isFinite(numeric)) return `${esc(budgetCurrency(currency))} ${esc(String(amount))}`;
  const units = micros ? numeric / 1000000 : numeric;
  try {
    // Keep the ISO code visible while letting Intl apply the currency's
    // fraction rules (for example JPY has no decimal subunit).
    const rendered = new Intl.NumberFormat('en-US', {style: 'currency', currency: budgetCurrency(currency), currencyDisplay: 'code', maximumFractionDigits: 4}).format(units).replace(/\u00a0/g, ' ');
    return esc(rendered);
  } catch (_) {
    const rendered = units.toLocaleString('zh-CN', {maximumFractionDigits: 4});
    return `${esc(budgetCurrency(currency))} ${esc(rendered)}`;
  }
}
function budgetSnapshot(job, artifact) {
  const payload = job?.payload || {};
  const result = payload.provider_result || {};
  const callback = payload.provider_callback || {};
  const metadata = artifact?.metadata || {};
  const raw = firstBudgetObject(job?.budget, job?.tenant_budget, job?.tenantBudget, payload.budget, payload.tenant_budget, payload.tenantBudget, result.budget, result.tenant_budget, callback.budget, callback.tenant_budget, metadata.budget, metadata.tenant_budget);
  if (!raw) return null;
  const keys = ['status', 'state', 'decision', 'reconciliation_required', 'allowed', 'blocked', 'warning', 'over_budget', 'overLimit', 'exceeded', 'limit', 'budget_limit', 'max', 'used', 'spent', 'consumed', 'remaining', 'available', 'reset_at', 'resetAt', 'period', 'period_key', 'window', 'scope', 'currency', 'message', 'reason', 'enabled', 'monthly_limit_micros', 'task_limit_micros', 'settled_micros', 'monthly_used_micros', 'reserved_micros', 'remaining_micros'];
  if (!keys.some(key => Object.prototype.hasOwnProperty.call(raw, key))) return null;
  const status = String(raw.status || raw.state || raw.decision || '').toLowerCase();
  const pendingReconciliation = boolish(raw.reconciliation_required) || raw.decision === 'amount_unknown' || status === 'pending_reconciliation';
  const blocked = boolish(raw.blocked) || boolish(raw.over_budget) || boolish(raw.overLimit) || boolish(raw.exceeded) || ['blocked', 'exceeded', 'over_budget', 'over-limit'].includes(status) || raw.allowed === false;
  const warning = !blocked && (boolish(raw.warning) || ['warning', 'near_limit', 'near-limit', 'at_risk'].includes(status));
  const micros = ['monthly_limit_micros', 'task_limit_micros', 'settled_micros', 'monthly_used_micros', 'reserved_micros', 'remaining_micros', 'amount_micros'].some(key => Object.prototype.hasOwnProperty.call(raw, key));
  const currency = budgetCurrency(raw.currency || raw.limit?.currency || raw.used?.currency || raw.spent?.currency || 'USD');
  const limit = raw.monthly_limit_micros ?? raw.limit ?? raw.budget_limit ?? raw.max;
  const taskLimit = raw.task_limit_micros;
  const used = raw.settled_micros ?? raw.monthly_used_micros ?? raw.used ?? raw.spent ?? raw.consumed;
  const reserved = raw.reserved_micros ?? raw.reserved;
  const remaining = raw.remaining_micros ?? raw.remaining ?? raw.available;
  const numericLimit = Number(budgetValue(limit));
  const numericUsed = Number(budgetValue(used));
  const ratio = Number.isFinite(numericLimit) && numericLimit > 0 && Number.isFinite(numericUsed) ? numericUsed / numericLimit : null;
  return {raw, status, blocked, warning, pendingReconciliation, enabled: raw.enabled !== false, currency, micros, limit, taskLimit, used, reserved, remaining, ratio, period: raw.period_key || raw.period || raw.window || '', scope: raw.scope || '', message: raw.message || raw.reason || '', resetAt: raw.reset_at || raw.resetAt || ''};
}
function budgetStatusLabel(budget) {
  if (!budget) return '';
  if (budget.pendingReconciliation) return budget.blocked ? '费用待对账，暂缓交付' : '费用待对账 · 预占保留';
  if (budget.blocked) return '已超预算';
  if (budget.enabled === false) return '未启用';
  if (budget.warning) return '接近上限';
  if (budget.status === 'ok' || budget.status === 'allowed' || budget.raw.allowed === true) return '预算正常';
  return budget.status ? String(budget.status) : '已计入预算';
}
function renderBudgetMeta(budget) {
  if (!budget) return '';
  const tone = budget.blocked ? 'blocked' : budget.warning || budget.pendingReconciliation ? 'warning' : 'ok';
  const amountOptions = {micros: budget.micros};
  const entries = [`预算 ${esc(budgetStatusLabel(budget))}`];
  if (budget.used !== undefined && budget.used !== null && budget.limit !== undefined && budget.limit !== null) entries.push(`已用 ${formatBudgetAmount(budget.used, budget.currency, amountOptions)} / ${formatBudgetAmount(budget.limit, budget.currency, amountOptions)}`);
  else if (budget.used !== undefined && budget.used !== null) entries.push(`已用 ${formatBudgetAmount(budget.used, budget.currency, amountOptions)}`);
  if (budget.reserved !== undefined && budget.reserved !== null) entries.push(`预占 ${formatBudgetAmount(budget.reserved, budget.currency, amountOptions)}`);
  if (budget.remaining !== undefined && budget.remaining !== null) entries.push(`剩余 ${formatBudgetAmount(budget.remaining, budget.currency, amountOptions)}`);
  if (budget.period) entries.push(`周期 ${esc(String(budget.period))}`);
  if (budget.scope) entries.push(`范围 ${esc(String(budget.scope))}`);
  if (budget.resetAt) entries.push(`重置 ${esc(relativeDate(budget.resetAt))}`);
  const ratio = budget.ratio !== null ? ` · ${Math.round(Math.max(0, budget.ratio) * 100)}%` : '';
  return `<div class="job-budget-meta ${tone}" role="status" aria-label="预算状态"><span>${entries.join('</span><span>')}</span>${ratio ? `<span>${esc(ratio.slice(3))} 已使用</span>` : ''}${budget.message ? `<strong>${esc(String(budget.message))}</strong>` : ''}</div>`;
}
function renderProviderMeta(provider) {
  const entries = [
    provider.provider && `Provider ${esc(provider.provider)}`,
    provider.model && `模型 ${esc(provider.model)}${provider.version ? ` · ${esc(provider.version)}` : ''}`,
    provider.externalJobId && `远程任务 ${esc(String(provider.externalJobId).slice(0, 18))}`,
    provider.status && `远端状态 ${esc(statusLabel(provider.status))}`,
    provider.failureCapability && `能力 ${esc(provider.failureCapability)}`,
    formatUsage(provider.usage),
    formatCost(provider.cost),
    provider.failureCode && `错误码 ${esc(provider.failureCode)}`,
    provider.failure && `Provider 错误 ${esc(sanitizeFailureText(provider.failure))}`,
  ].filter(Boolean);
  const warnings = Array.isArray(provider.warnings) ? provider.warnings.filter(Boolean).slice(0, 3).map(item => `提示 ${esc(sanitizeFailureText(item))}`) : [];
  entries.push(...warnings);
  if (!entries.length) return '';
  return `<details class="provider-details"><summary>查看处理详情</summary><div class="job-provider-meta" aria-label="处理详情">${entries.map(entry => `<span>${entry}</span>`).join('')}</div></details>`;
}
function safeRemoteUri(uri) {
  const value = String(uri || '').trim();
  return /^(https?:|s3:|gs:|oss:|az:|azure:)/i.test(value) ? value : '';
}
function renderArtifactLink(artifact) {
  if (!artifact?.id || !(artifact.kind === 'render' || String(artifact.kind || '').startsWith('video'))) return '';
  return `<div class="job-artifact-link"><span>任务视频</span><button class="small-action" type="button" data-open-artifact="${esc(artifact.id)}">查看成片</button></div>`;
}
function renderJobDetail(job) {
  const detail = document.querySelector('#jobDetail'); if (!detail || !job) return;
  const percent = progressPercent(job); const steps = Array.isArray(job.steps) ? job.steps : [];
  const attempts = Number.isFinite(Number(job.max_attempts)) ? `尝试 ${Number(job.attempt_count) || 0}/${Number(job.max_attempts)}` : '';
  const retryState = job.policy_blocked ? '策略阻止' : job.retryable && normalizedStatus(job.status) === 'queued' ? `等待重试${job.next_retry_at ? ` · ${relativeDate(job.next_retry_at)}` : ''}` : '';
  const failureCode = normalizedStatus(job.status) === 'failed' && job.failure_code ? `错误码 ${job.failure_code}` : '';
  const meta = [attempts, retryState].filter(Boolean).join(' · ');
  const artifact = Array.isArray(job.artifacts) ? job.artifacts[job.artifacts.length - 1] : null;
  const provider = providerSnapshot(job, artifact);
  const budget = budgetSnapshot(job, artifact);
  const failure = jobFailureMessage(job);
  const retryButton = canRetryJob(job) ? `<button class="job-retry-btn" type="button" data-retry-job-id="${esc(job.id)}">重试此任务</button>` : '';
  detail.hidden = false;
  detail.innerHTML = `<div class="job-detail-head"><div><strong>${esc(jobLabel(job.type))}</strong><small>${esc(job.id)} · ${relativeDate(job.updated_at)}${meta ? ` · ${esc(meta)}` : ''}${failureCode ? ` · ${esc(failureCode)}` : ''}</small></div><span class="job-detail-status">${esc(statusLabel(job.status))}</span></div><div class="job-detail-progress"><div class="progress-bar"><i style="width:${percent}%"></i></div><span>${percent}%</span></div>${renderProviderMeta(provider)}${renderBudgetMeta(budget)}<div class="job-steps">${steps.length ? steps.map(step => `<div class="job-step ${esc(normalizedStatus(step.status))}"><i class="job-step-dot"></i><span class="job-step-name">${esc(step.name)}</span><span class="job-step-progress">${esc(stepStatusLabel(step.status))} · ${Math.round(normalizedProgress(step.progress) * 100)}%</span></div>`).join('') : '<div class="empty-state">该任务尚未报告步骤进度。</div>'}</div>${renderArtifactLink(artifact)}${failure ? `<div class="job-detail-error" role="alert"><strong>失败原因</strong><span>${esc(failure)}</span>${provider.failureRetryable ? '<small>该失败可重试，系统会继续使用原任务上下文。</small>' : ''}${retryButton}</div>` : retryButton}`;
  if (job.type === 'auto_edit.plan' && normalizedStatus(job.status) === 'succeeded') {
    detail.innerHTML += '<button class="small-action" type="button" data-edit-plan>编辑此计划</button>';
    detail.querySelector('[data-edit-plan]')?.addEventListener('click', () => openPlanFromJob(job));
  }
  detail.querySelector('[data-retry-job-id]')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = '正在重试…';
    const retried = await retryJob(button.dataset.retryJobId);
    if (!retried) {
      button.disabled = false;
      button.textContent = '重试此任务';
    }
  });
  detail.querySelector('[data-open-artifact]')?.addEventListener('click', () => loadWorkspaceArtifact(job,
    artifact?.metadata?.quality === 'draft' ? 'draft' : 'high', artifact.id));
}
function renderJobTimeline(jobs) {
  const timeline = document.querySelector('.timeline'); if (!timeline) return;
  if (!jobs.length) { timeline.innerHTML = '<div class="empty-state">当前项目暂无任务。</div>'; document.querySelector('#jobDetail')?.setAttribute('hidden', ''); return; }
  const recent = jobs.slice().reverse().slice(0, 5);
  timeline.innerHTML = recent.map(job => {
    const status = normalizedStatus(job.status);
    const active = ACTIVE_JOB_STATUSES.has(status); const done = ['completed', 'succeeded', 'done'].includes(status); const failed = ['failed', 'cancelled'].includes(status); const percent = progressPercent(job);
    const artifact = Array.isArray(job.artifacts) ? job.artifacts[job.artifacts.length - 1] : null;
    const provider = providerSnapshot(job, artifact);
    const budget = budgetSnapshot(job, artifact);
    const providerLabel = provider.provider ? ` · ${provider.provider}` : '';
    const budgetLabel = budget?.pendingReconciliation ? ' · 费用待对账' : budget?.blocked ? ' · 超预算' : budget?.warning ? ' · 预算预警' : '';
    const budgetClass = budget?.blocked ? ' budget-blocked' : budget?.warning ? ' budget-warning' : '';
    const failure = jobFailureMessage(job);
    const failureLabel = failed && failure ? ` · ${failure}` : '';
    return `<div class="timeline-item ${done ? 'done' : active ? 'active' : failed ? 'failed' : ''}${budgetClass}${job.id === selectedJobId ? ' selected' : ''}" data-job-id="${esc(job.id)}" role="button" tabindex="0" aria-label="${esc(`${jobLabel(job.type)}，${statusLabel(job.status)}`)}"><span class="timeline-dot"></span><div><strong>${esc(jobLabel(job.type))}</strong><small>${esc(statusLabel(job.status))} · ${relativeDate(job.updated_at)}${esc(providerLabel)}${esc(budgetLabel)}${esc(failureLabel)}</small></div><div class="job-progress"><div class="progress-bar"><i style="width:${percent}%"></i></div><span>${percent}%</span></div></div>`;
  }).join('');
  timeline.querySelectorAll('[data-job-id]').forEach(item => {
    const open = () => { selectedJobId = item.dataset.jobId; renderJobTimeline(loadedJobs); renderJobDetail(loadedJobs.find(job => job.id === selectedJobId)); };
    item.addEventListener('click', open); item.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
  });
  if (selectedJobId && recent.some(job => job.id === selectedJobId)) renderJobDetail(loadedJobs.find(job => job.id === selectedJobId));
}
function scheduleJobsRefresh() {
  clearTimeout(jobsRefreshTimer);
  if (document.hidden || !loadedJobs.some(job => ACTIVE_JOB_STATUSES.has(normalizedStatus(job.status)))) return;
  jobsRefreshTimer = setTimeout(() => { loadJobs(); }, 4000);
}
function setLiveState(online) {
  const label = document.querySelector('.live-label');
  if (!label) return;
  label.classList.toggle('offline', !online);
  label.title = online ? '任务状态已同步' : '暂时无法同步任务状态，稍后会自动重试';
  label.innerHTML = `<i></i>${online ? ' 已同步' : ' 同步中断'}`;
}
async function loadJobs() {
  if (!tenantId) return loadedJobs;
  const requestKey = `${tenantId}:${selectedProjectId || ''}`;
  if (jobsLoadPromise) {
    if (jobsLoadKey === requestKey) return jobsLoadPromise;
    // Let the in-flight request finish, then issue a request for the newly
    // selected project instead of displaying the previous project's jobs.
    return jobsLoadPromise.then(() => loadJobs());
  }
  const requestTenant = tenantId;
  const requestProject = selectedProjectId;
  jobsLoadKey = requestKey;
  jobsLoadPromise = (async () => {
    try {
      const query = new URLSearchParams({order: 'desc', limit: '100'}); if (requestProject) query.set('project_id', requestProject);
      const jobs = await api(`/jobs?${query}`);
      jobs.sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));
      const enrichedJobs = await Promise.all(jobs.map(async job => {
        const [steps, artifacts] = await Promise.all([
          api(`/jobs/${encodeURIComponent(job.id)}/steps`).catch(() => []),
          api(`/artifacts?job_id=${encodeURIComponent(job.id)}`).catch(() => []),
        ]);
        return {...job, steps, artifacts};
      }));
      // Ignore a response from a project that the user has already left.
      if (requestTenant !== tenantId || requestProject !== selectedProjectId) return loadedJobs;
      loadedJobs = enrichedJobs;
      const latestPlan = loadedJobs.find(job => job.type === 'auto_edit.plan' && job.id === workspaceState.openPlanJobId) || loadedJobs.filter(job => job.type === 'auto_edit.plan').sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))[0];
      if (latestPlan) {
        workspaceState.planJob = latestPlan;
        workspaceState.timelineId = latestPlan.payload?.timeline_id || workspaceState.timelineId;
        renderPlanResult(latestPlan);
        await loadPlanEditor(latestPlan);
      }
      const pending = loadedJobs.filter(job => ACTIVE_JOB_STATUSES.has(normalizedStatus(job.status))).length;
      const stat = document.querySelectorAll('.stat-card .stat-value')[1]; if (stat) stat.textContent = String(pending).padStart(2, '0');
      renderJobTimeline(loadedJobs);
      if(typeof refreshWorkCenterFromJobs==='function')await refreshWorkCenterFromJobs();
      else renderProjectWorks(enrichedJobs.flatMap(job => (job.artifacts || []).map(artifact => ({...artifact, job_status: job.status, job_policy_blocked: job.policy_blocked}))));
      setLiveState(true);
      scheduleJobsRefresh();
      return loadedJobs;
    } catch (_) {
      setLiveState(false);
      scheduleJobsRefresh();
      if (!loadedJobs.length && !document.hidden) {
        clearTimeout(jobsRefreshTimer);
        jobsRefreshTimer = setTimeout(() => { loadJobs(); }, 10000);
      }
      return loadedJobs;
    } finally {
      if (jobsLoadKey === requestKey) { jobsLoadPromise = null; jobsLoadKey = null; }
    }
  })();
  return jobsLoadPromise;
}
