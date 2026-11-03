// Console entry point. Feature sources are allowlisted in web/manifest.json.
async function loadProjects() {
  try {
    await ensureTenant();
    if (!tenantId) throw new Error('暂时无法连接个人空间');
    await loadAccountSummary();
    projects = await api('/projects');
    if (!projects.some(project => project.id === selectedProjectId)) selectedProjectId = projects[0]?.id || null;
    if (selectedProjectId) localStorage.setItem(PROJECT_KEY, selectedProjectId); else localStorage.removeItem(PROJECT_KEY);
    renderProjects(); await loadJobs(); await loadWorkspace();
    const notice = document.querySelector('#workspaceNotice'); if (notice) { notice.hidden = true; notice.textContent = ''; }
    return true;
  } catch (error) {
    const notice = document.querySelector('#workspaceNotice');
    if (notice) { notice.hidden = false; notice.textContent = `暂时无法同步工作台：${friendlyApiError(error)}。已有内容会保留，可稍后点击同步。`; }
    if (!projects.length) renderProjects();
    renderAssetList(); renderStudioTemplates(); updateWorkspaceControls();
    return false;
  }
}

document.querySelector('#newProjectBtn')?.addEventListener('click', () => {
  const panel = document.querySelector('#newProjectPanel'); if (panel) { panel.hidden = false; panel.scrollIntoView({behavior: 'smooth'}); }
  document.querySelector('#projectNameInput')?.focus?.();
});
document.querySelector('#cancelNewProjectBtn')?.addEventListener('click', () => { document.querySelector('#newProjectPanel').hidden = true; });
document.querySelector('#newProjectForm')?.addEventListener('submit', async event => {
  event.preventDefault();
  const input = document.querySelector('#projectNameInput');
  const button = document.querySelector('#confirmNewProjectBtn');
  const name = String(input?.value || '').trim();
  if (!name || button?.disabled) return;
  if (button) button.disabled = true;
  try {
    await ensureTenant(); if (!tenantId) throw new Error('请先连接个人空间');
    const created = await api('/projects', {method: 'POST', body: JSON.stringify({name})});
    projects = [...projects, created]; await selectProject(created.id);
    input.value = ''; document.querySelector('#newProjectPanel').hidden = true;
    showToast(`已创建项目：${name}`);
  } catch (error) { showToast(error.status ? `项目创建未完成：${friendlyApiError(error)}` : '项目创建结果尚未确认，请先同步项目列表。'); }
  finally {if (button) button.disabled = false;}
});
document.querySelector('#projectStatusFilter')?.addEventListener('change', renderProjects);
document.querySelector('#viewAllBtn')?.addEventListener('click', async () => {if (await loadProjects()) showToast('项目列表已同步。');});
document.querySelector('#refreshAllBtn')?.addEventListener('click', async () => {if (await loadProjects()) showToast('工作台已同步。');});
document.querySelector('#timelineBtn')?.addEventListener('click', () => { document.querySelector('#timeline')?.scrollIntoView({behavior: 'smooth'}); loadJobs(); });
document.querySelector('#studioUploadBtn')?.addEventListener('click', () => document.querySelector('#fileInput')?.click());
document.querySelector('#refreshWorkspaceBtn')?.addEventListener('click', async () => { await loadJobs(); await loadWorkspace(); showToast('素材、任务和模板已刷新。'); });
document.querySelector('#createPlanBtn')?.addEventListener('click', createAutoEditPlan);
document.querySelector('#previewBtn')?.addEventListener('click', () => handleRenderAction('draft'));
document.querySelector('#exportBtn')?.addEventListener('click', () => handleRenderAction('high'));
document.querySelectorAll('.nav-list .nav-item').forEach(link => link.addEventListener('click', () => {
  document.querySelectorAll('.nav-list .nav-item').forEach(item => item.classList.remove('active')); link.classList.add('active');
}));
document.querySelector('#previewVideo')?.addEventListener('error', () => {
  const status = document.querySelector('#renderStatus');
  if (status) status.textContent = '播放未完成，可刷新访问授权后重试；无需重新生成视频。';
});
const todayLabel = document.querySelector('#todayLabel');
if (todayLabel) todayLabel.textContent = new Intl.DateTimeFormat('zh-CN', {year: 'numeric', month: 'long', day: 'numeric', weekday: 'long'}).format(new Date());

const workspaceReady = loadProjects();
