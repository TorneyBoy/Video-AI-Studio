// Independent of the page modules to avoid circular operation-helper imports.
export function hasPendingWave(context:string):boolean{
 const value=wx.getStorageSync('video-ai-studio.batch-run-operation.v1.'+context);
 return Boolean(context&&value&&typeof value==='object'&&value.context===context&&typeof value.key==='string')||Boolean(deliveryWaveReference(context));
}
export function deliveryWaveReference(context:string):Record<string,unknown>|null{const value=wx.getStorageSync('video-ai-studio.batch-delivery-operation.v1.'+context);return context&&value&&typeof value==='object'&&value.context===context&&typeof value.key==='string'?value:null;}
