// Fragment credentials stay in this page's memory and never enter storage/logs.
(() => {
  let token=''; try {token=decodeURIComponent(window.location.hash.slice(1));} catch (_) { /* Invalid fragments remain unusable. */ }
  window.history.replaceState(null,'',window.location.pathname);
  let current=null;let busy=false;
  const el=id=>document.getElementById(id);
  function controls(){for(const id of ['playShare','downloadShare','manifestShare','srtShare','vttShare'])el(id).disabled=busy||!current||(id!=='playShare'&&!current.allow_download)||(['srtShare','vttShare'].includes(id)&&!current.include_subtitles);el('inspectShare').disabled=busy;}
  async function call(path,extra={}){
    if(!/^sg1\.[A-Fa-f0-9-]{36}\.[A-Za-z0-9_-]{43}$/.test(token))throw new Error('请重新打开完整的原分享链接。');
    const response=await fetch('/api/v1/shared/'+path,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,...extra})});
    if(!response.ok){if([401,403,404,410].includes(response.status)){current=null;el('sharedVideo').pause();el('sharedVideo').removeAttribute('src');el('sharedVideo').hidden=true;el('sharedDownload').replaceChildren();}el('shareLogin').hidden=response.status!==401;throw new Error(response.status===401?'此分享需登录工作台。登录后回到本页刷新。':response.status===403?'此分享未开放该操作，或当前账号无权访问。':[404,410].includes(response.status)?'分享不可访问或已失效，请联系分享者。':response.status===429?'请求较多，请稍后刷新。':'分享服务暂不可用，请稍后刷新。');}
    return response.json();
  }
  async function run(action){if(busy)return;busy=true;el('shareStatus').textContent='';controls();try{await action();}catch(error){el('shareStatus').textContent=error.message;}finally{busy=false;controls();}}
  async function inspect(){await run(async()=>{current=null;el('sharedDownload').replaceChildren();const value=await call('inspect');current=value;el('shareTitle').textContent=value.label;el('shareSummary').textContent=`${value.edit_revision?'版本 v'+value.edit_revision+' · ':''}${Number(value.duration).toFixed(3)} 秒 · ${value.measurements.width}×${value.measurements.height} · 已审核`;el('shareExpiry').textContent='分享截止：'+new Date(value.expires_at).toLocaleString();el('shareLogin').hidden=true;});}
  async function media(purpose){await run(async()=>{const value=await call('access',{purpose});if(!/^https:\/\//.test(value.url))throw new Error('媒体授权无效，请刷新。');if(purpose==='preview'){el('sharedVideo').src=value.url;el('sharedVideo').hidden=false;el('shareStatus').textContent='播放地址已刷新，请点击播放器播放。';}else{const link=document.createElement('a');link.href=value.url;link.target='_blank';link.rel='noopener';link.textContent='打开视频下载地址（短期有效）';el('sharedDownload').replaceChildren(link);}});}
  async function file(kind){await run(async()=>{const value=await call('file',{kind});if(typeof value.content!=='string'||globalThis.sha256(value.content)!==value.sha256||!/^video-(delivery\.json|subtitles\.(srt|vtt))$/.test(value.filename))throw new Error('文件校验未通过，请刷新后重试。');const url=URL.createObjectURL(new Blob([value.content],{type:'application/octet-stream;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download=value.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),0);el('shareStatus').textContent='文件已准备，请查看浏览器下载记录。';});}
  el('inspectShare').addEventListener('click',inspect);el('playShare').addEventListener('click',()=>media('preview'));el('downloadShare').addEventListener('click',()=>media('download'));
  for(const [id,kind] of [['manifestShare','manifest'],['srtShare','srt'],['vttShare','vtt']])el(id).addEventListener('click',()=>file(kind));
  el('sharedVideo').addEventListener('error',()=>{el('shareStatus').textContent='播放未完成，授权可能已过期，请刷新播放授权。';});
  inspect();
})();
