import {message, personalSession} from '../../shared/api';
import {ArtifactAccess, ClientError} from '../../shared/contracts';
import {blankReviewIssue, checkReview, readReviewState, ReviewDecision, ReviewForm, ReviewPending, ReviewRecord,
  ReviewReport, reviewBody, reviewContext, reviewOperation, reviewStatus, saveReviewState, sendReview} from '../../shared/review-state';
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
    report: null as ReviewReport | null, reviewLabel: '', reviewLoading: false, reviewSaving: false, reviewConflict: false,
    form: {note: '', issues: []} as ReviewForm, pending: null as ReviewPending | null,
    categories: ['画面','声音','字幕','内容','其他'], categoryValues: ['picture','audio','subtitle','content','other'],
    severities: ['错误：需修改','提醒：请注意','说明'], severityValues: ['error','warning','info'],
    issueIndexes: [] as Array<{category: number; severity: number}>, videoTime: 0,
    history: [] as Array<ReviewRecord & {label: string}>, showHistory: false, historyOffset: 0, historyMore: false},
  authorizationGeneration: 0,
  reviewGeneration: 0,
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
    this.reviewGeneration += 1;
    this.setData({url: '', loading: false, saving: false, reviewLoading: false, reviewSaving: false});
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
    if (this.data.saving || !this.data.artifactId || !this.data.report?.can_download || this.data.pending || this.data.reviewSaving || !this.matches(this.data.context)) return;
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
    const context = this.data.context; if (!this.matches(context)) return this.setData({url: '', report: null, status: '账号已变化，请从作品列表重新打开'});
    const generation = ++this.reviewGeneration; this.setData({reviewLoading: true});
    try {const report = await personalSession.request<ReviewReport>(`/artifacts/${encodeURIComponent(this.data.artifactId)}/review`);
      if (this.matches(context) && generation === this.reviewGeneration) {this.applyReport(report); this.setData({history: [], showHistory: false, historyOffset: 0, reviewConflict: false});}
    } catch (error) {if (this.matches(context) && generation === this.reviewGeneration) this.setData({status: message(error)});}
    finally {if (this.matches(context) && generation === this.reviewGeneration) this.setData({reviewLoading: false});}
  },
  async refreshReview() {await this.loadReview(); await this.authorize();},
  videoTime(event: WechatMiniprogram.VideoTimeUpdate) {this.setData({videoTime: event.detail.currentTime});},
  seekIssue(event: WechatMiniprogram.TouchEvent) {const start = Number(event.currentTarget.dataset.start); if (this.data.url && Number.isFinite(start) && start >= 0 && start <= (this.data.report?.duration || 0)) wx.createVideoContext('workPreview', this).seek(start);},
  rememberReview(form: ReviewForm) {
    if (!this.matches(this.data.context) || this.data.pending || this.data.reviewSaving) return;
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
    const report = this.data.report; if (!report?.can_review || this.data.pending || this.data.reviewSaving || this.data.reviewLoading || this.data.reviewConflict) return;
    try {const decision: ReviewDecision = event.currentTarget.dataset.decision === 'approved' ? 'approved' : 'changes_requested'; await this.sendReviewOperation(reviewOperation(this.data.artifactId, reviewBody(report, this.data.form, decision)));}
    catch (error) {this.setData({status: message(error)});}
  },
  async sendReviewOperation(row: ReviewPending) {
    const context = this.data.context; const generation = this.reviewGeneration; this.setData({reviewSaving: true, status: ''});
    try {const result = await sendReview(row, this.data.form); if (!this.matches(context) || generation !== this.reviewGeneration) return;
      this.restoreReviewDraft(); this.applyReport(result.current); this.setData({status: '审核决定已保存。', history: [], showHistory: false, historyOffset: 0});
    } catch (error) {if (this.matches(context) && generation === this.reviewGeneration) {this.setData({pending: readReviewState(this.data.artifactId).pending, reviewConflict: error instanceof ClientError && [409,403].includes(error.status), status: message(error)});}}
    finally {if (this.matches(context) && generation === this.reviewGeneration) this.setData({reviewSaving: false});}
  },
  async recoverReview(event: WechatMiniprogram.TouchEvent) {
    const row = this.data.pending; if (!row || this.data.reviewSaving) return;
    if (event.currentTarget.dataset.repeat) return this.sendReviewOperation(row);
    const context = this.data.context; const generation = this.reviewGeneration; this.setData({reviewSaving: true});
    try {const result = await checkReview(row); if (!this.matches(context) || generation !== this.reviewGeneration) return;
      this.applyReport(result.current); this.restoreReviewDraft(); this.setData({status: result.found ? '已核对原审核提交。' : '尚未找到原回执，可沿用原提交继续。'});
    } catch (error) {if (this.matches(context) && generation === this.reviewGeneration) this.setData({status: message(error)});}
    finally {if (this.matches(context) && generation === this.reviewGeneration) this.setData({reviewSaving: false});}
  },
  discardReviewDraft() {if (!this.data.pending && !this.data.reviewSaving) {try {saveReviewState(this.data.artifactId, {note: '', issues: []}); this.restoreReviewDraft();} catch (_) {this.setData({status: '草稿存储暂不可用'});}}},
  async history() {
    const context = this.data.context; const generation = this.reviewGeneration; const offset = this.data.showHistory ? this.data.historyOffset : 0;
    try {const rows = await personalSession.request<ReviewRecord[]>(`/artifacts/${encodeURIComponent(this.data.artifactId)}/reviews?skip=${offset}&limit=20`);
      if (!this.matches(context) || generation !== this.reviewGeneration) return;
      const all = [...(offset ? this.data.history : []), ...rows.map(row => ({...row, label: reviewStatus(row.decision)}))]; this.setData({history: all.filter((row,index) => all.findIndex(item => item.id === row.id) === index), showHistory: true, historyOffset: offset + rows.length, historyMore: rows.length === 20});
    } catch (error) {if (this.matches(context)) this.setData({status: message(error)});}
  },
  continueEditing() {const report = this.data.report; if (!report?.origin_job_id || !report.project_id || !this.matches(this.data.context)) return; personalSession.selectProject(report.project_id); writeStudioCache({planId: report.origin_job_id}); wx.navigateTo({url: '/pages/studio/index'});},
  openDelivery() {if(this.data.pending||this.data.reviewSaving)return this.setData({status:"请先核对原审核提交"});if(this.matches(this.data.context))wx.navigateTo({url:'/pages/delivery/index?id='+encodeURIComponent(this.data.artifactId)});},
  compareVersions() {if(this.matches(this.data.context))wx.navigateTo({url:'/pages/compare/index?left='+encodeURIComponent(this.data.artifactId)});},
});
