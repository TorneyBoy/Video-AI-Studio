import {clientConfig} from '../config';
import {AccountGrant, AccountProfile, ClientError, object, serverError} from './contracts';

const SESSION_KEY = 'video-ai-studio.session.v1';
const WORKSPACE_KEY = 'video-ai-studio.workspace.v1';

function endpoint(path: string): string {
  const base = clientConfig.apiBase.replace(/\/$/, '');
  if (!/^https:\/\/[^/?#@]+\/api\/v1$/.test(base)) throw new ClientError('service_unconfigured', '工作台服务尚未配置');
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('..') || path.includes('://')) throw new ClientError('invalid_endpoint', '请求地址不符合要求');
  return base + path;
}

function transport<T>(path: string, method: 'GET' | 'POST', data?: unknown,
                      accessToken?: string, idempotencyKey?: string): Promise<T> {
  const url = endpoint(path);
  return new Promise((resolve, reject) => {
    const header: Record<string, string> = {'Content-Type': 'application/json'};
    if (accessToken) header.Authorization = 'Bearer ' + accessToken;
    if (idempotencyKey) header['Idempotency-Key'] = idempotencyKey;
    wx.request({url, method, data: data as WechatMiniprogram.IAnyObject,
      header, timeout: clientConfig.requestTimeoutMs,
      success: response => response.statusCode >= 200 && response.statusCode < 300
        ? resolve(response.data as T) : reject(serverError(response.data, response.statusCode)),
      fail: () => reject(new ClientError(method === 'GET' ? 'network_unavailable' : 'submission_unknown',
        method === 'GET' ? '网络暂时不可用，请稍后刷新' : '尚未确认提交结果，请先查询原任务', 0, method !== 'GET')),
    });
  });
}

class PersonalSession {
  private accessToken = '';
  private refreshToken = '';
  private accessExpiresAt = 0;
  private refreshExpiresAt = '';
  private remember = false;
  private refreshPromise: Promise<void> | null = null;
  private generation = 0;
  profile: AccountProfile | null = null;

  private save(grant: AccountGrant, remember: boolean): void {
    if (!grant.access_token || !grant.refresh_token || !grant.user?.id) throw new ClientError('invalid_login_response', '登录服务返回异常');
    const previous = this.profile?.id;
    this.accessToken = grant.access_token;
    this.refreshToken = grant.refresh_token;
    this.accessExpiresAt = Date.parse(grant.access_expires_at);
    this.refreshExpiresAt = grant.refresh_expires_at;
    this.profile = grant.user;
    this.remember = remember;
    if (previous && previous !== grant.user.id) wx.removeStorageSync(WORKSPACE_KEY);
    try {
      if (remember) wx.setStorageSync(SESSION_KEY, {version: 1, refreshToken: this.refreshToken,
        expiresAt: this.refreshExpiresAt, userId: grant.user.id});
      else wx.removeStorageSync(SESSION_KEY);
    } catch (_) {
      this.remember = false;
      wx.showToast({title: '未能保存登录，下次需重新登录', icon: 'none'});
    }
  }

  clear(): void {
    this.generation += 1;
    this.accessToken = ''; this.refreshToken = ''; this.profile = null;
    this.accessExpiresAt = 0; this.refreshExpiresAt = ''; this.remember = false;
    wx.removeStorageSync(SESSION_KEY); wx.removeStorageSync(WORKSPACE_KEY);
  }

  async login(loginName: string, password: string, remember: boolean, displayName?: string): Promise<AccountProfile> {
    const generation = ++this.generation;
    const grant = await transport<AccountGrant>(displayName === undefined ? '/auth/login' : '/auth/register', 'POST',
      {login_name: loginName, password, client: 'mini', ...(displayName === undefined ? {} : {display_name: displayName})});
    if (generation !== this.generation) throw new ClientError('session_changed', '登录状态已变化，请重新登录');
    this.save(grant, remember);
    return grant.user;
  }

  async restore(): Promise<AccountProfile | null> {
    if (!this.refreshToken) {
      const saved = object(wx.getStorageSync(SESSION_KEY));
      if (saved.version === 1 && typeof saved.refreshToken === 'string' && Date.parse(String(saved.expiresAt)) > Date.now()) {
        this.refreshToken = saved.refreshToken; this.remember = true;
      }
    }
    if (!this.refreshToken) return null;
    await this.refresh();
    return this.profile;
  }

  private async refresh(): Promise<void> {
    if (this.refreshPromise) return this.refreshPromise;
    if (!this.refreshToken) throw new ClientError('login_required', '请先登录');
    const generation = this.generation;
    this.refreshPromise = (async () => {
      try {
        const grant = await transport<AccountGrant>('/auth/refresh', 'POST', {client: 'mini', refresh_token: this.refreshToken});
        if (generation !== this.generation) throw new ClientError('session_changed', '登录状态已变化，请重新打开页面');
        this.save(grant, this.remember);
      } catch (error) {
        if (generation === this.generation && error instanceof ClientError && [401, 403].includes(error.status)) this.clear();
        throw error;
      } finally {this.refreshPromise = null;}
    })();
    return this.refreshPromise;
  }

  async request<T>(path: string, method: 'GET' | 'POST' = 'GET', data?: unknown, idempotencyKey?: string): Promise<T> {
    const expectedUser = this.profile?.id;
    if (!this.accessToken || this.accessExpiresAt <= Date.now() + 30000) {
      if (!this.refreshToken) await this.restore();
      else await this.refresh();
    }
    if (!this.accessToken) throw new ClientError('login_required', '请先登录');
    if (expectedUser && expectedUser !== this.profile?.id) throw new ClientError('session_changed', '登录账号已变化，请重新打开页面');
    try { return await transport<T>(path, method, data, this.accessToken, idempotencyKey); }
    catch (error) {
      if (!(error instanceof ClientError) || error.status !== 401) throw error;
      await this.refresh();
      if (expectedUser && expectedUser !== this.profile?.id) throw new ClientError('session_changed', '登录账号已变化，请重新打开页面');
      // Explicit 401 only: preserve the request and its original key.
      return transport<T>(path, method, data, this.accessToken, idempotencyKey);
    }
  }

  async logout(): Promise<void> {
    try {
      if (this.refreshToken) await transport<void>('/auth/logout', 'POST', {client: 'mini', refresh_token: this.refreshToken});
    } catch (_) {
      throw new ClientError('logout_unconfirmed', '已清除本机登录，服务器退出尚未确认；可在联网后从其他设备撤销此会话');
    } finally {this.clear();}
  }

  activeProject(): string {
    const saved = object(wx.getStorageSync(WORKSPACE_KEY));
    return saved.userId === this.profile?.id && typeof saved.projectId === 'string' ? saved.projectId : '';
  }

  selectProject(projectId: string): void {
    if (!this.profile) throw new ClientError('login_required', '请先登录');
    wx.setStorageSync(WORKSPACE_KEY, {userId: this.profile.id, projectId});
  }
}

export const personalSession = new PersonalSession();
export const publicRequest = <T>(path: string): Promise<T> => transport<T>(path, 'GET');
export function message(error: unknown): string {return error instanceof ClientError ? error.message : '暂时无法完成，请稍后重试';}
