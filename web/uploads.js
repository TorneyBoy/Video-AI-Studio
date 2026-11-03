// Resumable upload metadata only. Signed URLs and file bytes are never cached.
const uploadRecords = new Map();
const uploadFileRefs = new Map();

function renderUploadQueue() {
  const target = document.querySelector('#uploadQueue'); if (!target) return;
  const rows = [...uploadRecords.values()].filter(r => r.tenant === tenantId && r.project === selectedProjectId);
  target.innerHTML = rows.map(row => `<article class="upload-record${row.error ? ' error' : ''}"><div><strong>${esc(row.name)}</strong><small>${esc(row.label || '等待上传')}</small>${row.error ? `<small>${esc(row.error)}</small>` : ''}</div>${row.percent === null ? '' : `<progress max="100" value="${row.percent}" aria-label="${esc(row.label)}"></progress>`}${row.assetId && !row.busy && row.stage !== 'ready' ? `<button class="small-action" type="button" data-verify-upload="${esc(row.id)}">核验已有上传</button>` : ''}${!row.busy && row.stage !== 'ready' ? `<button class="small-action" type="button" data-reselect-upload="${esc(row.id)}">重新选择原文件</button>` : ''}</article>`).join('');
  target.querySelectorAll('[data-verify-upload]').forEach(button => button.addEventListener('click', () => verifyUploadRecord(button.dataset.verifyUpload)));
  target.querySelectorAll('[data-reselect-upload]').forEach(button => button.addEventListener('click', () => {
    const record = uploadRecords.get(button.dataset.reselectUpload);
    if (record) pendingReselection = record.id;
    document.querySelector('#fileInput')?.click();
  }));
}

function updateUpload(record, values) {Object.assign(record, values); renderUploadQueue();}

async function fingerprintFile(file, progress) {
  const hash = globalThis.sha256.create();
  const chunkSize = 1024 * 1024;
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    const chunk = await file.slice(offset, offset + chunkSize).arrayBuffer();
    hash.update(new Uint8Array(chunk));
    progress?.(Math.round(Math.min(file.size, offset + chunk.byteLength) / file.size * 100));
  }
  return hash.hex();
}

async function findUploadByHash(project, hash) {
  const rows = await api(`/assets/upload-intents?project_id=${encodeURIComponent(project)}&sha256=${hash}&include_completed=true&limit=1`);
  return rows[0] || null;
}

async function grantForRecord(record) {
  if (record.tenant !== tenantId || record.project !== selectedProjectId) throw new Error('项目已变化，请回到原项目继续上传');
  const session = await api('/assets/upload-intents', {method: 'POST', headers: {'Idempotency-Key': record.key}, body: JSON.stringify(record.specification)});
  record.assetId = session.asset_id;
  for (const [id, other] of uploadRecords) if (id !== record.id && other.tenant === record.tenant && other.project === record.project && other.assetId === record.assetId && !other.busy) uploadRecords.delete(id);
  return session;
}

function transferFile(file, session, progress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(session.upload_method, session.upload_url);
    request.withCredentials = false;
    request.timeout = 10 * 60 * 1000;
    Object.entries(session.upload_headers || {}).forEach(([key, value]) => request.setRequestHeader(key, value));
    request.upload.onprogress = event => {if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100));};
    request.onload = () => request.status >= 200 && request.status < 300 || request.status === 409
      ? resolve() : reject(new Error(`上传未完成（${request.status}），可以核验或重新选择原文件`));
    request.onerror = request.ontimeout = () => reject(new Error('上传结果尚未确认，可先核验已有上传'));
    let body = file;
    if (session.upload_method === 'POST') {
      body = new FormData();
      Object.entries(session.upload_fields || {}).forEach(([key, value]) => body.append(key, value));
      body.append(session.upload_file_field || 'file', file, file.name);
    }
    // Never attach platform credentials or tenant headers to cloud grants.
    request.send(body);
  });
}

async function verifyRecordObject(record) {
  try {return await api(`/assets/${encodeURIComponent(record.assetId)}/verify-storage-upload`, {method: 'POST'});}
  catch (error) {
    if (!['upload_expired', 'upload_session_expired'].includes(error.code)) throw error;
    await grantForRecord(record);
    return api(`/assets/${encodeURIComponent(record.assetId)}/verify-storage-upload`, {method: 'POST'});
  }
}

async function maybeAnalyzeUpload(record, asset) {
  if (record.tenant !== tenantId || record.project !== selectedProjectId) return;
  if (!record.analyze || assetType(asset) !== 'video' || asset.metadata?.analysis?.completeness === 'complete') return;
  record.label = '已上传；选中素材后查看分析报价并确认';
}

async function verifyUploadRecord(id) {
  const record = uploadRecords.get(id); if (!record || record.busy) return;
  updateUpload(record, {busy: true, percent: null, label: '正在核验已有上传', error: ''});
  try {
    const asset = await verifyRecordObject(record);
    record.stage = 'ready'; record.label = '已上传并核验';
    await maybeAnalyzeUpload(record, asset);
    if (record.tenant === tenantId && record.project === selectedProjectId) {await loadJobs(); await loadWorkspace();}
  } catch (error) {updateUpload(record, {error: friendlyApiError(error), label: '上传尚未完成'});}
  finally {updateUpload(record, {busy: false});}
}

