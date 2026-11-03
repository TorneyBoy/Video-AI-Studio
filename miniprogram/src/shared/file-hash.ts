import {sha256} from '../vendor/sha256';
import {ClientError} from './contracts';

/** Prefer the platform digest; fall back to bounded reads on older base libs.
 * No video decoding, transcoding or analysis runs on the client. */
export async function fileFingerprint(path: string, expectedSize: number, progress: (value: number) => void): Promise<string> {
  const fs = wx.getFileSystemManager();
  try {
    const native = await new Promise<WechatMiniprogram.GetFileInfoSuccessCallbackResult>((resolve, reject) =>
      fs.getFileInfo({filePath: path, digestAlgorithm: 'sha256', success: resolve, fail: reject}));
    if (native.size !== expectedSize) throw new ClientError('file_changed', '文件大小发生变化，请重新选择');
    if (/^[a-f0-9]{64}$/i.test(native.digest)) {progress(100); return native.digest.toLowerCase();}
  } catch (error) {if (error instanceof ClientError) throw error;}
  const fd = await new Promise<string>((resolve, reject) => fs.open({filePath: path, flag: 'r',
    success: result => resolve(result.fd), fail: () => reject(new ClientError('file_missing', '临时文件已失效，请重新选择原文件'))}));
  const hash = sha256.create(); const chunkSize = 1024 * 1024;
  try {
    for (let offset = 0; offset < expectedSize;) {
      const length = Math.min(chunkSize, expectedSize - offset);
      const result = await new Promise<WechatMiniprogram.ReadSuccessCallbackResult>((resolve, reject) => fs.read({
        fd, arrayBuffer: new ArrayBuffer(length), offset: 0, length, position: offset,
        success: resolve, fail: () => reject(new ClientError('file_unreadable', '文件读取失败，请重新选择原文件'))}));
      if (result.bytesRead <= 0) throw new ClientError('file_changed', '文件内容不完整，请重新选择原文件');
      hash.update(new Uint8Array(result.arrayBuffer, 0, result.bytesRead));
      offset += result.bytesRead; progress(Math.round(offset / expectedSize * 100));
    }
    return hash.hex();
  } finally {await new Promise<void>(resolve => fs.close({fd, complete: () => resolve()}));}
}
