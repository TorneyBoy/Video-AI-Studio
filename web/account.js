// Account UI reads verified server identity; it never selects a tenant claim.
async function loadAccountSummary() {
  const name = document.querySelector('#accountName');
  const role = document.querySelector('#accountRole');
  const space = document.querySelector('#workspaceName');
  const avatar = document.querySelector('#accountAvatar');
  const manage = document.querySelector('#manageAccountLink');
  if (authMode === 'session') {
    try {
      const user = await api('/auth/me');
      if (name) name.textContent = user.display_name;
      if (role) role.textContent = '个人账号';
      if (space) space.textContent = `${user.display_name}的工作台`;
      if (avatar) avatar.textContent = String(user.display_name || '我').slice(0, 1);
      if (manage) { manage.hidden = false; manage.href = '/login'; }
    } catch (_) {
      if (name) name.textContent = '账号信息暂未同步';
      if (role) role.textContent = '请稍后刷新';
    }
  } else {
    if (name) name.textContent = authMode === 'trusted_proxy' ? '验收工作台' : '开发工作台';
    if (role) role.textContent = authMode === 'trusted_proxy' ? '通过私有入口访问' : '开发身份';
    if (space) space.textContent = '当前工作空间';
    if (avatar) avatar.textContent = '我';
    if (manage) manage.hidden = true;
  }
}
