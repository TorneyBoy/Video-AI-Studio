import {message, personalSession, publicRequest} from '../../shared/api';
import {AuthCapabilities} from '../../shared/contracts';

Page({
  data: {loginName: '', password: '', displayName: '', register: false, remember: true,
    busy: false, registrationAvailable: false, available: false, status: '正在连接账号服务…'},
  async onLoad() {
    try {
      const result = await publicRequest<AuthCapabilities>('/auth/capabilities');
      this.setData({available: result.account_login, registrationAvailable: result.registration,
        status: result.account_login ? '' : '账号服务尚未启用'});
    } catch (error) {this.setData({status: message(error)});}
  },
  loginNameInput(e: WechatMiniprogram.Input) {this.setData({loginName: e.detail.value});},
  passwordInput(e: WechatMiniprogram.Input) {this.setData({password: e.detail.value});},
  displayNameInput(e: WechatMiniprogram.Input) {this.setData({displayName: e.detail.value});},
  rememberChange(e: WechatMiniprogram.SwitchChange) {this.setData({remember: e.detail.value});},
  toggleRegister() {this.setData({register: !this.data.register, status: ''});},
  async submit() {
    if (this.data.busy || !this.data.available) return;
    if (!this.data.loginName.trim() || !this.data.password) return this.setData({status: '请填写账号和密码'});
    this.setData({busy: true, status: this.data.register ? '正在创建个人空间…' : '正在登录…'});
    try {
      await personalSession.login(this.data.loginName.trim(), this.data.password, this.data.remember,
        this.data.register ? this.data.displayName.trim() : undefined);
      this.setData({password: ''});
      wx.switchTab({url: '/pages/create/index'});
    } catch (error) {this.setData({status: message(error)});}
    finally {this.setData({busy: false});}
  },
});
