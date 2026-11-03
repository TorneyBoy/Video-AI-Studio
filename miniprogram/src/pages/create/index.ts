import {message, personalSession} from '../../shared/api';
import {AccountProfile, ClientError, Project} from '../../shared/contracts';

Page({
  data: {profile: null as AccountProfile | null, projects: [] as Project[], selectedProjectId: '',
    newProjectName: '', loading: false, creating: false, status: ''},
  async onShow() {await this.load();},
  async onPullDownRefresh() {try {await this.load();} finally {wx.stopPullDownRefresh();}},
  async load() {
    this.setData({loading: true, status: ''});
    try {
      const profile = personalSession.profile || await personalSession.restore();
      this.setData({profile});
      if (!profile) return;
      const projects = await personalSession.request<Project[]>('/projects');
      const current = personalSession.activeProject();
      const selected = projects.some(p => p.id === current) ? current : projects[0]?.id || '';
      if (selected) personalSession.selectProject(selected);
      this.setData({projects, selectedProjectId: selected});
    } catch (error) {
      if (error instanceof ClientError && [401, 403].includes(error.status)) this.setData({profile: null, projects: []});
      this.setData({status: message(error)});
    } finally {this.setData({loading: false});}
  },
  login() {wx.navigateTo({url: '/pages/login/index'});},
  nameInput(e: WechatMiniprogram.Input) {this.setData({newProjectName: e.detail.value});},
  chooseProject(e: WechatMiniprogram.TouchEvent) {
    const id = String(e.currentTarget.dataset.id || '');
    if (!this.data.projects.some(p => p.id === id)) return;
    personalSession.selectProject(id); this.setData({selectedProjectId: id});
  },
  async createProject() {
    const name = this.data.newProjectName.trim();
    if (!name || this.data.creating) return;
    this.setData({creating: true, status: ''});
    try {
      const project = await personalSession.request<Project>('/projects', 'POST', {name});
      personalSession.selectProject(project.id);
      this.setData({newProjectName: '', selectedProjectId: project.id});
      await this.load();
    } catch (error) {this.setData({status: message(error)});}
    finally {this.setData({creating: false});}
  },
  openBatches(){wx.navigateTo({url:'/pages/batches/index'});},
  openStoryboards(){wx.navigateTo({url:'/pages/storyboards/index'});},
  openAssets() {wx.switchTab({url: '/pages/assets/index'});},
  openStudio() {wx.navigateTo({url: '/pages/studio/index'});},
  openBilling() {wx.navigateTo({url: '/pages/billing/index'});},
  openWorks() {wx.switchTab({url: '/pages/works/index'});},
});
