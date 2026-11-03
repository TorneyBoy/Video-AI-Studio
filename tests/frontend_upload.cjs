// Network-free client contract: upload acknowledgement loss, original-file
// checks, stable cross-client identity, and no platform credentials to storage.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');

const bytes = Buffer.alloc(2 * 1024 * 1024 + 137, 0x61);
const hash = crypto.createHash('sha256').update(bytes).digest('hex');
const specification = {name: 'first.mp4', content_type: 'video/mp4', project_id: 'project-a', size_bytes: bytes.length, sha256: hash};
const ready = {id: 'asset-a', name: 'first.mp4', project_id: 'project-a', kind: 'video/mp4', uri: 'oss://fixture/immutable',
  metadata: {storage: {mode: 'external_presigned', status: 'ready', sha256: hash}}};
const grants = []; let transfers = 0; let uploaded = false;
const response = (status, body) => ({status, ok: status >= 200 && status < 300, text: async () => JSON.stringify(body)});
class FormData {constructor() {this.fields = [];} append(...field) {this.fields.push(field);}}
class UploadRequest {
  constructor() {this.upload = {}; this.headers = {}; this.status = 0;}
  open(method, url) {assert.equal(method, 'POST'); assert.equal(new URL(url).hostname, 'cloud.example');}
  setRequestHeader(key, value) {this.headers[key.toLowerCase()] = value;}
  send(body) {
    assert.equal(this.withCredentials, false);
    for (const header of ['authorization', 'cookie', 'x-tenant-id']) assert.ok(!(header in this.headers));
    assert.ok(body instanceof FormData);
    assert.equal(body.fields.filter(field => field[0] === 'file').length, 1);
    transfers += 1; uploaded = true;
    this.upload.onprogress({lengthComputable: true, loaded: 1, total: 2});
    this.onerror(); // Cloud stored the bytes, but the client never got the ACK.
  }
}
const storage = new Map([['video-ai-studio.tenant-id', 'tenant-a'], ['video-ai-studio.project-id', 'project-a']]);
const context = {
  console, URL, URLSearchParams, setTimeout, clearTimeout, FormData, XMLHttpRequest: UploadRequest,
  localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
  document: {querySelector: () => null, querySelectorAll: () => [], addEventListener() {}}, window: {},
  fetch: async (url, options = {}) => {
    const path = new URL(url, 'http://local.test').pathname;
    if (path === '/api/v1/assets/upload-intents' && options.method === 'POST') {
      const spec = JSON.parse(options.body); assert.deepEqual(spec, specification);
      grants.push({key: options.headers['Idempotency-Key'], spec});
      return response(201, {asset_id: ready.id, status: uploaded ? 'ready' : 'created',
        upload_method: 'POST', upload_url: 'https://cloud.example/', upload_headers: {},
        upload_fields: {policy: 'ephemeral-policy', 'x-oss-security-token': 'ephemeral-sts'}, upload_file_field: 'file',
        expires_at: new Date(Date.now() + 300000).toISOString()});
    }
    if (path === '/api/v1/assets/upload-intents') return response(200, uploaded ? [{asset_id: ready.id, idempotency_key: grants[0].key, specification}] : []);
    if (path === '/api/v1/assets/asset-a/verify-storage-upload') return uploaded ? response(200, ready) : response(409, {detail: {code: 'object_not_uploaded'}});
    if (path === '/api/v1/assets/asset-a') return response(200, ready);
    throw new Error('Unexpected request: ' + path);
  },
};
context.globalThis = context; vm.createContext(context);
const scripts = JSON.parse(fs.readFileSync(require.resolve('../web/manifest.json'), 'utf8')).scripts;
vm.runInContext(scripts.map(name => fs.readFileSync(require.resolve('../web/' + name), 'utf8')).join('\n') + `
authMode = 'development_header';
// Workspace retrieval is covered by frontend_retry.cjs. Keep this test focused
// on the actual upload recovery path, with no unrelated project API calls.
loadJobs = async () => {}; loadWorkspace = async () => {};
this.__uploads = {fingerprintFile, directUploadFile, verifyUploadRecord,
  rows: () => [...uploadRecords.values()], fileCount: () => uploadFileRefs.size};`, context);

function file(name, data = bytes) {
  return {name, type: 'video/mp4', size: data.length, slice(start, end) {
    assert.ok(end - start <= 1024 * 1024, 'fingerprinting must keep reads bounded');
    const chunk = data.subarray(start, end);
    return {arrayBuffer: async () => chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.byteLength)};
  }};
}
(async () => {
  const client = context.__uploads;
  assert.equal(await client.fingerprintFile(file('first.mp4')), hash);
  await assert.rejects(client.directUploadFile(file('first.mp4'), 'project-a'), /尚未确认/);
  const row = client.rows()[0];
  assert.equal(row.stage, 'waiting'); assert.equal(transfers, 1); assert.equal(client.fileCount(), 0);
  await assert.rejects(client.directUploadFile(file('wrong.mp4', Buffer.from('different')), 'project-a', row.id), /原上传不一致/);
  assert.equal(transfers, 1); assert.equal(grants.length, 1, 'a wrong file must not renew or replace an upload');
  await client.verifyUploadRecord(row.id);
  assert.equal(row.stage, 'ready'); assert.equal(transfers, 1, 'unknown ACK recovery must not upload again');
  await client.directUploadFile(file('renamed.mp4'), 'project-a');
  assert.equal(transfers, 1, 'same content in the same project reuses its durable upload');
  assert.equal(grants[0].key, grants[1].key);
  assert.equal(grants[1].spec.name, 'first.mp4', 'original intent specification must survive a local rename');
  assert.equal(client.rows().length, 1, 'rediscovered upload rows must be deduplicated');
  const metadata = JSON.stringify(client.rows());
  for (const secret of ['ephemeral-policy', 'ephemeral-sts', 'https://cloud.example/']) assert.ok(!metadata.includes(secret));
  console.log('frontend upload recovery: PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
