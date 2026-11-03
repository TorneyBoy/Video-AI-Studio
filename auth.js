"use strict";
const accountForm = document.querySelector('#accountForm');
const accountStatus = document.querySelector('#accountStatus');
const submitAccount = document.querySelector('#submitBtn');
const switchAccountMode = document.querySelector('#switchMode');
let registrationMode = false;

function accountMessage(text, error = false) {
  accountStatus.textContent = text;
  accountStatus.dataset.error = String(error);
}

async function accountRequest(path, body) {
  const response = await fetch('/api/v1/auth' + path, {method: body ? 'POST' : 'GET',
    credentials: 'same-origin', cache: 'no-store', headers: {'Content-Type': 'application/json'},
    ...(body ? {body: JSON.stringify(body)} : {})});
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(data?.detail?.message || (response.status === 429 ? '操作较频繁，请稍后重试' : '暂时无法完成，请稍后重试'));
    error.status = response.status;
    throw error;
  }
  return data;
}

switchAccountMode.addEventListener('click', () => {
  registrationMode = !registrationMode;
  document.querySelector('#displayField').hidden = !registrationMode;
  document.querySelector('#passwordHint').hidden = !registrationMode;
  accountForm.elements.password.autocomplete = registrationMode ? 'new-password' : 'current-password';
  accountForm.elements.password.minLength = registrationMode ? 15 : 1;
  document.querySelector('#formTitle').textContent = registrationMode ? '创建你的个人空间' : '继续创作';
  submitAccount.textContent = registrationMode ? '创建账号并进入' : '登录工作台';
  switchAccountMode.textContent = registrationMode ? '已有账号，去登录' : '创建个人账号';
  accountMessage('');
});

accountForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (submitAccount.disabled) return;
  const payload = {login_name: accountForm.elements.login_name.value.trim(),
    password: accountForm.elements.password.value, client: 'web'};
  if (registrationMode) payload.display_name = accountForm.elements.display_name.value.trim();
  submitAccount.disabled = true;
  accountMessage(registrationMode ? '正在创建个人空间…' : '正在登录…');
  try {
    await accountRequest(registrationMode ? '/register' : '/login', payload);
    accountForm.elements.password.value = '';
    // Browser credentials stay in HttpOnly cookies. Never save passwords,
    // access tokens or refresh tokens in localStorage or page state.
    window.location.assign('/');
  } catch (error) { accountMessage(error.message, true); }
  finally { submitAccount.disabled = false; }
});

document.querySelector('#logoutBtn').addEventListener('click', async event => {
  event.currentTarget.disabled = true;
  try {
    await accountRequest('/logout', {client: 'web'});
    for (const key of ['video-ai-studio.project-id', 'video-ai-studio.workspace', 'video-ai-studio.workspace-owner', 'video-ai-studio.tenant-id']) localStorage.removeItem(key);
    window.location.reload();
  } catch (error) { accountMessage(error.message, true); event.currentTarget.disabled = false; }
});

(async () => {
  try {
    const capabilities = await accountRequest('/capabilities');
    if (!capabilities.account_login) return accountMessage('当前环境尚未启用账号登录。', true);
    switchAccountMode.hidden = !capabilities.registration;
    let current;
    const restore = async () => {
      try { return await accountRequest('/me'); }
      catch (error) {
        if (error.status !== 401) throw error;
        try { await accountRequest('/refresh', {client: 'web'}); return await accountRequest('/me'); }
        catch (refreshError) { if (![401, 403].includes(refreshError.status)) throw refreshError; }
      }
    };
    current = await (navigator.locks ? navigator.locks.request('video-ai-session-refresh', restore) : restore());
    if (current) {
      document.querySelector('#currentAccount').hidden = false;
      document.querySelector('#currentName').textContent = current.display_name;
      return accountMessage('个人空间已连接。');
    }
    accountForm.hidden = false;
    accountMessage('');
  } catch (error) { accountMessage(error.message, true); }
})();
