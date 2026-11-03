// Shared classic-script scope; loaded in web/manifest.json order.
function remoteAssetKind(uri) {
  const clean = String(uri || '').split(/[?#]/)[0].toLowerCase();
  if (/\.(mp4|mov|m4v|webm|avi|mkv)$/.test(clean)) return 'video';
  if (/\.(mp3|wav|m4a|aac|flac|ogg)$/.test(clean)) return 'audio';
  if (/\.(jpg|jpeg|png|webp|gif|heic|avif)$/.test(clean)) return 'image';
  return 'video';
}
async function registerRemoteAsset() {
  const input = document.querySelector('#remoteAssetUri');
  const uri = String(input?.value || '').trim();
  if (!uri) return showToast('请输入远程素材 URI');
  if (!safeRemoteUri(uri)) return showToast('URI 必须使用 HTTPS、S3、GS、OSS 或 Azure 对象存储协议');
  await ensureTenant();
  if (!tenantId) return showToast('后端暂不可用，请稍后重试');
  if (!selectedProjectId) return showToast('请先选择或创建一个项目');
  const rawName = uri.split(/[?#]/)[0].split('/').pop() || '远程素材';
  const name = (() => { try { return decodeURIComponent(rawName).slice(0, 200); } catch (_) { return rawName.slice(0, 200); } })();
  const button = document.querySelector('#registerRemoteAssetBtn');
  if (button) { button.disabled = true; button.dataset.label = button.textContent; button.textContent = '登记中…'; }
  try {
    await api('/assets/register-remote', {method: 'POST', body: JSON.stringify({
      name,
      kind: remoteAssetKind(uri),
      uri,
      project_id: selectedProjectId,
      metadata: {source: 'external_uri', remote: true, upload: {status: 'ready', remote: true}},
    })});
    if (input) input.value = '';
    showToast('远程素材已登记，可直接交给外部 Provider 处理');
    await loadWorkspace();
  } catch (error) {
    showToast(`登记失败：${friendlyApiError(error)}`);
  } finally {
    if (button) { button.disabled = false; button.textContent = button.dataset.label || '登记远程素材'; }
  }
}
document.querySelector('#registerRemoteAssetBtn')?.addEventListener('click', registerRemoteAsset);
function assetIsReady(asset) {
  const storage = asset?.metadata?.storage;
  if (storage?.mode === 'external_presigned') return storage.status === 'ready';
  return asset?.metadata?.upload?.status === 'ready' || !String(asset?.uri || '').startsWith('upload://');
}


function assetType(asset) {
  const kind = String(asset?.kind || '').toLowerCase();
  if (kind.startsWith('video')) return 'video';
  if (kind.startsWith('image')) return 'image';
  if (kind.startsWith('audio')) return 'audio';
  return kind || 'other';
}

function assetDuration(asset) {
  const analysis = asset?.metadata?.analysis || {};
  const value = Number(analysis.duration ?? asset?.metadata?.duration ?? 0);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function renderAssetList() {
  const list = document.querySelector('#assetList');
  const filtering = Boolean(workspaceState.assetQuery || workspaceState.assetTag || workspaceState.assetKind);
  const navCount = document.querySelector('#navAssetCount'); if (navCount) navCount.textContent = filtering ? '筛选中' : String(workspaceState.assets.length);
  const summary = document.querySelector('#assetSummary');
  if (!list) return;
  // Search results must not discard selections from the same project.
  if (summary) summary.textContent = `${filtering ? '筛选结果 ' : ''}${workspaceState.assets.length} 个素材 · 已选 ${workspaceState.selectedAssetIds.size} 个`;
  if (!workspaceState.assets.length) {
    list.innerHTML = `<div class="empty-state">${workspaceState.assetQuery || workspaceState.assetTag || workspaceState.assetKind ? '没有匹配的素材，可调整筛选条件。' : '当前项目还没有素材。点击“添加素材”上传视频。'}</div>`;
    updateWorkspaceControls();
    return;
  }
  list.innerHTML = workspaceState.assets.map(asset => {
    const type = assetType(asset); const selected = workspaceState.selectedAssetIds.has(asset.id);
    const ready = assetIsReady(asset);
    const duration = assetDuration(asset);
    const tags = Array.isArray(asset.metadata?.user_tags) ? asset.metadata.user_tags : [];
    return `<label class="asset-card${selected ? ' selected' : ''}"><input type="checkbox" data-asset-id="${esc(asset.id)}" ${selected ? 'checked' : ''} aria-label="选择 ${esc(asset.name)}"><span class="asset-thumb ${esc(type)}">${type === 'video' ? '▶' : type === 'audio' ? '♫' : type === 'image' ? '▧' : '◇'}</span><span class="asset-copy"><strong title="${esc(asset.name)}">${esc(asset.name)}</strong><small>${esc(type.toUpperCase())}${duration ? ` · ${duration.toFixed(1)}s` : ''}${tags.length ? ` · ${esc(tags.join('、'))}` : ''}</small></span><span class="asset-state${ready ? '' : ' pending'}">${ready ? '就绪' : '待上传'}</span></label>`;
  }).join('');
  list.querySelectorAll('input[data-asset-id]').forEach(input => input.addEventListener('change', () => {
    if (input.checked) workspaceState.selectedAssetIds.add(input.dataset.assetId); else workspaceState.selectedAssetIds.delete(input.dataset.assetId);
    renderAssetList();
  }));
  updateWorkspaceControls();
}

// Keep the revision captured when the form opened. A 409 must never be retried
// against a silently refreshed revision, which would overwrite another client.
let assetLabelEdit = null;
document.querySelector('#assetSearchForm')?.addEventListener('submit', async event => {
  event.preventDefault();
  workspaceState.assetQuery = String(document.querySelector('#assetSearch')?.value || '').trim();
  workspaceState.assetTag = String(document.querySelector('#assetTagSearch')?.value || '').trim();
  workspaceState.assetKind = document.querySelector('#assetKindFilter')?.value || '';
  await loadWorkspace();
});

document.querySelector('#editAssetLabelsBtn')?.addEventListener('click', async () => {
  const ids = [...workspaceState.selectedAssetIds];
  if (!ids.length) return showToast('请先选择要整理的素材');
  const tenant = tenantId; const project = selectedProjectId;
  try {
    const rows = await Promise.all(ids.map(id => api(`/assets/${encodeURIComponent(id)}`)));
    if (tenant !== tenantId || project !== selectedProjectId) return;
    assetLabelEdit = {tenant, project, rows};
    const single = rows.length === 1;
    document.querySelector('#assetRenameLabel').hidden = !single;
    document.querySelector('#assetNoteLabel').hidden = !single;
    document.querySelector('#assetRename').value = single ? rows[0].name : '';
    document.querySelector('#assetTags').value = single ? (rows[0].metadata?.user_tags || []).join(', ') : '';
    document.querySelector('#assetNote').value = single ? rows[0].metadata?.user_note || '' : '';
    document.querySelector('#assetTagsLabel').textContent = single ? '标签（逗号分隔）' : `为 ${rows.length} 个素材追加标签（保留原标签）`;
    document.querySelector('#assetLabelsStatus').textContent = '';
    document.querySelector('#saveAssetLabelsBtn').disabled = false;
    document.querySelector('#assetLabelsForm').hidden = false;
  } catch (error) {showToast(friendlyApiError(error));}
});

document.querySelector('#cancelAssetLabelsBtn')?.addEventListener('click', () => {
  assetLabelEdit = null; document.querySelector('#assetLabelsForm').hidden = true;
});

document.querySelector('#assetLabelsForm')?.addEventListener('submit', async event => {
  event.preventDefault();
  const edit = assetLabelEdit;
  if (!edit || edit.busy || edit.tenant !== tenantId || edit.project !== selectedProjectId) return;
  const tags = String(document.querySelector('#assetTags').value).split(/[,，、\n]/).map(s => s.trim()).filter(Boolean);
  const single = edit.rows.length === 1;
  const name = String(document.querySelector('#assetRename').value).trim();
  const note = String(document.querySelector('#assetNote').value);
  if (single && !name) return showToast('请填写素材名称');
  edit.busy = true;
  document.querySelector('#saveAssetLabelsBtn').disabled = true;
  const status = document.querySelector('#assetLabelsStatus');
  status.textContent = '正在保存…';
  let saved = 0; const failed = [];
  try {
    for (const asset of edit.rows) {
      if (edit.tenant !== tenantId || edit.project !== selectedProjectId) break;
      const metadata = asset.metadata || {};
      try {
        const result = await api(`/assets/${encodeURIComponent(asset.id)}/labels`, {method: 'PATCH', body: JSON.stringify({
          base_revision: Number(metadata.user_labels_revision || 0),
          ...(single ? {name} : {}), tags: single ? tags : [...new Set([...(metadata.user_tags || []), ...tags])],
          note: single ? note : metadata.user_note || '',
        })});
        workspaceState.assetCache[result.id] = result;
        saved += 1;
      } catch (error) {failed.push(`${asset.name}：${friendlyApiError(error)}`);}
    }
    if (edit.tenant !== tenantId || edit.project !== selectedProjectId) return;
    status.textContent = `${saved} 个已保存${failed.length ? `；${failed.join('；')}。请取消后重新打开，再审阅冲突内容。` : ''}`;
    if (!failed.length) {document.querySelector('#assetLabelsForm').hidden = true; assetLabelEdit = null; showToast(`${saved} 个素材已整理`);}
    // Disable further saves from this snapshot, including an unknown POST/PATCH
    // outcome. Explicitly reopen the form before another edit.
    edit.finished = true;
    await loadWorkspace();
  } finally {
    edit.busy = Boolean(edit.finished);
    if (assetLabelEdit === edit) document.querySelector('#saveAssetLabelsBtn').disabled = Boolean(edit.finished);
  }
});
