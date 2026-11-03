import {message, personalSession} from '../../shared/api';
import {Asset, ClientError, object} from '../../shared/contracts';
import {addSelectedAsset, selectedAssets, toggleAsset} from '../../shared/asset-selection';
import {savePreparation} from '../../shared/preparation-state';
import {contentType, currentUploads, LocalMedia, recoverUploads, subscribeUploads, uploadMedia, verifyPending} from '../../shared/uploads';

let sequence = 0;
let unsubscribe: (() => void) | null = null;
let labelSnapshot: Asset[] = [];
let labelContext = '';
let labelFinished = false;
function currentContext(): string {return `${personalSession.profile?.id || ''}:${personalSession.profile?.tenant_id || ''}:${personalSession.activeProject()}`;}
function kind(asset: Asset): string {return asset.kind.startsWith('video') ? '视频' : asset.kind.startsWith('image') ? '图片' : asset.kind.startsWith('audio') ? '音频' : '素材';}
function ready(asset: Asset): boolean {const storage = object(asset.metadata.storage); return storage.mode === 'external_presigned' ? storage.status === 'ready' : object(asset.metadata.upload).status === 'ready' || !asset.uri.startsWith('upload://');}
function chooseFiles(count: number): Promise<LocalMedia[]> {
  return new Promise((resolve, reject) => wx.chooseMessageFile({count, type: 'all',
    success: result => resolve(result.tempFiles.map(file => ({path: file.path, name: file.name, size: file.size, contentType: contentType(file.name, file.type)}))),
    fail: () => reject(new ClientError('selection_cancelled', '未选择文件'))}));
}

