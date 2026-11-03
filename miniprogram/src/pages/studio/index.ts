import {message, personalSession} from '../../shared/api';
import {ClientError, EditingAction, EditingRevision, Job, Artifact, object, OriginalTranscript, PageResult, PlanSelection, ShotCandidate, SubtitleExport, Template} from '../../shared/contracts';
import {blankSubtitle, SubtitleForm, subtitleForm, subtitleCommand} from '../../shared/subtitle-form';
import {LayerForm, layerForm, layerCommand} from '../../shared/layer-form';
import {sha256} from '../../vendor/sha256';
import {selectedAssets} from '../../shared/asset-selection';
import {savePreparation} from '../../shared/preparation-state';
import {checkStudioOperation, confirmStudioOperation, editorDraft, guardStudio, operation, pendingStudioOperations,
  readStudioCache, removeEditorDraft, saveEditorDraft, sendStudioOperation, studioContext, studioHash, StudioOperation, writeStudioCache} from '../../shared/studio-state';

type ShotView = PlanSelection & {sourceText: string; targetText: string; reason: string; slotName: string};
type CandidateView = ShotCandidate & {timeText: string};
type TrackView = {id: string; name: string; type: string; muted: boolean; index: number; muteText: string; clips: Array<{id: string; label: string; time: string}>};
type SubtitleView = {id: string; trackId: string; text: string; time: string; original: string; anchor: string; partial: boolean};
const active = (job: Job | null): boolean => Boolean(job && ['queued', 'running', 'processing'].includes(job.status));
const statusName = (status: string): string => ({queued: '排队中', running: '处理中', processing: '处理中', succeeded: '已完成', failed: '未完成', cancelled: '已取消'}[status] || status);
function duration(value: string): number | null {
  if (!value.trim()) return null;
  const result = Number(value); if (!Number.isFinite(result) || result < 0.1 || result > 600) throw new ClientError('duration_invalid', '时长需为0.1至600秒');
  return result;
}
function ratio(index: number): '9:16' | '16:9' | '1:1' {return (['9:16', '16:9', '1:1'] as const)[index] || '9:16';}
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let generation = 0;

