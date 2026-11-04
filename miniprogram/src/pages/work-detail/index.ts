import {message, personalSession} from '../../shared/api';
import {ArtifactAccess, ClientError} from '../../shared/contracts';
import {blankReviewIssue, checkReview, readReviewState, ReviewDecision, ReviewForm, ReviewPending, ReviewRecord,
  ReviewReport, reviewBody, reviewContext, reviewOperation, reviewStatus, saveReviewState, sendReview} from '../../shared/review-state';
import * as Quality from '../../shared/review-state';
import * as ScenarioForms from '../../vendor/scenario-review-form';
import {writeStudioCache} from '../../shared/studio-state';

function download(url: string): Promise<string> {
  if (!url.startsWith('https://')) throw new ClientError('invalid_playback_url', '暂时无法获取视频');
  return new Promise((resolve, reject) => wx.downloadFile({url,
    success: result => result.statusCode === 200 ? resolve(result.tempFilePath)
      : reject(new ClientError('video_download_failed', '下载未完成，请刷新授权后重试')),
    fail: () => reject(new ClientError('video_download_failed', '下载未完成，请检查网络与可用空间')),
  }));
}

Page({
  data: {artifactId: '', context: '', url: '', expiresAt: '', status: '', loading: false, saving: false,
    qualityData:null as ScenarioForms.Context|null,qualityProfiles:[] as ScenarioForms.Profile[],qualityForm:null as ScenarioForms.Form|null,qualityPending:null as Quality.QualityPending|null,qualityLoading:false,qualitySaving:false,qualityStaleReport:false,qualityNotice:'',qualityFields:[] as Array<ScenarioForms.DimensionForm&{label:string;options:string[];choices:Array<{id:ScenarioForms.Status;label:string}>;index:number}>,qualityLatest:null as (ScenarioForms.Record&{label:string;dimensionLabels:Array<{label:string;status:string;score:number|null;note:string}>})|null,
    report: null as ReviewReport | null, reviewLabel: '', reviewLoading: false, reviewSaving: false, reviewConflict: false,
    form: {note: '', issues: []} as ReviewForm, pending: null as ReviewPending | null,
    categories: ['画面','声音','字幕','内容','其他'], categoryValues: ['picture','audio','subtitle','content','other'],
    severities: ['错误：需修改','提醒：请注意','说明'], severityValues: ['error','warning','info'],
    issueIndexes: [] as Array<{category: number; severity: number}>, videoTime: 0,
    history: [] as Array<ReviewRecord & {label: string}>, showHistory: false, historyOffset: 0, historyMore: false},
  authorizationGeneration: 0,
  reviewGeneration: 0,
  qualityGeneration:0,
  matches(context: string) {return context === this.data.context && context === reviewContext(this.data.artifactId);},
  async onLoad(options: Record<string, string | undefined>) {
    const id = options.id || '';
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(id)) return this.setData({status: '作品地址无效'});
    if (!personalSession.profile) await personalSession.restore();
    this.setData({artifactId: id, context: reviewContext(id)}); this.restoreReviewDraft(); await this.loadReview(); await this.authorize();
  },
  async onShow() {if (this.data.artifactId && !this.data.url) {this.restoreReviewDraft(); await this.loadReview(); await this.authorize();}},
  onHide() {
    this.authorizationGeneration += 1;
    this.reviewGeneration += 1;this.qualityGeneration += 1;
    this.setData({url: '', loading: false, saving: false, reviewLoading: false, reviewSaving: false,qualityLoading:false,qualitySaving:false});
  },
  onUnload() {this.onHide();},
  async authorize() {
    if (!this.data.artifactId || this.data.loading || !this.data.report?.can_preview || !this.matches(this.data.context)) return;
    const generation = ++this.authorizationGeneration;
    this.setData({loading: true, status: ''});
    try {
      const access = await personalSession.request<ArtifactAccess>('/artifacts/' + encodeURIComponent(this.data.artifactId) + '/access', 'POST', {purpose: 'preview'});
      if (generation !== this.authorizationGeneration || !this.matches(this.data.context)) return;
      if (!access.url.startsWith('https://')) throw new ClientError('invalid_playback_url', '暂时无法获取视频');
      this.setData({url: access.url, expiresAt: access.expires_at});
    } catch (error) {if (generation === this.authorizationGeneration) this.setData({url: '', status: message(error)});}
    finally {if (generation === this.authorizationGeneration) this.setData({loading: false});}
  },
  playbackError() {this.setData({status: '播放未完成，链接可能已过期，请刷新播放授权。'});},
  async saveVideo() {
    if (this.data.saving || !this.data.artifactId || !this.data.report?.can_download || this.data.pending || this.data.reviewSaving || this.qualityBlocked() || !this.matches(this.data.context)) return;
    const context = this.data.context; const generation = this.authorizationGeneration;
    this.setData({saving: true, status: '正在下载视频…'});
    try {
      const access = await personalSession.request<ArtifactAccess>('/artifacts/' + encodeURIComponent(this.data.artifactId) + '/access', 'POST', {purpose: 'download'});
      if (!this.matches(context) || generation !== this.authorizationGeneration) return;
      const path = await download(access.url);
      if (!this.matches(context) || generation !== this.authorizationGeneration) return;
      await new Promise<void>((resolve, reject) => wx.saveVideoToPhotosAlbum({filePath: path,
        success: () => resolve(), fail: () => reject(new ClientError('album_save_failed', '保存未完成，请检查相册权限和可用空间'))}));
      if (this.matches(context) && generation === this.authorizationGeneration) this.setData({status: '视频已保存到相册。'});
    } catch (error) {if (this.matches(context) && generation === this.authorizationGeneration) this.setData({status: message(error)});}
    finally {if (this.matches(context) && generation === this.authorizationGeneration) this.setData({saving: false});}
  },
  restoreReviewDraft() {if (!this.matches(this.data.context)) return; const saved = readReviewState(this.data.artifactId); this.setData({form: saved.form, pending: saved.pending}); this.syncIssueIndexes();},
  syncIssueIndexes() {this.setData({issueIndexes: this.data.form.issues.map(row => ({category: Math.max(0,this.data.categoryValues.indexOf(row.category)), severity: Math.max(0,this.data.severityValues.indexOf(row.severity))}))});},
  applyReport(report: ReviewReport) {this.setData({report, reviewLabel: reviewStatus(report.status)});},
  async loadReview() {
    if(this.data.reviewSaving||this.data.qualitySaving)return;
    const context = this.data.context; if (!this.matches(context)) return this.setData({url: '', report: null,qualityData:null,qualityForm:null,qualityPending:null,qualityFields:[],qualityLatest:null, status: '账号已变化，请从作品列表重新打开'});
    const generation = ++this.reviewGeneration; this.setData({reviewLoading: true});
    try {const report = await personalSession.request<ReviewReport>(`/artifacts/${encodeURIComponent(this.data.artifactId)}/review`);
      if (this.matches(context) && generation === this.reviewGeneration) {this.applyReport(report); this.setData({history: [], showHistory: false, historyOffset: 0, reviewConflict: false,qualityStaleReport:false});await this.loadQuality();}
    } catch (error) {if (this.matches(context) && generation === this.reviewGeneration) this.setData({status: message(error)});}
    finally {if (this.matches(context) && generation === this.reviewGeneration) this.setData({reviewLoading: false});}
  },
  async refreshReview() {await this.loadReview(); await this.authorize();},
  videoTime(event: WechatMiniprogram.VideoTimeUpdate) {this.setData({videoTime: event.detail.currentTime});},
  seekIssue(event: WechatMiniprogram.TouchEvent) {const start = Number(event.currentTarget.dataset.start); if (this.data.url && Number.isFinite(start) && start >= 0 && start <= (this.data.report?.duration || 0)) wx.createVideoContext('workPreview', this).seek(start);},
  rememberReview(form: ReviewForm) {
    if (!this.matches(this.data.context) || this.data.pending || this.data.reviewSaving || this.qualityBlocked()) return;
    this.setData({form}); this.syncIssueIndexes();
    try {saveReviewState(this.data.artifactId, form);} catch (_) {this.setData({status: '审核草稿暂时只能保留本页，请检查小程序存储后重试'});}
  },
  reviewNote(event: WechatMiniprogram.Input) {this.rememberReview({...this.data.form, note: event.detail.value});},
  addIssue() {if (this.data.form.issues.length < 50) this.rememberReview({...this.data.form, issues: [...this.data.form.issues, {...blankReviewIssue(), start: this.data.url ? this.data.videoTime.toFixed(3) : ''}]});},
  removeIssue(event: WechatMiniprogram.TouchEvent) {this.rememberReview({...this.data.form, issues: this.data.form.issues.filter((_, index) => index !== Number(event.currentTarget.dataset.index))});},
  issueInput(event: WechatMiniprogram.Input) {const index = Number(event.currentTarget.dataset.index); const field = String(event.currentTarget.dataset.field); if (!this.data.form.issues[index] || !['message','start','end'].includes(field)) return; this.rememberReview({...this.data.form, issues: this.data.form.issues.map((row,i) => i === index ? {...row, [field]: event.detail.value} : row)});},
  issueChoice(event: WechatMiniprogram.PickerChange) {
    const index = Number(event.currentTarget.dataset.index); const field = String(event.currentTarget.dataset.field); const choice = Number(event.detail.value);
    const value = (field === 'category' ? this.data.categoryValues : this.data.severityValues)[choice];
    if (!this.data.form.issues[index] || !['category','severity'].includes(field) || !value) return;
    this.rememberReview({...this.data.form, issues: this.data.form.issues.map((row,i) => i === index ? {...row, [field]: value} as typeof row : row)});
  },
  async submitReview(event: WechatMiniprogram.TouchEvent) {
    const report = this.data.report; if (!report?.can_review || this.data.pending || this.data.reviewSaving || this.data.reviewLoading || this.data.reviewConflict || this.qualityBlocked()) return;
    try {const decision: ReviewDecision = event.currentTarget.dataset.decision === 'approved' ? 'approved' : 'changes_requested'; await this.sendReviewOperation(reviewOperation(this.data.artifactId, reviewBody(report, this.data.form, decision)));}
    catch (error) {this.setData({status: message(error)});}
  },
  async sendReviewOperation(row: ReviewPending) {
    if(this.qualityBlocked())return;
    const context = this.data.context; const generation = this.reviewGeneration; this.setData({reviewSaving: true, status: ''});
    try {const result = await sendReview(row, this.data.form); if (!this.matches(context) || generation !== this.reviewGeneration) return;
      this.restoreReviewDraft(); this.applyReport(result.current); this.setData({status: '审核决定已保存。', history: [], showHistory: false, historyOffset: 0});
    } catch (error) {if (this.matches(context) && generation === this.reviewGeneration) {this.setData({pending: readReviewState(this.data.artifactId).pending, reviewConflict: error instanceof ClientError && [409,403].includes(error.status), status: message(error)});}}
    finally {if (this.matches(context) && generation === this.reviewGeneration) this.setData({reviewSaving: false});}
  },
  async recoverReview(event: WechatMiniprogram.TouchEvent) {
    const row = this.data.pending; if (!row || this.data.reviewSaving || this.data.qualitySaving || this.data.qualityLoading) return;
    if (event.currentTarget.dataset.repeat) return this.sendReviewOperation(row);
    const context = this.data.context; const generation = this.reviewGeneration; this.setData({reviewSaving: true});
    try {const result = await checkReview(row); if (!this.matches(context) || generation !== this.reviewGeneration) return;
      this.applyReport(result.current); this.restoreReviewDraft(); this.setData({status: result.found ? '已核对原审核提交。' : '尚未找到原回执，可沿用原提交继续。'});
    } catch (error) {if (this.matches(context) && generation === this.reviewGeneration) this.setData({status: message(error)});}
    finally {if (this.matches(context) && generation === this.reviewGeneration) this.setData({reviewSaving: false});}
  },
  discardReviewDraft() {if (!this.data.pending && !this.data.reviewSaving && !this.qualityBlocked()) {try {saveReviewState(this.data.artifactId, {note: '', issues: []}); this.restoreReviewDraft();} catch (_) {this.setData({status: '草稿存储暂不可用'});}}},
  async history() {
    const context = this.data.context; const generation = this.reviewGeneration; const offset = this.data.showHistory ? this.data.historyOffset : 0;
    try {const rows = await personalSession.request<ReviewRecord[]>(`/artifacts/${encodeURIComponent(this.data.artifactId)}/reviews?skip=${offset}&limit=20`);
      if (!this.matches(context) || generation !== this.reviewGeneration) return;
      const all = [...(offset ? this.data.history : []), ...rows.map(row => ({...row, label: reviewStatus(row.decision)}))]; this.setData({history: all.filter((row,index) => all.findIndex(item => item.id === row.id) === index), showHistory: true, historyOffset: offset + rows.length, historyMore: rows.length === 20});
    } catch (error) {if (this.matches(context)) this.setData({status: message(error)});}
  },
  qualityBlocked(){return Boolean(this.data.qualityData?.scenario||this.data.qualityPending)&&this.data.report?.project_id!==personalSession.activeProject()||this.data.qualitySaving||this.data.qualityLoading||Boolean(this.data.qualityPending)||this.data.qualityStaleReport;},
  qualityMatches(context:string,projectId:string,artifactId:string,generation:number){return this.matches(context)&&this.data.artifactId===artifactId&&personalSession.activeProject()===projectId&&this.data.report?.project_id===projectId&&generation===this.qualityGeneration;},
  async loadQuality(){
    const context=this.data.context,artifactId=this.data.artifactId,projectId=this.data.report?.project_id;if(!projectId||personalSession.activeProject()!==projectId){this.setData({qualityData:null,qualityForm:null,qualityPending:null,qualityFields:[],qualityLatest:null});return;}
    const generation=++this.qualityGeneration,saved=Quality.readQualityState(projectId,artifactId);this.setData({qualityLoading:true,qualityPending:saved.pending});
    try{const [media,profiles]=await Promise.allSettled([personalSession.request<ScenarioForms.Context>(Quality.qualityBase(projectId)+'/artifacts/'+encodeURIComponent(artifactId)),personalSession.request<{profiles:ScenarioForms.Profile[]}>(Quality.qualityBase(projectId)+'/scenarios')]);if(!this.qualityMatches(context,projectId,artifactId,generation))return;if(media.status==='rejected')throw media.reason;if(media.value.artifact_id!==artifactId)throw new ClientError('quality_response_mismatch','成片质量回执不匹配，请重新载入');this.setData({qualityData:media.value,qualityProfiles:profiles.status==='fulfilled'?profiles.value.profiles||[]:[],qualityForm:media.value.scenario?ScenarioForms.draft(saved.form,media.value):null,qualityNotice:''});this.syncQuality();}
    catch(error){if(this.qualityMatches(context,projectId,artifactId,generation)&&(this.data.qualityData?.scenario||saved.pending))this.setData({qualityNotice:'场景评审暂不可用：'+message(error)});}
    finally{if(this.qualityMatches(context,projectId,artifactId,generation))this.setData({qualityLoading:false});}
  },
  syncQuality(){const data=this.data.qualityData,form=this.data.qualityForm,latest=data?.reviews?.[0];this.setData({qualityFields:data&&form?form.dimensions.map(row=>{const choices=ScenarioForms.choices(data,this.data.qualityProfiles,row.dimension);return {...row,label:ScenarioForms.label(row.dimension),options:choices.map(item=>item.label),choices,index:Math.max(0,choices.findIndex(item=>item.id===row.status))};}):[],qualityLatest:latest?{...latest,label:ScenarioForms.verdict(latest.assessment?.verdict),dimensionLabels:(latest.review?.dimensions||[]).map(row=>({label:ScenarioForms.label(row.dimension),status:ScenarioForms.statusLabel(row.status),score:row.score??null,note:row.note}))}:null});},
  rememberQuality(form:ScenarioForms.Form){const projectId=this.data.report?.project_id;if(!projectId||this.qualityBlocked()||this.data.reviewSaving||this.data.reviewLoading||this.data.pending||!this.matches(this.data.context))return;this.setData({qualityForm:form});this.syncQuality();try{Quality.saveQualityState(projectId,this.data.artifactId,form);}catch(_){this.setData({qualityNotice:'评审草稿暂时只能保留本页，请检查存储后再提交'});}},
  qualityInput(event:WechatMiniprogram.Input){const index=Number(event.currentTarget.dataset.index),field=String(event.currentTarget.dataset.field),form=this.data.qualityForm;if(!form||!form.dimensions[index]||!['score','note'].includes(field))return;this.rememberQuality({...form,dimensions:form.dimensions.map((row,i)=>i===index?{...row,[field]:event.detail.value}:row)});},
  qualityStatus(event:WechatMiniprogram.PickerChange){const index=Number(event.currentTarget.dataset.index),choice=this.data.qualityFields[index]?.choices[Number(event.detail.value)],form=this.data.qualityForm;if(!form||!choice)return;this.rememberQuality({...form,dimensions:form.dimensions.map((row,i)=>i===index?{...row,status:choice.id,score:['unreviewed','not_applicable'].includes(choice.id)?'':row.score}:row)});},
  async saveQuality(){const data=this.data.qualityData,form=this.data.qualityForm,projectId=this.data.report?.project_id;if(!data||!form||!projectId||!this.data.report?.can_review||this.qualityBlocked()||this.data.reviewLoading||this.data.reviewSaving||this.data.saving||this.data.pending)return;try{await this.sendQualityOperation(Quality.qualityOperation(projectId,ScenarioForms.body(data,form,this.data.qualityProfiles)));}catch(error){this.setData({qualityNotice:message(error)});}},
  async sendQualityOperation(row:Quality.QualityPending,repeat=false){
    const context=this.data.context,artifactId=this.data.artifactId,generation=this.qualityGeneration,current=()=>this.qualityMatches(context,row.projectId,artifactId,generation);if(!current()||this.data.qualitySaving||this.data.qualityLoading||this.data.reviewSaving||this.data.reviewLoading||this.data.saving||this.data.pending)return;this.setData({qualitySaving:true,qualityNotice:''});let confirmed=false;
    try{await Quality.sendQualityReview(row,this.data.qualityForm,repeat,current);if(!current())return;this.setData({qualityPending:null,qualityStaleReport:true,qualityNotice:'场景评审已保存，请核对更新后的成片审核状态'});confirmed=true;}
    catch(error){if(current())this.setData({qualityPending:Quality.readQualityState(row.projectId,artifactId).pending,qualityNotice:message(error)});}
    finally{if(current())this.setData({qualitySaving:false});}
    if(confirmed&&current())await this.loadReview();
  },
  async recoverQuality(event:WechatMiniprogram.TouchEvent){
    const row=this.data.qualityPending,context=this.data.context,artifactId=this.data.artifactId,generation=this.qualityGeneration;if(!row||this.data.qualitySaving||this.data.qualityLoading||this.data.reviewSaving||this.data.reviewLoading||this.data.saving||this.data.pending)return;const current=()=>this.qualityMatches(context,row.projectId,artifactId,generation);if(!current())return;if(event.currentTarget.dataset.repeat)return this.sendQualityOperation(row,true);this.setData({qualitySaving:true});let confirmed=false;
    try{const receipt=await Quality.checkQualityReview(row,current);if(!current())return;confirmed=Boolean(receipt.found&&receipt.result);this.setData({qualityPending:Quality.readQualityState(row.projectId,artifactId).pending,qualityStaleReport:confirmed||this.data.qualityStaleReport,qualityNotice:confirmed?'已核对原场景评审，请核对当前审核状态':'尚未找到原回执，可重放原键与原正文'});}
    catch(error){if(current())this.setData({qualityNotice:message(error)});}
    finally{if(current())this.setData({qualitySaving:false});}
    if(confirmed&&current())await this.loadReview();
  },
  continueEditing() {const report = this.data.report; if (!report?.origin_job_id || !report.project_id || !this.matches(this.data.context)) return; personalSession.selectProject(report.project_id); writeStudioCache({planId: report.origin_job_id}); wx.navigateTo({url: '/pages/studio/index'});},
  openDelivery() {if(this.data.pending||this.data.reviewSaving||this.qualityBlocked())return this.setData({status:"请先核对原审核提交"});if(this.matches(this.data.context))wx.navigateTo({url:'/pages/delivery/index?id='+encodeURIComponent(this.data.artifactId)});},
  compareVersions() {if(this.matches(this.data.context))wx.navigateTo({url:'/pages/compare/index?left='+encodeURIComponent(this.data.artifactId)});},
});
