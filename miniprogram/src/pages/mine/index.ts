import {message, personalSession} from '../../shared/api';
import {AccountProfile} from '../../shared/contracts';

Page({
  data: {profile: null as AccountProfile | null, status: '', busy: false},
  async onShow() {
    try {this.setData({profile: personalSession.profile || await personalSession.restore(), status: ''});}
    catch (error) {this.setData({profile: null, status: message(error)});}
  },
  login() {wx.navigateTo({url: '/pages/login/index'});},
  async logout() {
    if (this.data.busy) return;
    this.setData({busy: true, status: ''});
    try {await personalSession.logout(); this.setData({profile: null, status: '已退出当前账号。'});}
    catch (error) {this.setData({profile: null, status: message(error)});}
    finally {this.setData({busy: false});}
  },
});
