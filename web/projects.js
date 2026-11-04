// Shared classic-script scope; loaded in web/manifest.json order.
function projectStatus(status) {
  const map = {active: ['制作中', 'in-progress'], in_progress: ['制作中', 'in-progress'], review: ['待审核', 'review'], completed: ['已完成', 'done'], done: ['已完成', 'done'], archived: ['已归档', 'muted']};
  return map[status] || [status || '制作中', 'in-progress'];
}
function relativeDate(value) {
  if (!value) return '—';
  const date = new Date(value); if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleDateString('zh-CN', {month: 'short', day: 'numeric'}) + ' ' + date.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
}
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function projectRow(project, index) {
  const [label, cls] = projectStatus(project.status);
  const selected = project.id === selectedProjectId ? ' selected' : '';
  const thumb = ['coral', 'blue-thumb', 'purple-thumb'][index % 3];
  return `<div class="table-row project-row${selected}" data-project-id="${esc(project.id)}" data-project="${esc(project.name)}"><div class="project-name"><div class="project-thumb ${thumb}">✦</div><div><strong>${esc(project.name)}</strong><span>${esc(project.description || '个人创作项目')}</span></div></div><span class="pill ${cls}"><i></i>${esc(label)}</span><span class="muted">${relativeDate(project.updated_at)}</span><div>${selected ? '当前项目' : '继续制作'}</div><button class="row-menu" data-open-project="${esc(project.id)}" aria-label="打开项目 ${esc(project.name)}">→</button></div>`;
}
function renderProjects() {
  const table = document.querySelector('.project-table'); if (!table) return;
  const head = table.querySelector('.table-head');
  const filter = document.querySelector('#projectStatusFilter')?.value || 'all';
  const visible = projects.filter(p => filter === 'all' || p.status === filter || (filter === 'active' && p.status === 'in_progress') || (filter === 'completed' && p.status === 'done'));
  table.innerHTML = (head ? head.outerHTML : '') + (visible.length ? visible.map(projectRow).join('') : `<div class="empty-state">${projects.length ? '该状态下暂无项目。' : '还没有项目，点击“新建项目”开始创作。'}</div>`);
  const count = document.querySelector('.stat-card .stat-value'); if (count) count.textContent = String(projects.length).padStart(2, '0');
  const current = projects.find(item => item.id === selectedProjectId);
  const heading = document.querySelector('.upload-copy h3'); if (heading) heading.textContent = current ? `添加素材到「${current.name}」` : '添加素材到项目';
  const breadcrumb = document.querySelector('#currentProjectName'); if (breadcrumb) breadcrumb.textContent = current?.name || '项目总览';
  table.querySelectorAll('.project-row').forEach(row => row.addEventListener('click', event => { if (!event.target.closest('button')) selectProject(row.dataset.projectId); }));
  table.querySelectorAll('[data-open-project]').forEach(button => button.addEventListener('click', () => selectProject(button.dataset.openProject)));
}
async function selectProject(id) {
  stopWorkspacePolls();
  if (selectedProjectId !== id) {
    if(typeof clearStoryboards==='function')clearStoryboards();
    if(typeof clearShotMatching==='function')clearShotMatching();
    if(typeof clearAiProduction==='function')clearAiProduction();
    if(typeof clearWorkCenter==='function')clearWorkCenter();
    if(typeof clearBilling==='function')clearBilling();
    workspaceState.artifactViewGeneration = (workspaceState.artifactViewGeneration || 0) + 1;
    if (typeof clearWorkReview === 'function') clearWorkReview();
    clearPlanEditor(); workspaceState.openPlanJobId = null;
    loadedJobs = []; selectedJobId = null;
    workspaceState.assets = []; workspaceState.assetCache = {}; workspaceState.templates = []; workspaceState.selectedAssetIds = new Set();
    workspaceState.assetQuery = ''; workspaceState.assetTag = ''; workspaceState.assetKind = '';
    for (const id of ['assetSearch', 'assetTagSearch', 'assetKindFilter']) { const input = document.querySelector(`#${id}`); if (input) input.value = ''; }
    assetLabelEdit = null;
    const labels = document.querySelector('#assetLabelsForm'); if (labels) labels.hidden = true;
    workspaceState.selectedTemplateId = null; workspaceState.planJob = null; workspaceState.timelineId = null;
    workspaceState.previewJob = null; workspaceState.exportJob = null;
    const video = document.querySelector('#previewVideo');
    if (video) { video.pause?.(); video.removeAttribute?.('src'); video.hidden = true; }
    const remote = document.querySelector('#remoteArtifact'); if (remote) { remote.hidden = true; remote.innerHTML = ''; }
    const status = document.querySelector('#renderStatus'); if (status) status.textContent = '';
    renderJobTimeline([]); renderAssetList(); renderPlanResult(null); renderProjectWorks([]);
  }
  selectedProjectId = id; selectedJobId = null; localStorage.setItem(PROJECT_KEY, id); renderProjects();
  renderUploadQueue();
  const project = projects.find(item => item.id === id);
  const heading = document.querySelector('.upload-copy h3'); if (heading) heading.textContent = project ? `添加素材到「${project.name}」` : '添加素材到项目';
  await loadJobs(); await loadWorkspace();
  showToast(`已选择项目：${project?.name || id}`);
}
