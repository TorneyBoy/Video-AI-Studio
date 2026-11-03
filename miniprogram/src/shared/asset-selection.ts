import {personalSession} from './api';
import {object} from './contracts';

const KEY = 'video-ai-studio.asset-selection.v1';
function context(): string {const p = personalSession.profile; return p ? `${p.id}:${p.tenant_id}:${personalSession.activeProject()}` : '';}
export function selectedAssets(): string[] {
  const saved = object(wx.getStorageSync(KEY));
  return saved.context === context() && Array.isArray(saved.ids) ? saved.ids.filter((id): id is string => typeof id === 'string').slice(0, 500) : [];
}
export function toggleAsset(id: string): void {
  const ids = new Set(selectedAssets()); if (ids.has(id)) ids.delete(id); else ids.add(id);
  wx.setStorageSync(KEY, {context: context(), ids: [...ids].slice(0, 500)});
}
export function addSelectedAsset(id: string): void {
  wx.setStorageSync(KEY, {context: context(), ids: [...new Set([...selectedAssets(), id])].slice(0, 500)});
}