Page({
  data: {context: '', signedIn: false, projectSelected: false, loading: false, saving: false, status: '',
    templates: [] as Template[], templateIndex: 0, templateNames: [] as string[], selectedCount: 0,
    target: '', aspectIndex: 0, aspects: ['竖屏 9:16', '横屏 16:9', '方形 1:1'],
    plans: [] as Job[], planNames: [] as string[], planIndex: 0, plan: null as Job | null, planStatus: '', planFailure: '', canRetryPlan: false,
    revision: null as EditingRevision | null, shots: [] as ShotView[], warnings: [] as string[],
    editTarget: '', editAspectIndex: 0, draftLabel: '', draftRebased: false,
    pendingLabel: '', candidateSelection: '', candidates: [] as CandidateView[], candidateTotal: 0,
    history: [] as EditingRevision[], showHistory: false, historyMore: false,
    tracks: [] as TrackView[], timing: '', preciseId: '', preciseName: '', preciseBounds: '', preciseLocked: false,
    preciseType: '', preciseIndex: 0, preciseCount: 0, preciseKind: '', hasRejectedDraft: false,
    sourceStart: '', sourceEnd: '', splitOffset: '', clipVolume: '',
    fitNames: ['完整显示', '居中填满裁切', '拉伸填满'], fitValues: ['contain', 'cover', 'stretch'], fitIndex: 0,
    transitionNames: ['无转场（硬切）', '淡化', '溶解', '左擦除', '右擦除', '左滑动', '右滑动'],
    transitionValues: ['none', 'fade', 'dissolve', 'wipeleft', 'wiperight', 'slideleft', 'slideright'], transitionIndex: 0, transitionDuration: '0',
    layerEditing: false, layerForm: layerForm(null), hasLayerDraft: false, layerMissing: false,
    layerSourceIds: [] as string[], layerSourceNames: [] as string[], layerSourceIndex: 0, layerFitIndex: 0,
    layerCommonFields: [{key:'trackId', label:'轨道名称（修改会移动片段）'}, {key:'start', label:'成片起点（秒）'}, {key:'end', label:'成片终点（秒）'}, {key:'sourceStart', label:'素材起点（图片填0，秒）'}],
    layerAudioFields: [{key:'volume',label:'音量倍数（0–4）'}, {key:'fadeIn',label:'淡入时长（秒）'}, {key:'fadeOut',label:'淡出时长（秒）'}],
    layerOverlayFields: [{key:'x',label:'横向位置（像素）'}, {key:'y',label:'纵向位置（像素）'}, {key:'width',label:'宽度（像素）'}, {key:'height',label:'高度（像素）'}, {key:'opacity',label:'透明度（0–1）'}],
    layerRoleNames: ['配乐', '旁白', '其他'], layerRoleIndex: 0, renderSupport: '',
    subtitleCues: [] as SubtitleView[], subtitleLimit: 50, subtitleMore: false, subtitleEditing: false,
    subtitleForm: blankSubtitle(), subtitleMissing: false, hasSubtitleDraft: false,
    subtitleModes: ['保持原关联（仅改文字和样式）', '固定成片时间', '跟随单个视频片段'], subtitleModeIndex: 1,
    subtitleAnchorIds: [] as string[], subtitleAnchorNames: [] as string[], subtitleAnchorIndex: 0,
    originals: [] as Array<OriginalTranscript & {time: string; key: string}>, showOriginals: false, originalMore: false,
    subtitleExportPath: '', subtitleExportName: '', subtitleExportContext: '',
    subtitlePreviewSize: 24, subtitlePreviewColor: 'white',
    renderJob: null as Job | null, renderStatus: '', renderFailure: '', canRetryRender: false, renderReady: false, canRender: false},
  onHide() {this.stopPoll(); generation += 1;},
  onUnload() {this.stopPoll(); generation += 1;},
  async onShow() {await this.load();},
  async onPullDownRefresh() {try {await this.load(true);} finally {wx.stopPullDownRefresh();}},
  stopPoll() {if (pollTimer) clearTimeout(pollTimer); pollTimer = null;},
  schedulePoll() {
    this.stopPoll();
    if (!active(this.data.plan) && !active(this.data.renderJob)) return;
    const view = generation; const context = this.data.context;
    pollTimer = setTimeout(async () => {
      if (view !== generation || context !== studioContext()) return;
      await this.refreshJobs(); this.schedulePoll();
    }, 3000);
  },
  refreshPending() {this.setData({pendingLabel: pendingStudioOperations().map(row => row.label).join('；')}); this.updateControls();},
  updateControls() {const support = this.data.revision?.editor?.advanced_support; this.setData({renderSupport: support?.supported === false ? support.reason : '', canRender: Boolean(this.data.revision && support?.supported !== false && !this.data.saving && !pendingStudioOperations().length && !this.data.draftLabel && !(this.data.revision.plan.missing_required_slots || []).length)});},
  async load(force = false) {
    const view = ++generation; this.stopPoll(); this.setData({loading: true, saving: false, status: ''});
    try {
      const profile = personalSession.profile || await personalSession.restore();
      const context = studioContext(); const project = personalSession.activeProject();
      if (view !== generation) return;
      this.setData({signedIn: Boolean(profile), projectSelected: Boolean(project)});
      if (!profile || !project) {this.setData({layerEditing: false, hasLayerDraft: false, layerForm: layerForm(null), context: '', revision: null, plan: null, shots: [], candidates: [], history: [], renderJob: null, pendingLabel: '', draftLabel: '', canRender: false, subtitleCues: [], subtitleForm: blankSubtitle(), subtitleExportPath: '', originals: []}); return;}
      if (context !== this.data.context) this.setData({layerEditing: false, hasLayerDraft: false, layerForm: layerForm(null), context, plan: null, revision: null, shots: [], candidates: [], history: [], renderJob: null, renderReady: false, draftLabel: '', draftRebased: false, preciseId: '', preciseName: '', tracks: [], preciseKind: '', hasRejectedDraft: false});
      let templates = await personalSession.request<Template[]>('/templates?project_id=' + encodeURIComponent(project));
      guardStudio(context); if (view !== generation) return;
      if (!templates.length) templates = await personalSession.request<Template[]>(`/projects/${encodeURIComponent(project)}/template-presets`, 'POST');
      guardStudio(context); if (view !== generation) return;
      const plans = await personalSession.request<Job[]>(`/jobs?project_id=${encodeURIComponent(project)}&job_type=auto_edit.plan&order=desc&limit=100`);
      guardStudio(context); if (view !== generation) return;
      const saved = readStudioCache();
      const selectedTemplate = Math.max(0, templates.findIndex(item => item.id === saved.templateId));
      this.setData({templates, templateNames: templates.map(item => item.name), templateIndex: selectedTemplate,
        plans, planNames: plans.map(item => `${templates.find(t => t.id === item.payload.template_id)?.name || '剪辑计划'} · ${new Date(item.created_at).toLocaleString()} · ${statusName(item.status)}`),
        selectedCount: selectedAssets().length, target: typeof saved.target === 'string' ? saved.target : '', aspectIndex: Number(saved.aspectIndex || 0)});
      let plan = plans.find(item => item.id === saved.planId) || null;
      if (!plan && typeof saved.planId === 'string') {
        try {plan = await personalSession.request<Job>('/jobs/' + encodeURIComponent(saved.planId));} catch (_) { /* Keep a missing old record separate from a new submission. */ }
      }
      guardStudio(context); if (view !== generation) return;
      plan = plan?.project_id === project ? plan : plans[0] || null;
      if (plan && plan.project_id === project) {
        if (!plans.some(row => row.id === plan!.id)) {
          plans.unshift(plan); this.setData({plans, planNames: plans.map(row => `剪辑计划 · ${new Date(row.created_at).toLocaleString()} · ${statusName(row.status)}`)});
        }
        this.setData({plan, planIndex: Math.max(0, plans.findIndex(row => row.id === plan!.id))}); await this.showPlan(plan, force);
      }
      this.refreshPending(); this.schedulePoll();
    } catch (error) {if (view === generation) this.setData({status: message(error)});}
    finally {if (view === generation) this.setData({loading: false});}
  },
  async showPlan(plan: Job, force = false) {
    const context = studioContext(); const view = generation;
    if (context !== this.data.context) return;
    this.setData({plan, planStatus: statusName(plan.status), planFailure: ['failed', 'cancelled'].includes(plan.status) ? plan.failure?.message || plan.failure_code || '计划未完成，请检查素材或恢复原任务' : '',
      canRetryPlan: plan.status === 'failed' && plan.retryable && !plan.policy_blocked && plan.attempt_count < plan.max_attempts});
    writeStudioCache({planId: plan.id});
    if (plan.status !== 'succeeded') {this.setData({revision: null, shots: [], tracks: [], preciseName: '', canRender: false}); return;}
    if (!force && this.data.revision?.origin_job_id === plan.id && (this.data.draftLabel || this.data.saving || pendingStudioOperations().length)) return;
    const revision = await personalSession.request<EditingRevision>(`/jobs/${encodeURIComponent(plan.id)}/editing`, 'POST');
    guardStudio(context); if (view !== generation || this.data.plan?.id !== plan.id) return;
    const previous = this.data.revision;
    const draft = editorDraft(revision.document_id);
    const metadata = object(revision.template.metadata); const resolution = object(metadata.resolution);
    const slotNames = new Map((Array.isArray(revision.template.slots) ? revision.template.slots : []).map(raw => {const slot = object(raw); return [String(slot.name), String(slot.description || slot.name)];}));
    const values = object(draft.values);
    this.setData({revision, shots: revision.plan.selected.map(row => ({...row,
      sourceText: `${row.source_start.toFixed(3)}–${row.source_end.toFixed(3)} 秒`, targetText: `${row.timeline_start.toFixed(3)}–${row.timeline_end.toFixed(3)} 秒`,
      slotName: slotNames.get(row.slot) || row.slot, reason: Object.keys(row.rationale || {}).length ? '已有特征的加权得分 ' + row.score.toFixed(3) : '未提供质量评分，按素材与镜头顺序匹配'})), warnings: revision.plan.warnings || [],
      draftLabel: typeof draft.label === 'string' ? draft.label : '', draftRebased: Boolean(force && draft.body),
      editTarget: typeof values.target === 'string' ? values.target : revision.template.duration === null || revision.template.duration === undefined ? '' : String(revision.template.duration),
      editAspectIndex: typeof values.aspectIndex === 'number' ? values.aspectIndex : resolution.width === resolution.height ? 2 : Number(resolution.width) > Number(resolution.height) ? 1 : 0});
    if (previous?.revision_id !== revision.revision_id) this.setData({candidateSelection: '', candidates: [], history: [], showHistory: false, renderJob: null, renderStatus: '', renderReady: false,
      originals: [], showOriginals: false, originalMore: false, subtitleExportPath: '', subtitleExportName: '', subtitleExportContext: ''});
    this.syncPrecise(); this.syncSubtitles(); this.syncLayers();
    await this.loadRenderJob(); this.updateControls();
  },
  async refreshJobs() {
    const context = this.data.context; const view = generation;
    try {
      if (this.data.plan && active(this.data.plan)) {const id = this.data.plan.id; const plan = await personalSession.request<Job>('/jobs/' + encodeURIComponent(id)); guardStudio(context); if (view !== generation || this.data.plan?.id !== id) return; await this.showPlan(plan);}
      if (this.data.renderJob && active(this.data.renderJob)) {const job = await personalSession.request<Job>('/jobs/' + encodeURIComponent(this.data.renderJob.id)); guardStudio(context); if (view !== generation) return; this.showRenderJob(job);}
    } catch (error) {if (context === studioContext()) this.setData({status: '状态同步未完成：' + message(error)});}
  },
  async loadRenderJob() {
    const revision = this.data.revision; const context = studioContext(); const view = generation; if (!revision) return;
    const rows = await personalSession.request<Job[]>(`/jobs?project_id=${encodeURIComponent(personalSession.activeProject())}&order=desc&limit=100`);
    if (context !== studioContext() || view !== generation || this.data.revision?.revision_id !== revision.revision_id) return;
    const job = rows.find(row => row.type.startsWith('render.') && row.payload.timeline_revision_id === revision.revision_id);
    if (job) this.showRenderJob(job);
  },
  showRenderJob(job: Job) {
    if (job.payload.timeline_revision_id !== this.data.revision?.revision_id) return;
    this.setData({renderJob: job, renderStatus: `${job.type === 'render.draft' ? '预览' : '高清成片'}：${statusName(job.status)}`,
      renderFailure: ['failed', 'cancelled'].includes(job.status) ? job.failure?.message || job.failure_code || '任务未完成' : '',
      canRetryRender: job.status === 'failed' && job.retryable && !job.policy_blocked && job.attempt_count < job.max_attempts,
      renderReady: job.status === 'succeeded'});
  },
  targetInput(event: WechatMiniprogram.Input) {this.setData({target: event.detail.value}); writeStudioCache({target: event.detail.value});},
  aspectChange(event: WechatMiniprogram.PickerChange) {this.setData({aspectIndex: Number(event.detail.value)}); writeStudioCache({aspectIndex: Number(event.detail.value)});},
  templateChange(event: WechatMiniprogram.PickerChange) {const index = Number(event.detail.value); this.setData({templateIndex: index}); if (this.data.templates[index]) writeStudioCache({templateId: this.data.templates[index].id});},
  async planChange(event: WechatMiniprogram.PickerChange) {
    const index = Number(event.detail.value); const plan = this.data.plans[index]; if (!plan) return;
    this.setData({planIndex: index, revision: null, draftLabel: '', renderJob: null, renderReady: false});
    try {await this.showPlan(plan, true); this.schedulePoll();} catch (error) {this.setData({status: message(error)});}
  },
  async createPlan() {
    if (this.data.saving || this.data.draftLabel) return this.setData({status: '请先保存或放弃未保存修改'});
    const template = this.data.templates[this.data.templateIndex]; const ids = selectedAssets().slice().sort();
    if (!template || !ids.length) return this.setData({status: '请先选择模板和项目素材'});
    try {
      const body = {project_id: personalSession.activeProject(), template_id: template.id, asset_ids: ids,
        target_duration: duration(this.data.target), aspect_ratio: ratio(this.data.aspectIndex), quality: 'draft', max_attempts: 3};
      savePreparation('plan',body);
      wx.navigateTo({url:'/pages/billing/index?prepare=plan'});
    } catch (error) {this.setData({status: message(error)});}
  },
  async submit(record: StudioOperation, repeat = false) {
    if (this.data.saving) return;
    const view = generation;
    this.setData({saving: true, status: '正在提交…'}); this.refreshPending();
    try {
      let result: unknown;
      try {result = await sendStudioOperation<unknown>(record, repeat);}
      catch (error) {
        if (view === generation && record.context === studioContext() && record.kind === 'edit' && error instanceof ClientError && error.status >= 400 && error.status < 500 && this.data.revision) {
          const draft = editorDraft(this.data.revision.document_id);
          saveEditorDraft(this.data.revision.document_id, {label: `尚未保存：${record.label}。${message(error)}`, body: record.body, values: draft.values, precise: draft.precise, subtitle: draft.subtitle, layer: draft.layer});
          this.setData({draftLabel: `尚未保存：${record.label}。${message(error)}`, draftRebased: false, hasRejectedDraft: true});
        }
        if (view === generation && record.context === studioContext()) this.setData({status: error instanceof ClientError && error.status >= 400 && error.status < 500 ? message(error) : '尚未确认提交结果，请先核对原提交。'});
        throw error;
      }
      if (view !== generation || record.context !== studioContext()) return;
      try {await this.acceptResult(record, result);}
      catch (_) {if (view === generation && record.context === studioContext()) this.setData({status: '提交已确认，页面同步未完成，请刷新当前版本。'});}
    } finally {if (view === generation && record.context === studioContext()) {this.setData({saving: false}); this.refreshPending();}}
  },
  async acceptResult(record: StudioOperation, result: unknown) {
    guardStudio(record.context);
    if (record.kind === 'plan') {
      const plan = result as Job; writeStudioCache({planId: plan.id}); this.setData({plan, revision: null, draftLabel: ''}); await this.showPlan(plan, true);
    } else if (record.kind === 'edit') {
      if (this.data.revision?.document_id !== record.documentId || !this.data.plan) return;
      removeEditorDraft(record.documentId!); this.setData({draftLabel: '', draftRebased: false});
      if (Array.isArray(record.body.operations) && record.body.operations.some(row => object(row).op === 'save_subtitle' && object(row).cue_id === null)) this.setData({subtitleEditing: false});
      if (Array.isArray(record.body.operations) && record.body.operations.some(row => ['save_audio_clip','save_overlay_clip'].includes(String(object(row).op)) && object(row).clip_id === null)) this.setData({layerEditing: false});
      await this.showPlan(this.data.plan, true);
    } else {
      const job = (object(result).job || result) as Job;
      if (this.data.revision?.revision_id !== record.revisionId) return;
      this.showRenderJob(job);
    }
    this.setData({status: '提交已确认。'}); this.refreshPending(); this.schedulePoll();
  },
  async saveOperations(actions: EditingAction[], label: string, sourceIds: string[] = [], reapply = false) {
    const revision = this.data.revision; if (!revision || this.data.saving) return;
    if (this.data.draftLabel && !reapply) {
      const draft = editorDraft(revision.document_id); const precise = object(draft.precise);
      const configuration = draft.values && !draft.precise && !draft.subtitle && !draft.layer && !draft.body && actions.every(row => row.op === 'configure');
      const clip = draft.precise && !draft.body && actions.every(row => 'clip_id' in row && row.clip_id === precise.clipId);
      const subtitle = draft.subtitle && !draft.body && actions.every(row => row.op === 'save_subtitle' && row.cue_id === object(draft.subtitle).cueId);
      const layer = draft.layer && !draft.body && actions.every(row => ['save_audio_clip','save_overlay_clip'].includes(row.op) && 'clip_id' in row && row.clip_id === object(draft.layer).clipId);
      if (!configuration && !clip && !subtitle && !layer) return this.setData({status: '请先处理当前未保存修改'});
    }
    const body = {base_revision_id: revision.revision_id, operations: actions, source_asset_ids: [...new Set(sourceIds)].sort(), note: ''};
    try {await this.submit(operation('edit', 'edit:' + studioHash(body), body, label, {documentId: revision.document_id}));}
    catch (_) { /* submit retains the exact pending request or rejected draft. */ }
  },
  lockShot(event: WechatMiniprogram.TouchEvent) {const row = this.data.shots.find(item => item.selection_id === event.currentTarget.dataset.id); if (row) this.saveOperations([{op: 'set_lock', selection_id: row.selection_id, locked: !row.locked}], row.locked ? '解锁镜头' : '锁定镜头');},
  replanShot(event: WechatMiniprogram.TouchEvent) {this.saveOperations([{op: 'replan', selection_ids: [String(event.currentTarget.dataset.id)]}], '局部重算镜头');},
  replanAll() {this.saveOperations([{op: 'replan'}], '重算未锁定镜头');},
  undo() {this.saveOperations([{op: 'undo'}], '撤销上次修改');},
  addSources() {const ids = selectedAssets().filter(id => !this.data.revision?.source_asset_ids.includes(id)); if (ids.length) this.saveOperations([{op: 'refresh_candidates'}], '添加候选素材', ids); else this.setData({status: '请先在素材页选择新的已分析素材'});},
  async replaceShot(event: WechatMiniprogram.TouchEvent) {this.setData({candidateSelection: String(event.currentTarget.dataset.id), candidates: []}); await this.loadCandidates();},
  async loadCandidates(append = false) {
    const revision = this.data.revision; const selection = this.data.candidateSelection; if (!revision || !selection) return;
    try {
      const page = await personalSession.request<PageResult<ShotCandidate>>(`/editing/${encodeURIComponent(revision.document_id)}/revisions/${encodeURIComponent(revision.revision_id)}/candidates?selection_id=${encodeURIComponent(selection)}&skip=${append ? this.data.candidates.length : 0}&limit=50`);
      if (this.data.revision?.revision_id !== revision.revision_id || this.data.candidateSelection !== selection) return;
      const rows = page.items.map(item => ({...item, timeText: `${item.source_start.toFixed(3)}–${item.source_end.toFixed(3)} 秒`}));
      this.setData({candidates: append ? [...this.data.candidates, ...rows] : rows, candidateTotal: page.total});
    } catch (error) {this.setData({status: message(error)});}
  },
  moreCandidates() {this.loadCandidates(true);},
  closeCandidates() {this.setData({candidateSelection: '', candidates: []});},
  adoptCandidate(event: WechatMiniprogram.TouchEvent) {this.saveOperations([{op: 'replace', selection_id: this.data.candidateSelection, candidate_id: String(event.currentTarget.dataset.id)}], '替换镜头');},
  rememberConfig() {const revision = this.data.revision; if (!revision || this.data.preciseKind || this.data.hasRejectedDraft || this.data.hasSubtitleDraft || this.data.hasLayerDraft) return; this.setData({draftLabel: '画幅或时长设置尚未保存。', draftRebased: false}); try {saveEditorDraft(revision.document_id, {label: '画幅或时长设置尚未保存。', values: {target: this.data.editTarget, aspectIndex: this.data.editAspectIndex}});} catch (_) {this.setData({status: '草稿暂时只能保留在本页，请检查小程序存储后再离开。'});} this.updateControls();},
  editTargetInput(event: WechatMiniprogram.Input) {this.setData({editTarget: event.detail.value}); this.rememberConfig();},
  editAspectChange(event: WechatMiniprogram.PickerChange) {this.setData({editAspectIndex: Number(event.detail.value)}); this.rememberConfig();},
  saveConfig() {try {this.saveOperations([{op: 'configure', target_duration: duration(this.data.editTarget), aspect_ratio: ratio(this.data.editAspectIndex)}], '调整画幅与时长');} catch (error) {this.setData({status: message(error)});}},
  async refreshRevision() {if (this.data.plan) {const context = studioContext(); const view = generation; const id = this.data.plan.id; try {const plan = await personalSession.request<Job>('/jobs/' + encodeURIComponent(id)); guardStudio(context); if (view !== generation || this.data.plan?.id !== id) return; await this.showPlan(plan, true); this.schedulePoll();} catch (error) {if (view === generation) this.setData({status: message(error)});}}},
  discardDraft() {if (this.data.revision && !pendingStudioOperations().length) {removeEditorDraft(this.data.revision.document_id); this.setData({draftLabel: '', draftRebased: false}); this.refreshRevision();}},
  reapplyDraft() {const revision = this.data.revision; if (!revision || !this.data.draftRebased) return; const body = object(editorDraft(revision.document_id).body); if (Array.isArray(body.operations)) this.saveOperations(body.operations as EditingAction[], '重新应用未保存修改', Array.isArray(body.source_asset_ids) ? body.source_asset_ids as string[] : [], true);},
  async history() {this.setData({showHistory: !this.data.showHistory}); if (this.data.showHistory) await this.loadHistory();},
  async loadHistory(append = false) {
    const revision = this.data.revision; if (!revision) return;
    try {
      const rows = await personalSession.request<EditingRevision[]>(`/editing/${encodeURIComponent(revision.document_id)}/revisions?skip=${append ? this.data.history.length : 0}&limit=20`);
      if (this.data.revision?.revision_id !== revision.revision_id) return;
      this.setData({history: append ? [...this.data.history, ...rows] : rows, historyMore: rows.length === 20});
    } catch (error) {this.setData({status: message(error)});}
  },
  moreHistory() {this.loadHistory(true);},
  restoreVersion(event: WechatMiniprogram.TouchEvent) {this.saveOperations([{op: 'restore', revision_id: String(event.currentTarget.dataset.id)}], '恢复历史版本');},
  syncPrecise() {
    const revision = this.data.revision; if (!revision) return;
    const draft = editorDraft(revision.document_id); const raw = object(draft.precise);
    const id = typeof raw.clipId === 'string' ? raw.clipId : this.data.preciseId;
    const editor = revision.editor; const names: Record<string, string> = {video: '主视频', audio: '音轨', overlay: '叠加', subtitle: '字幕'};
    const labels = new Map(revision.plan.selected.map(row => [row.clip_id, row.name]));
    const tracks = (editor?.tracks || []).map((track, index) => ({id: track.id, type: track.type, muted: track.muted, index, muteText: ['video','audio'].includes(track.type) ? (track.muted ? '恢复声音' : track.type === 'video' ? '静音原声' : '静音') : (track.muted ? '显示' : '隐藏'), name: (names[track.type] || track.type) + (track.muted ? ' · 静音/隐藏' : ''),
      clips: track.clips.map((clip, index) => ({id: clip.id, label: `${index + 1}. ${labels.get(clip.id) || clip.text || clip.id}`, time: `${clip.range.start.toFixed(3)}–${clip.range.end.toFixed(3)} 秒`}))}));
    this.setData({tracks, hasRejectedDraft: Boolean(draft.body), preciseKind: String(raw.kind || ''),
      timing: editor ? `${editor.duration.toFixed(3)} 秒 · ${editor.fps} fps · ${editor.resolution.width}×${editor.resolution.height}` : ''});
    const track = editor?.tracks.find(track => track.clips.some(clip => clip.id === id));
    const index = track?.clips.findIndex(clip => clip.id === id) ?? -1; const clip = track?.clips[index];
    if (!track || !clip) {this.setData({preciseId: '', preciseName: ''}); return;}
    const row = revision.plan.selected.find(row => row.clip_id === id);
    this.setData({preciseId: id, preciseName: `${names[track.type]} · 第 ${index + 1} 段`, preciseType: track.type,
      preciseIndex: index, preciseCount: track.clips.length, preciseLocked: Boolean(row?.locked),
      preciseBounds: row ? `可用源区间 ${row.window_start.toFixed(3)}–${row.window_end.toFixed(3)} 秒` : '',
      sourceStart: typeof raw.sourceStart === 'string' ? raw.sourceStart : String(clip.source_start),
      sourceEnd: typeof raw.sourceEnd === 'string' ? raw.sourceEnd : String(clip.source_end ?? ''),
      splitOffset: typeof raw.splitOffset === 'string' ? raw.splitOffset : String((clip.range.end - clip.range.start) / 2),
      clipVolume: typeof raw.volume === 'string' ? raw.volume : String(clip.volume),
      fitIndex: Math.max(0, this.data.fitValues.indexOf(String(raw.fit || clip.composition?.fit || 'contain'))),
      transitionIndex: Math.max(0, this.data.transitionValues.indexOf(String(raw.transitionType || clip.transition?.type || 'none'))),
      transitionDuration: typeof raw.transitionDuration === 'string' ? raw.transitionDuration : String(clip.transition?.duration || 0)});
  },
  choosePrecise(event: WechatMiniprogram.TouchEvent) {
    const id = String(event.currentTarget.dataset.id); const revision = this.data.revision; if (!revision) return;
    const draft = editorDraft(revision.document_id);
    if (this.data.draftLabel && object(draft.precise).clipId !== id) return this.setData({status: '请先保存或放弃当前未保存修改'});
    const subtitleTrack = revision.editor?.tracks.find(track => track.type === 'subtitle' && track.clips.some(clip => clip.id === id));
    if (subtitleTrack) {this.setData({subtitleEditing: true, subtitleForm: subtitleForm(revision, subtitleTrack.clips.find(clip => clip.id === id), subtitleTrack.id)}); this.syncSubtitles(); return;}
    const layerTrack = revision.editor?.tracks.find(track => ['audio','overlay'].includes(track.type) && track.clips.some(clip => clip.id === id));
    if (layerTrack) return this.openLayer(layerTrack.type as LayerForm['kind'], id, layerTrack.id);
    this.setData({preciseId: id}); this.syncPrecise();
  },
  preciseInput(event: WechatMiniprogram.Input) {
    const field = String(event.currentTarget.dataset.field); const kind = field === 'fitIndex' ? 'fit' : ['transitionIndex','transitionDuration'].includes(field) ? 'transition' : field === 'clipVolume' ? 'volume' : field === 'splitOffset' ? 'split' : 'trim';
    const revision = this.data.revision;
    if (!revision || !this.data.preciseId || this.data.saving || this.data.pendingLabel || this.data.hasRejectedDraft || this.data.hasSubtitleDraft || this.data.hasLayerDraft ||
      (this.data.draftLabel && !this.data.preciseKind) || (this.data.preciseKind && this.data.preciseKind !== kind)) return;
    if (!['sourceStart', 'sourceEnd', 'splitOffset', 'clipVolume', 'fitIndex', 'transitionIndex', 'transitionDuration'].includes(field)) return;
    this.setData({[field]: field.endsWith('Index') ? Number(event.detail.value) : event.detail.value, preciseKind: kind, draftLabel: '片段参数尚未保存，请保存当前修改或放弃草稿。', draftRebased: false});
    try {saveEditorDraft(revision.document_id, {label: this.data.draftLabel, precise: {clipId: this.data.preciseId, kind,
      sourceStart: this.data.sourceStart, sourceEnd: this.data.sourceEnd, splitOffset: this.data.splitOffset, volume: this.data.clipVolume,
      fit: this.data.fitValues[this.data.fitIndex], transitionType: this.data.transitionValues[this.data.transitionIndex], transitionDuration: this.data.transitionDuration}});}
    catch (_) {this.setData({status: '草稿只能暂留本页，请检查小程序存储后再离开'});}
    this.updateControls();
  },
  async preciseAction(event: WechatMiniprogram.TouchEvent) {
    const id = this.data.preciseId; if (!id || this.data.preciseLocked) return;
    const kind = String(event.currentTarget.dataset.kind);
    const number = (raw: string, name: string): number => {const value = Number(raw); if (!raw.trim() || !Number.isFinite(value)) throw new ClientError('clip_input_invalid', '请填写有效的' + name); return value;};
    try {
      let action: EditingAction; let label: string;
      if (kind === 'trim') {action = {op: 'trim_clip', clip_id: id, source_start: number(this.data.sourceStart, '源起点'), source_end: number(this.data.sourceEnd, '源终点')}; label = '裁切并顺接';}
      else if (kind === 'split') {action = {op: 'split_clip', clip_id: id, offset_seconds: number(this.data.splitOffset, '分割偏移')}; label = '分割片段';}
      else if (kind === 'volume') {action = {op: 'set_clip_audio', clip_id: id, volume: number(this.data.clipVolume, '音量倍数')}; label = '调整原声音量';}
      else if (kind === 'fit') {action = {op: 'set_video_fit', clip_id: id, mode: this.data.fitValues[this.data.fitIndex] as 'contain' | 'cover' | 'stretch'}; label = '调整画面适配';}
      else if (kind === 'transition') {const type = this.data.transitionValues[this.data.transitionIndex] as 'none' | 'fade' | 'dissolve' | 'wipeleft' | 'wiperight' | 'slideleft' | 'slideright'; action = {op: 'set_transition', clip_id: id, type, duration: type === 'none' ? 0 : number(this.data.transitionDuration, '转场时长')}; label = '调整入场转场';}
      else if (kind === 'delete') {action = {op: 'remove_clip', clip_id: id}; label = '删除片段并顺接';}
      else {action = {op: 'move_clip', clip_id: id, to_index: this.data.preciseIndex + (kind === 'up' ? -1 : 1)}; label = '调整片段顺序';}
      await this.saveOperations([action], label);
    } catch (error) {this.setData({status: message(error)});}
  },
  syncLayers() {
    const revision = this.data.revision; if (!revision) return;
    const draft = editorDraft(revision.document_id); let form = this.data.layerForm;
    if (draft.layer) form = layerForm(revision, form.kind, undefined, form.trackId, draft.layer);
    else if (this.data.layerEditing) {
      const track = revision.editor?.tracks.find(track => track.clips.some(clip => clip.id === form.clipId));
      form = layerForm(revision, form.kind, track?.clips.find(clip => clip.id === form.clipId), track?.id || form.trackId);
    }
    const sources = (revision.editor?.sources || []).filter(source => form.kind === 'audio' ? source.has_audio : /^(video|image)/.test(source.kind));
    this.setData({layerForm: form, layerEditing: Boolean(draft.layer || this.data.layerEditing), hasLayerDraft: Boolean(draft.layer),
      layerMissing: Boolean(form.clipId && !revision.editor?.tracks.some(track => track.clips.some(clip => clip.id === form.clipId))),
      layerSourceIds: sources.map(source => source.id), layerSourceNames: sources.map(source => source.name), layerSourceIndex: Math.max(0, sources.findIndex(source => source.id === form.assetId)),
      layerFitIndex: Math.max(0, this.data.fitValues.indexOf(form.fit)), layerRoleIndex: ['music','voice','other'].indexOf(form.role)});
  },
  openLayer(kind: LayerForm['kind'], clipId?: string, trackId?: string) {
    const revision = this.data.revision; if (!revision || this.data.saving || this.data.pendingLabel) return;
    const draft = editorDraft(revision.document_id);
    if (this.data.draftLabel && (!draft.layer || object(draft.layer).clipId !== clipId)) return this.setData({status: '请先处理当前未保存修改'});
    const track = revision.editor?.tracks.find(track => track.clips.some(clip => clip.id === clipId));
    this.setData({layerEditing: true, layerForm: layerForm(revision, kind, track?.clips.find(clip => clip.id === clipId), trackId || track?.id)}); this.syncLayers();
  },
  newLayer(event: WechatMiniprogram.TouchEvent) {this.openLayer(event.currentTarget.dataset.kind === 'overlay' ? 'overlay' : 'audio');},
  rememberLayer() {
    const revision = this.data.revision; if (!revision || this.data.saving || this.data.pendingLabel || this.data.hasRejectedDraft || (this.data.draftLabel && !this.data.hasLayerDraft)) return;
    this.setData({hasLayerDraft: true, draftLabel: '音轨或叠加参数尚未保存。', draftRebased: false});
    try {saveEditorDraft(revision.document_id, {label: this.data.draftLabel, layer: this.data.layerForm});}
    catch (_) {this.setData({status: '草稿暂时只能保留本页，请检查存储后再离开。'});} this.updateControls();
  },
  layerInput(event: WechatMiniprogram.Input) {
    const field = String(event.currentTarget.dataset.field);
    if (this.data.hasRejectedDraft || (this.data.draftLabel && !this.data.hasLayerDraft) || !['trackId','start','end','sourceStart','volume','fadeIn','fadeOut','x','y','width','height','opacity'].includes(field)) return;
    this.setData({layerForm: {...this.data.layerForm, [field]: event.detail.value}}); this.rememberLayer();
  },
  layerChoice(event: WechatMiniprogram.PickerChange) {
    if (this.data.hasRejectedDraft || (this.data.draftLabel && !this.data.hasLayerDraft)) return;
    const field = String(event.currentTarget.dataset.field); const index = Number(event.detail.value);
    const value = field === 'assetId' ? this.data.layerSourceIds[index] : field === 'fit' ? this.data.fitValues[index] : ['music','voice','other'][index];
    if (!['assetId','fit','role'].includes(field) || !value) return;
    this.setData({layerForm: {...this.data.layerForm, [field]: value}}); this.rememberLayer(); this.syncLayers();
  },
  async saveLayer() {try {await this.saveOperations([layerCommand(this.data.layerForm)], '保存音轨或叠加片段');} catch (error) {this.setData({status: message(error)});}},
  closeLayer() {if (!this.data.draftLabel) this.setData({layerEditing: false});},
  copyLayerAsNew() {
    const revision = this.data.revision; if (!revision || !this.data.hasLayerDraft || !this.data.layerMissing || this.data.saving || this.data.pendingLabel) return;
    const form = {...this.data.layerForm, clipId: null};
    try {saveEditorDraft(revision.document_id, {label: '已转为新增片段草稿，请审阅后保存。', layer: form});}
    catch (_) {return this.setData({status: '保留草稿失败，请检查存储后重试。'});}
    this.setData({layerForm: form, hasRejectedDraft: false, layerMissing: false, draftLabel: '已转为新增片段草稿，请审阅后保存。', draftRebased: false}); this.syncLayers(); this.updateControls();
  },
  layerTrackAction(event: WechatMiniprogram.TouchEvent) {
    if (this.data.draftLabel) return; const id = String(event.currentTarget.dataset.id); const kind = String(event.currentTarget.dataset.kind);
    const track = this.data.revision?.editor?.tracks.find(track => track.id === id); if (!track) return;
    const index = this.data.tracks.findIndex(track => track.id === id);
    const action: EditingAction = kind === 'mute' ? {op: 'set_track_mute', track_id: id, muted: !track.muted} : kind === 'delete' ? {op: 'delete_layer_track', track_id: id} : {op: 'move_track', track_id: id, to_index: index + (kind === 'up' ? -1 : 1)};
    this.saveOperations([action], '调整附加轨道');
  },
  deleteLayer(event: WechatMiniprogram.TouchEvent) {if (!this.data.draftLabel) this.saveOperations([{op: 'delete_layer_clip', clip_id: String(event.currentTarget.dataset.id)}], '删除附加片段');},
  syncSubtitles() {
    const revision = this.data.revision; if (!revision) return;
    const draft = editorDraft(revision.document_id); const raw = object(draft.subtitle);
    const all = (revision.editor?.tracks || []).filter(track => track.type === 'subtitle').flatMap(track => track.clips.map(clip => ({
      id: clip.id, trackId: track.id, text: clip.text || '', time: `${clip.range.start.toFixed(3)}–${clip.range.end.toFixed(3)} 秒`,
      original: clip.subtitle?.origin?.text || '', anchor: clip.subtitle?.anchor ? '随来源片段' : '固定成片时间', partial: Boolean(clip.subtitle?.partial)})));
    const videos = (revision.editor?.tracks || []).filter(track => track.type === 'video').flatMap(track => track.clips);
    let form = this.data.subtitleForm;
    if (draft.subtitle) form = subtitleForm(revision, undefined, String(raw.trackId || 'subtitles'), raw);
    else if (this.data.subtitleEditing) {
      const row = all.find(row => row.id === form.cueId); const clip = revision.editor?.tracks.find(track => track.id === row?.trackId)?.clips.find(clip => clip.id === form.cueId);
      form = subtitleForm(revision, clip, row?.trackId || form.trackId);
    }
    this.setData({subtitleCues: all.slice(0, this.data.subtitleLimit), subtitleMore: all.length > this.data.subtitleLimit,
      hasSubtitleDraft: Boolean(draft.subtitle), subtitleEditing: Boolean(draft.subtitle || this.data.subtitleEditing), subtitleForm: form,
      subtitleMissing: Boolean(form.cueId && !all.some(row => row.id === form.cueId)), subtitleModeIndex: ['keep', 'absolute', 'clip'].indexOf(form.anchorMode),
      subtitleAnchorIds: videos.map(clip => clip.id), subtitleAnchorNames: videos.map((clip, index) => `第${index + 1}段 · ${clip.range.start.toFixed(3)}–${clip.range.end.toFixed(3)}秒`),
      subtitleAnchorIndex: Math.max(0, videos.findIndex(clip => clip.id === form.anchorClipId))});
    this.previewSubtitle();
  },
  moreSubtitleCues() {this.setData({subtitleLimit: this.data.subtitleLimit + 50}); this.syncSubtitles();},
  newSubtitle() {
    const revision = this.data.revision; if (!revision || this.data.draftLabel || this.data.pendingLabel) return;
    this.setData({subtitleEditing: true, subtitleForm: subtitleForm(revision)}); this.syncSubtitles();
  },
  editSubtitle(event: WechatMiniprogram.TouchEvent) {
    const revision = this.data.revision; const id = String(event.currentTarget.dataset.id); if (!revision) return;
    if (this.data.draftLabel && object(editorDraft(revision.document_id).subtitle).cueId !== id) return this.setData({status: '请先处理当前未保存修改'});
    const track = revision.editor?.tracks.find(track => track.type === 'subtitle' && track.clips.some(clip => clip.id === id));
    if (!track) return;
    this.setData({subtitleEditing: true, subtitleForm: subtitleForm(revision, track.clips.find(clip => clip.id === id), track.id)}); this.syncSubtitles();
  },
  rememberSubtitle() {
    const revision = this.data.revision; if (!revision || this.data.saving || this.data.pendingLabel || this.data.hasRejectedDraft || (this.data.draftLabel && !this.data.hasSubtitleDraft)) return;
    this.setData({draftLabel: '字幕修改尚未保存。', draftRebased: false, hasSubtitleDraft: true});
    try {saveEditorDraft(revision.document_id, {label: this.data.draftLabel, subtitle: this.data.subtitleForm});}
    catch (_) {this.setData({status: '字幕草稿只能暂留本页，请检查小程序存储后再离开'});}
    this.updateControls();
  },
  subtitleInput(event: WechatMiniprogram.Input) {
    const field = String(event.currentTarget.dataset.field);
    if (!['text', 'start', 'end', 'fontSize', 'color', 'x', 'y'].includes(field) || this.data.hasRejectedDraft || (this.data.draftLabel && !this.data.hasSubtitleDraft)) return;
    this.setData({subtitleForm: {...this.data.subtitleForm, [field]: event.detail.value}}); this.rememberSubtitle(); this.previewSubtitle();
  },
  previewSubtitle() {const form = this.data.subtitleForm; const font = Number(form.fontSize); this.setData({subtitlePreviewSize: Math.max(12, Math.min(32, Number.isFinite(font) ? font / 2 : 24)), subtitlePreviewColor: /^(white|black|red|#[A-Fa-f0-9]{6})$/.test(form.color) ? form.color : 'white'});},
  subtitleModeChange(event: WechatMiniprogram.PickerChange) {
    const index = Number(event.detail.value); const mode = (['keep', 'absolute', 'clip'] as const)[index]; if (!mode) return;
    this.setData({subtitleModeIndex: index, subtitleForm: {...this.data.subtitleForm, anchorMode: mode}}); this.rememberSubtitle();
  },
  subtitleAnchorChange(event: WechatMiniprogram.PickerChange) {
    const index = Number(event.detail.value); this.setData({subtitleAnchorIndex: index, subtitleForm: {...this.data.subtitleForm, anchorClipId: this.data.subtitleAnchorIds[index] || ''}}); this.rememberSubtitle();
  },
  closeSubtitle() {if (!this.data.draftLabel) this.setData({subtitleEditing: false});},
  async saveSubtitle() {try {await this.saveOperations([subtitleCommand(this.data.subtitleForm)], '保存字幕修订');} catch (error) {this.setData({status: message(error)});}},
  deleteSubtitle(event: WechatMiniprogram.TouchEvent) {if (!this.data.draftLabel) this.saveOperations([{op: 'delete_subtitle', cue_id: String(event.currentTarget.dataset.id)}], '删除字幕');},
  importSubtitles() {this.saveOperations([{op: 'import_transcript'}], '导入已有识别字幕');},
  copySubtitleAsNew() {
    const revision = this.data.revision; if (!revision || this.data.saving || this.data.pendingLabel || !this.data.hasSubtitleDraft) return;
    const form: SubtitleForm = {...this.data.subtitleForm, cueId: null, anchorMode: 'absolute'};
    try {saveEditorDraft(revision.document_id, {label: '已转为新增字幕草稿，请审阅时间后保存。', subtitle: form});}
    catch (_) {this.setData({status: '无法保存新草稿，请检查小程序存储，原草稿仍保留'}); return;}
    this.setData({subtitleForm: form, hasRejectedDraft: false, subtitleMissing: false, draftLabel: '已转为新增字幕草稿，请审阅时间后保存。', draftRebased: false}); this.syncSubtitles(); this.updateControls();
  },
  async toggleOriginals() {this.setData({showOriginals: !this.data.showOriginals}); if (this.data.showOriginals) await this.loadOriginals();},
  async loadOriginals(append = false) {
    const revision = this.data.revision; if (!revision) return; const context = studioContext(); const view = generation;
    try {
      const page = await personalSession.request<PageResult<OriginalTranscript>>(`/editing/${encodeURIComponent(revision.document_id)}/revisions/${encodeURIComponent(revision.revision_id)}/transcripts?skip=${append ? this.data.originals.length : 0}&limit=50`);
      if (context !== studioContext() || view !== generation || this.data.revision?.revision_id !== revision.revision_id) return;
      const rows = page.items.map(row => ({...row, key: `${row.asset_id}:${row.transcript_hash}:${row.id}`, time: `${row.start.toFixed(3)}–${row.end.toFixed(3)}秒`}));
      this.setData({originals: append ? [...this.data.originals, ...rows] : rows, originalMore: page.skip + rows.length < page.total});
    } catch (error) {if (view === generation) this.setData({status: message(error)});}
  },
  moreOriginals() {this.loadOriginals(true);},
  async exportSubtitles(event: WechatMiniprogram.TouchEvent) {
    const revision = this.data.revision; if (!revision || this.data.saving || this.data.draftLabel || this.data.pendingLabel) return;
    const format = event.currentTarget.dataset.format === 'vtt' ? 'vtt' : 'srt'; const context = studioContext(); const view = generation;
    try {
      const file = await personalSession.request<SubtitleExport>(`/editing/${encodeURIComponent(revision.document_id)}/revisions/${encodeURIComponent(revision.revision_id)}/subtitles?format=${format}`);
      if (view !== generation || context !== studioContext() || this.data.revision?.revision_id !== revision.revision_id) return;
      if (file.revision_id !== revision.revision_id || file.timeline_hash !== revision.timeline_hash || sha256(file.content) !== file.sha256 || !/^subtitles-[a-zA-Z0-9-]{1,60}\.(srt|vtt)$/.test(file.filename)) throw new ClientError('subtitle_export_invalid', '字幕文件与当前版本或内容校验不符');
      const path = wx.env.USER_DATA_PATH + '/' + file.filename;
      await new Promise<void>((resolve, reject) => wx.getFileSystemManager().writeFile({filePath: path, data: file.content, encoding: 'utf8', success: () => resolve(), fail: () => reject(new ClientError('subtitle_file_failed', '字幕文件写入失败，请检查可用空间'))}));
      if (view !== generation || context !== studioContext()) return;
      this.setData({subtitleExportPath: path, subtitleExportName: file.filename, subtitleExportContext: context, status: `已保存当前v${revision.number}字幕文件（文字和时间）；可点击下方按钮选择文件接收位置。`});
    } catch (error) {if (view === generation) this.setData({status: message(error)});}
  },
  shareSubtitleFile() {
    if (!this.data.subtitleExportPath || this.data.subtitleExportContext !== studioContext()) return;
    wx.shareFileMessage({filePath: this.data.subtitleExportPath, fileName: this.data.subtitleExportName,
      success: () => this.setData({status: '字幕文件分享流程已完成。'}), fail: () => this.setData({status: '文件未分享，可再次选择接收位置；原文件仍保留。'})});
  },
  async render(event: WechatMiniprogram.TouchEvent) {
    const revision=this.data.revision,plan=this.data.plan;if(!revision||!plan||this.data.saving||pendingStudioOperations().length)return;
    const quality=event.currentTarget.dataset.quality==='draft'?'draft':'high',fresh=Boolean(event.currentTarget.dataset.fresh),context=studioContext(),view=generation,allowed=this.data.canRender;
    if(fresh&&(!allowed||['queued','running'].includes(this.data.renderJob?.status||'')))return;
    this.setData({saving:true});
    try{
      if(!fresh){const jobs=await personalSession.request<Job[]>(`/jobs?project_id=${encodeURIComponent(personalSession.activeProject())}&job_type=render.${quality}&revision_id=${encodeURIComponent(revision.revision_id)}&order=desc&limit=1`);guardStudio(context);if(view!==generation)return;
        if(jobs.length){const job=jobs[0];if(job.status==='succeeded'){const artifacts=await personalSession.request<Artifact[]>('/artifacts?job_id='+encodeURIComponent(job.id));guardStudio(context);if(view!==generation)return;const artifact=artifacts.find(row=>row.kind==='render');if(artifact){wx.navigateTo({url:'/pages/work-detail/index?id='+encodeURIComponent(artifact.id)});return;}}
          wx.navigateTo({url:'/pages/billing/index?job='+encodeURIComponent(job.id)});return;}}
      if(!allowed)return this.setData({status:this.data.renderSupport||'请先保存有效编辑后再获取报价'});
      wx.navigateTo({url:`/pages/billing/index?document=${encodeURIComponent(revision.document_id)}&revision=${encodeURIComponent(revision.revision_id)}&origin=${encodeURIComponent(plan.id)}&quality=${quality}`});
    }catch(error){if(context===studioContext()&&view===generation)this.setData({status:message(error)});}finally{if(context===studioContext()&&view===generation){this.setData({saving:false});this.updateControls();}}
  },
  async recover(event: WechatMiniprogram.TouchEvent) {
    const record = pendingStudioOperations()[0]; if (!record || this.data.saving) return;
    try {
      if (event.currentTarget.dataset.repeat) {await this.submit(record, true); return;}
      this.setData({saving: true}); const receipt = await checkStudioOperation(record);
      if (!receipt.found) return this.setData({status: '暂未查到结果，可继续原提交，保留原请求和操作标识。'});
      confirmStudioOperation(record); await this.acceptResult(record, receipt.result);
    } catch (error) {this.setData({status: message(error)});}
    finally {this.setData({saving: false}); this.refreshPending();}
  },
  async retry(event: WechatMiniprogram.TouchEvent) {
    const job=event.currentTarget.dataset.kind==='plan'?this.data.plan:this.data.renderJob;
    if(job&&!this.data.saving)wx.navigateTo({url:'/pages/billing/index?job='+encodeURIComponent(job.id)});
  },
  chooseAssets() {wx.switchTab({url: '/pages/assets/index'});},
  chooseProject() {wx.switchTab({url: '/pages/create/index'});},
  openWorks() {wx.switchTab({url: '/pages/works/index'});},
  login() {wx.navigateTo({url: '/pages/login/index'});},
});