async function discoverPendingUploads(project, tenant) {
  try {
    const intents = await api(`/assets/upload-intents?project_id=${encodeURIComponent(project)}`);
    if (project !== selectedProjectId || tenant !== tenantId) return;
    for (const item of intents) {
      if ([...uploadRecords.values()].some(r => r.assetId === item.asset_id)) continue;
      uploadRecords.set(item.asset_id, {id: item.asset_id, tenant, project, name: item.specification.name,
        key: item.idempotency_key, specification: item.specification, assetId: item.asset_id,
        stage: 'waiting', percent: null, busy: false, analyze: false,
        label: '上次上传未完成，可先核验；原文件丢失时需重新选择'});
    }
    renderUploadQueue();
  } catch (_) { /* A refresh failure cannot erase an existing recoverable record. */ }
}

async function directUploadFile(file, projectId, expectedId = null) {
  const tenant = tenantId;
  const record = expectedId ? uploadRecords.get(expectedId) : {id: `file-${Date.now()}-${Math.random()}`, tenant, project: projectId, name: file.name, stage: 'hashing', percent: 0};
  if (!record || record.tenant !== tenant || record.project !== projectId) throw new Error('上传所属项目已变化，请重新选择');
  uploadRecords.set(record.id, record); uploadFileRefs.set(record.id, file);
  updateUpload(record, {busy: true, error: '', label: '计算文件指纹', stage: 'hashing', analyze: document.querySelector('#analyzeUploads')?.checked === true});
  try {
    const hash = await fingerprintFile(file, percent => updateUpload(record, {percent}));
    if (record.specification && (hash !== record.specification.sha256 || file.size !== record.specification.size_bytes)) throw new Error('所选文件与原上传不一致，请重新选择原文件');
    const existing = record.specification ? null : await findUploadByHash(projectId, hash);
    record.key ||= existing?.idempotency_key || `upload:${projectId}:${hash}`;
    record.specification ||= existing?.specification || {name: file.name, content_type: file.type || `${remoteAssetKind(file.name)}/octet-stream`, size_bytes: file.size, sha256: hash, project_id: projectId};
    record.assetId ||= existing?.asset_id;
    updateUpload(record, {percent: null, label: '准备上传授权'});
    let session;
    try {session = await grantForRecord(record);}
    catch (error) {
      if (error.code !== 'idempotency_input_conflict') throw error;
      const concurrent = await findUploadByHash(projectId, hash); if (!concurrent) throw error;
      record.key = concurrent.idempotency_key; record.specification = concurrent.specification;
      session = await grantForRecord(record);
    }
    let asset;
    if (session.status === 'ready') asset = await api(`/assets/${encodeURIComponent(session.asset_id)}`);
    else {
      try {asset = await verifyRecordObject(record);}
      catch (error) {if (error.code !== 'object_not_uploaded') throw error;}
      if (!asset) {
        if (record.tenant !== tenantId || record.project !== selectedProjectId) throw new Error('项目已变化，请回到原项目继续上传');
        if (Date.parse(session.expires_at) <= Date.now() + 10000) session = await grantForRecord(record);
        updateUpload(record, {stage: 'uploading', percent: 0, label: '正在上传'});
        await transferFile(file, session, percent => updateUpload(record, {percent}));
        updateUpload(record, {stage: 'verifying', percent: null, label: '上传完成，正在核验'});
        asset = await verifyRecordObject(record);
      }
    }
    updateUpload(record, {stage: 'ready', percent: null, label: '已上传并核验'});
    await maybeAnalyzeUpload(record, asset);
    if (projectId === selectedProjectId && tenant === tenantId) workspaceState.selectedAssetIds.add(asset.id);
    return session;
  } catch (error) {updateUpload(record, {stage: 'waiting', percent: null, label: '可恢复上传', error: friendlyApiError(error)}); throw error;}
  finally {uploadFileRefs.delete(record.id); updateUpload(record, {busy: false});}
}

let pendingReselection = null;
document.querySelector('#fileInput')?.addEventListener('change', async event => {
  const files = [...event.target.files]; if (!files.length) return;
  const expected = pendingReselection; pendingReselection = null;
  try {
    await ensureTenant();
    if (!tenantId || !selectedProjectId) throw new Error('请先选择或创建项目');
    const project = selectedProjectId; const tenant = tenantId;
    const config = await api('/storage/config');
    if (!config.direct_upload_configured) throw new Error('上传服务尚未就绪');
    let completed = 0;
    for (const file of files) {
      if (project !== selectedProjectId || tenant !== tenantId) break;
      if (file.size > config.max_upload_bytes) {showToast(`“${file.name}”超过上传限制`); continue;}
      try {await directUploadFile(file, project, expected); completed += 1;}
      catch (_) { /* Every file keeps its own recoverable result; continue the selection. */ }
      if (expected) break;
    }
    if (project === selectedProjectId && tenant === tenantId) {await loadJobs(); await loadWorkspace();}
    showToast(`${completed}个素材已上传，未完成项可在上传记录中继续。`);
  } catch (error) {showToast(friendlyApiError(error));}
  finally {event.target.value = '';}
});