Page({
  data: {items: [] as Array<{id: string; name: string; kind: string; status: string; tags: string; selected: boolean}>,
    uploads: [] as Array<{id: string; name: string; label: string; error: string; percent: number | null; busy: boolean; ready: boolean; canVerify: boolean}>,
    status: '', loading: false, uploading: false, signedIn: false, projectSelected: false, loadedContext: '',
    query: '', tag: '', kindIndex: 0, kindNames: ['所有类型', '视频', '图片', '音频'], analyze: true,
    selectedCount: 0, more: false, offset: 0, editing: false, editSingle: false, editName: '', editTags: '', editNote: '', editStatus: '', saving: false, editFinished: false},
  async onShow() {
    if (unsubscribe) unsubscribe();
    unsubscribe = subscribeUploads(() => this.renderUploads());
    this.renderUploads(); await this.load();
  },
  onUnload() {unsubscribe?.(); unsubscribe = null; sequence += 1; labelSnapshot = [];},
  async onPullDownRefresh() {try {await this.load();} finally {wx.stopPullDownRefresh();}},
  renderUploads() {this.setData({uploads: currentUploads().map(row => ({id: row.id, name: row.name, label: row.label,
    error: row.error, percent: row.percent, busy: row.busy, ready: row.stage === 'ready', canVerify: Boolean(row.assetId)}))});},
  async load(append = false) {
    const loadSequence = ++sequence;
    if (!personalSession.profile) this.setData({items: [], uploads: [], signedIn: false});
    this.setData({loading: true, status: ''});
    try {
      const profile = personalSession.profile || await personalSession.restore();
      const project = personalSession.activeProject(); const context = currentContext();
      if (loadSequence !== sequence) return;
      if (this.data.loadedContext !== context) {
        labelSnapshot = []; append = false;
        this.setData({items: [], loadedContext: context, query: '', tag: '', kindIndex: 0, editing: false, offset: 0});
      }
      this.setData({signedIn: Boolean(profile), projectSelected: Boolean(project)}); this.renderUploads();
      if (!profile || !project) return this.setData({items: [], more: false, selectedCount: 0});
      const offset = append ? this.data.offset : 0;
      let path = `/assets?project_id=${encodeURIComponent(project)}&skip=${offset}&limit=50`;
      if (this.data.query.trim()) path += '&q=' + encodeURIComponent(this.data.query.trim());
      if (this.data.tag.trim()) path += '&tag=' + encodeURIComponent(this.data.tag.trim());
      if (this.data.kindIndex) path += '&media_type=' + ['','video','image','audio'][this.data.kindIndex];
      const assets = await personalSession.request<Asset[]>(path);
      if (loadSequence !== sequence || currentContext() !== context) return;
      const selected = new Set(selectedAssets());
      const rows = assets.map(asset => ({id: asset.id, name: asset.name, kind: kind(asset), status: ready(asset) ? '就绪' : '待上传',
        tags: Array.isArray(asset.metadata.user_tags) ? asset.metadata.user_tags.join('、') : '', selected: selected.has(asset.id)}));
      this.setData({items: append ? [...this.data.items, ...rows] : rows, offset: offset + assets.length,
        more: assets.length === 50, selectedCount: selected.size});
      try {await recoverUploads();} catch (error) {if (context === currentContext()) this.setData({status: '素材已加载；上传记录同步未完成：' + message(error)});}
    } catch (error) {if (loadSequence === sequence) this.setData({status: message(error)});}
    finally {if (loadSequence === sequence) this.setData({loading: false});}
  },
  queryInput(event: WechatMiniprogram.Input) {this.setData({query: event.detail.value});},
  tagInput(event: WechatMiniprogram.Input) {this.setData({tag: event.detail.value});},
  kindChange(event: WechatMiniprogram.PickerChange) {this.setData({kindIndex: Number(event.detail.value)});},
  async search() {await this.load();},
  async loadMore() {if (!this.data.loading && this.data.more) await this.load(true);},
  toggleSelection(event: WechatMiniprogram.TouchEvent) {
    const id = String(event.currentTarget.dataset.id); toggleAsset(id);
    const selected = new Set(selectedAssets());
    this.setData({selectedCount: selected.size, items: this.data.items.map(item => ({...item, selected: selected.has(item.id)}))});
  },
  analyzeChange(event: WechatMiniprogram.SwitchChange) {this.setData({analyze: event.detail.value});},
  openBatch(){wx.navigateTo({url:'/pages/batches/index'});},
  analysisQuote(){const ids=selectedAssets();if(!ids.length)return this.setData({status:'请先选择已上传的素材'});savePreparation('analysis',{asset_ids:ids,shot_analysis:true,transcribe:true,whisper_model:'small',timeout_seconds:30,max_attempts:3,force_new:false});wx.navigateTo({url:'/pages/billing/index?prepare=analysis'});},
  async runUploads(files: LocalMedia[], expectedId?: string) {
    if (this.data.uploading) return;
    const context = currentContext(); this.setData({uploading: true, status: ''}); let completed = 0;
    try {
      for (const file of files) {
        if (context !== currentContext()) break;
        try {
          const assetId = await uploadMedia(file, this.data.analyze, expectedId); completed += 1;
          if (context === currentContext()) addSelectedAsset(assetId);
        } catch (_) { /* Every file retains its own recovery record. */ }
      }
      if (context !== currentContext()) return;
      await this.load(); this.setData({status: `${completed} 个素材已上传，未完成项可在上传记录中继续。`});
    } finally {this.setData({uploading: false});}
  },
  async chooseMedia() {
    if (this.data.uploading) return;
    const context = currentContext();
    try {
      const result = await new Promise<WechatMiniprogram.ChooseMediaSuccessCallbackResult>((resolve, reject) => wx.chooseMedia({
        count: 9, mediaType: ['image', 'video'], sourceType: ['album', 'camera'], sizeType: ['original'], success: resolve,
        fail: () => reject(new ClientError('selection_cancelled', '未选择素材'))}));
      if (context !== currentContext()) return this.setData({status: '项目已变化，请重新选择素材'});
      await this.runUploads(result.tempFiles.map(file => {
        const name = file.tempFilePath.split('/').pop() || (file.fileType === 'video' ? '视频.mp4' : '图片.jpg');
        return {path: file.tempFilePath, name, size: file.size, contentType: contentType(name, file.fileType)};
      }));
    } catch (error) {this.setData({status: message(error)});}
  },
  async chooseFiles() {
    const context = currentContext();
    try {const files = await chooseFiles(9); if (context === currentContext()) await this.runUploads(files);}
    catch (error) {this.setData({status: message(error)});}
  },
  async reselect(event: WechatMiniprogram.TouchEvent) {
    const id = String(event.currentTarget.dataset.id);
    const context = currentContext();
    try {const files = await chooseFiles(1); if (context === currentContext()) await this.runUploads(files, id);}
    catch (error) {this.setData({status: message(error)});}
  },
  async verify(event: WechatMiniprogram.TouchEvent) {
    try {await verifyPending(String(event.currentTarget.dataset.id)); await this.load();}
    catch (error) {this.setData({status: message(error)});}
  },
  async openLabels() {
    const ids = selectedAssets(); if (!ids.length) return this.setData({status: '请先选择素材'});
    const context = currentContext();
    try {
      const rows: Asset[] = [];
      for (const id of ids) {
        if (context !== currentContext()) return;
        rows.push(await personalSession.request<Asset>('/assets/' + encodeURIComponent(id)));
      }
      if (context !== currentContext()) return;
      labelSnapshot = rows; labelContext = context; labelFinished = false;
      const single = rows.length === 1; const metadata = rows[0].metadata;
      this.setData({editing: true, editFinished: false, editSingle: single, editName: single ? rows[0].name : '',
        editTags: single && Array.isArray(metadata.user_tags) ? metadata.user_tags.join(', ') : '',
        editNote: single ? String(metadata.user_note || '') : '', editStatus: ''});
    } catch (error) {this.setData({status: message(error)});}
  },
  closeLabels() {if (this.data.saving) return; labelSnapshot = []; this.setData({editing: false});},
  nameInput(event: WechatMiniprogram.Input) {this.setData({editName: event.detail.value});},
  tagsInput(event: WechatMiniprogram.Input) {this.setData({editTags: event.detail.value});},
  noteInput(event: WechatMiniprogram.Input) {this.setData({editNote: event.detail.value});},
  async saveLabels() {
    if (this.data.saving || labelFinished || labelContext !== currentContext() || !labelSnapshot.length) return;
    const single = labelSnapshot.length === 1;
    const name = this.data.editName.trim(); if (single && !name) return this.setData({editStatus: '请填写素材名称'});
    const tags = this.data.editTags.split(/[,，、\n]/).map(s => s.trim()).filter(Boolean);
    this.setData({saving: true, editStatus: '正在保存…'}); let saved = 0; const failed: string[] = [];
    try {
      for (const asset of labelSnapshot) {
        if (labelContext !== currentContext()) return;
        const metadata = asset.metadata;
        try {
          await personalSession.request<Asset>(`/assets/${encodeURIComponent(asset.id)}/labels`, 'POST', {
            base_revision: Number(metadata.user_labels_revision || 0), ...(single ? {name} : {}),
            tags: single ? tags : [...new Set([...(Array.isArray(metadata.user_tags) ? metadata.user_tags : []), ...tags])],
            note: single ? this.data.editNote : String(metadata.user_note || '')});
          saved += 1;
        } catch (error) {failed.push(`${asset.name}：${message(error)}`);}
      }
      if (labelContext !== currentContext()) return;
      labelFinished = true;
      this.setData({editFinished: true});
      this.setData({editStatus: `${saved} 个已保存${failed.length ? '；' + failed.join('；') + '。请关闭后重新打开整理面板。' : ''}`, editing: failed.length > 0});
      await this.load();
    } finally {this.setData({saving: false});}
  },
  chooseProject() {wx.switchTab({url: '/pages/create/index'});},
  continueCreate() {wx.navigateTo({url: '/pages/studio/index'});},
  login() {wx.navigateTo({url: '/pages/login/index'});},
});
