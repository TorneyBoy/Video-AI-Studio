import {message,personalSession} from '../../shared/api';
import {WorkItem,WorkPage} from '../../shared/delivery-state';
Page({
 data:{items:[] as Array<WorkItem & {label:string;spec:string}>,status:'',loading:false,signedIn:false,projectSelected:false,context:'',cursor:null as string|null,more:false,total:0,filter:0,filters:['全部','处理中','已完成','需处理'],states:['all','processing','completed','attention']},
 generation:0,
 onHide(){this.generation+=1;this.setData({loading:false});},onUnload(){this.onHide();},
 async onShow(){await this.load();},async onPullDownRefresh(){try{await this.load();}finally{wx.stopPullDownRefresh();}},
 async load(more=false){if(this.data.loading)return;const generation=++this.generation;this.setData({loading:true,status:''});
  try{const profile=personalSession.profile||await personalSession.restore();const project=personalSession.activeProject();const context=`${profile?.id}:${profile?.tenant_id}:${project}`;
   if(generation!==this.generation)return;this.setData({signedIn:Boolean(profile),projectSelected:Boolean(project)});if(!profile||!project){this.setData({items:[]});return;}
   if(context!==this.data.context){more=false;this.setData({context,items:[],cursor:null});}
   const cursor=more&&this.data.cursor?'&cursor='+encodeURIComponent(this.data.cursor):'';
   const page=await personalSession.request<WorkPage>(`/projects/${encodeURIComponent(project)}/works?state=${this.data.states[this.data.filter]}&limit=20${cursor}`);
   if(generation!==this.generation||profile.id!==personalSession.profile?.id||project!==personalSession.activeProject())return;
   const rows=page.items.map(item=>({...item,label:(item.revision_number?'v'+item.revision_number+' · ':'')+(item.quality==='draft'?'预览视频':'高清成片'),spec:item.duration!==null?`${item.duration}秒 · ${item.width}×${item.height}`:'规格待同步'}));
   const items=[...(more?this.data.items:[]),...rows];this.setData({items:items.filter((row,index)=>items.findIndex(item=>item.job_id===row.job_id)===index),cursor:page.next_cursor,more:page.has_more,total:page.total_jobs});
  }catch(error){if(generation===this.generation)this.setData({status:message(error)});}finally{if(generation===this.generation)this.setData({loading:false});}},
 filterChange(event:WechatMiniprogram.PickerChange){this.setData({filter:Number(event.detail.value)});this.load();},moreWorks(){this.load(true);},refreshWorks(){this.load(false);},
 openWork(event:WechatMiniprogram.TouchEvent){const row=this.data.items.find(item=>item.job_id===String(event.currentTarget.dataset.id));if(row?.artifact_id)wx.navigateTo({url:'/pages/work-detail/index?id='+encodeURIComponent(row.artifact_id)});else if(row)this.setData({status:row.reason});},
 chooseProject(){wx.switchTab({url:'/pages/create/index'});},login(){wx.navigateTo({url:'/pages/login/index'});}
});
