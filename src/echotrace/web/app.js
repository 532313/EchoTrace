'use strict';
const $ = id => document.getElementById(id);
const state = {project: null, session: null, anchor: null, compare: null, status: null, projects: [], sessions: [], urls: [], player: null, selection: 0};
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const labels = {pedal:'步骤标记',voice:'口述记录',tray:'托盘变化',scene:'画面变化',gap:'记录中断',sensor_fault:'传感器异常'};
const sessionLabel=s=>s.end_reason==='source_restart'?'意外中断后归档':(states[s.state]||s.state);
const states = {recording:'正在记忆',paused:'已暂停',stopped:'记录结束',interrupted:'终端连接中断'};
const fmt = us => {const sec=Math.max(0,Math.floor(us/1e6));return `${String(Math.floor(sec/60)).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`;};
async function api(path, options={}) {
  const headers = {...(options.body ? {'Content-Type':'application/json'} : {}), ...options.headers};
  const token = sessionStorage.getItem('echotraceToken'); if(token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path,{...options,headers});
  if (!response.ok) {let detail;try {detail=(await response.json()).detail;}catch{detail=`请求失败 (${response.status})`;}throw new Error(typeof detail==='string'?detail:JSON.stringify(detail));}
  return options.blob ? response.blob() : response.json();
}
const send = (path, body={}, method='POST') => api(path,{method,body:JSON.stringify(body)});
function toast(message) { $('toast').textContent=message;$('toast').classList.add('visible');clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').classList.remove('visible'),4500); }
async function guarded(action) {try{return await action();}catch(e){toast(e.message);console.error(e);return null;}}
async function promptText(title,description='',value='') {
  $('modalTitle').textContent=title;$('modalDescription').textContent=description;$('modalInput').value=value;$('modalInput').type='text';
  $('modal').showModal();setTimeout(()=>$('modalInput').focus(),50);
  return new Promise(resolve=>$('modal').addEventListener('close',()=>resolve($('modal').returnValue==='ok'?$('modalInput').value:null),{once:true}));
}
async function refreshStatus() {
  state.status=await api('/api/status');
  const count=state.status.devices.length;
  $('connectionDot').classList.add('online');$('connection').textContent=count?'记录终端已连接':'工作台在线 · 终端未连接';
  const models=Object.values(state.status.models).filter(m=>m.status==='files_present').length;
  $('statusText').textContent=`本地 ${state.status.stats.anchors} 条记忆 · ${count} 个终端 · ${models}/5 组模型文件已安装`;
}
async function loadProjects() {
  state.projects=await api('/api/projects');
  if(!state.project && state.projects.length) state.project=state.projects[0];
  $('projects').innerHTML=state.projects.map(p=>`<button class="project ${state.project?.id===p.id?'active':''}" data-project="${esc(p.id)}">▤ &nbsp;${esc(p.name)}</button>`).join('');
  document.querySelectorAll('[data-project]').forEach(el=>el.onclick=()=>guarded(async()=>{state.project=state.projects.find(p=>p.id===el.dataset.project);state.session=null;clearEvidence();state.compare=null;$('answer').classList.add('hidden');await loadProjects();}));
  if(state.project){$('projectTitle').textContent=state.project.name;$('projectDescription').textContent='每次标记，都是下一次继续的起点。';await loadSessions();}
}
async function loadSessions() {
  if(!state.project)return;
  state.sessions=await api(`/api/sessions?project_id=${encodeURIComponent(state.project.id)}`);
  $('sessionCount').textContent=`${state.sessions.length} 次`;
  if(!state.session || !state.sessions.some(s=>s.id===state.session.id)) state.session=state.sessions[0]||null;
  $('sessions').innerHTML=state.sessions.map((s,i)=>`<button class="session ${state.session?.id===s.id?'selected':''}" data-session="${esc(s.id)}"><strong>${esc(s.title||`工作记录 ${state.sessions.length-i}`)}</strong><small>${esc(new Date(s.created_ms).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}))} · ${esc(sessionLabel(s))}</small>${s.device_id.includes('synthetic')||s.device_id.includes('simulator')?'<span class="sim">模拟素材 · 不代表真实操作</span>':''}</button>`).join('') || '<div class="empty">还没有工作记录</div>';
  document.querySelectorAll('[data-session]').forEach(el=>el.onclick=()=>guarded(async()=>{state.session=state.sessions.find(s=>s.id===el.dataset.session);clearEvidence();$('summaryText').classList.add('hidden');await loadSessions();}));
  await loadTimeline();
}
async function loadTimeline() {
  if(!state.session){$('timeline').innerHTML='开始一次工作，让过程留下来。';$('timeline').className='empty';return;}
  const result=await api(`/api/sessions/${encodeURIComponent(state.session.id)}/timeline`);state.session=result.session;
  $('timelineTitle').textContent='过程时间线';$('recordingState').textContent=sessionLabel(state.session);
  $('recordingState').className='badge '+state.session.state;$('recordingControls').classList.toggle('hidden',!['recording','paused','interrupted'].includes(state.session.state));
  $('pause').textContent=state.session.state==='paused'?'继续记忆':'暂停';$('mark').disabled=state.session.state!=='recording';
  $('timeline').className='';
  const all=[...result.anchors,...result.events].sort((a,b)=>a.source_time_us-b.source_time_us);
  $('timeline').innerHTML=all.map(a=>`<button class="step ${state.anchor?.id===a.id?'selected':''}" data-anchor="${esc(a.id)}" ${a.kind==='gap'||a.kind==='sensor_fault'?'disabled':''}><time>${fmt(a.source_time_us-state.session.started_us)}</time><p>${esc(a.note||a.asr_text||a.caption||labels[a.kind]||'操作记录')}</p><span class="tag ${a.caption&&!a.note&&!a.asr_text?'derived':''}">${esc(a.caption&&!a.note&&!a.asr_text?'自动描述':labels[a.kind]||a.kind)}</span></button>`).join('') || '<div class="empty">正在等待第一个操作标记。</div>';
  document.querySelectorAll('[data-anchor]').forEach(el=>el.onclick=()=>guarded(()=>showEvidence(el.dataset.anchor)));
}
async function mediaURL(path) {const blob=await api(path,{blob:true});const url=URL.createObjectURL(blob);state.urls.push(url);return url;}
function releaseMedia(){state.player?.dispose();state.player=null;state.urls.forEach(URL.revokeObjectURL);state.urls=[];}
function clearEvidence(){++state.selection;releaseMedia();state.anchor=null;$('evidence').className='empty';$('evidence').textContent='选择步骤，查看对应的原始证据。';}
async function showEvidence(id) {
  const selection=++state.selection;
  releaseMedia();
  const result=await api(`/api/anchors/${encodeURIComponent(id)}`);if(selection!==state.selection)return;state.anchor=result.anchor;
  releaseMedia();$('evidence').className='';
  const a=result.anchor;
  $('evidence').innerHTML=`<div id="evidencePlayer"><video class="media-frame" playsinline muted></video><audio hidden></audio><div class="playback-controls"><button data-play>播放</button><button data-begin>前段开头</button><label><input type="checkbox" checked> 声音</label><output aria-live="polite"></output></div><input class="playback-slider" type="range" min="0" step="20" value="0" aria-label="证据播放进度"><div class="playback-message" role="status"></div><div class="playback-gaps"></div></div><div class="media-actions"><button id="fetchEvidence">获取原始片段</button><button id="makeFrame">提取关键帧</button><button id="compareEvidence">${state.compare?'与已选步骤对照':'设为对照步骤'}</button><button id="editNote">编辑笔记</button></div><div id="keyframe"></div><div class="evidence-note"><small>你的笔记</small>${esc(a.note||'尚未添加')}</div>${a.asr_text?`<div class="evidence-note"><small>语音转写</small>${esc(a.asr_text)}</div>`:''}${a.caption?`<div class="evidence-note"><small>自动画面描述 · 请以原图为准</small>${esc(a.caption)}</div>`:''}<div class="evidence-meta">来源时间 ${fmt(a.source_time_us-a.started_us)} · ${esc(labels[a.kind])}<br>证据 ${esc(a.id.slice(0,12))}<br>原始记录和自动描述分别保存。</div>`;
  state.player=new EvidencePlayback($('evidencePlayer'),result.playback,(path,signal)=>api(path,{blob:true,signal}),toast);
  await state.player.seek(a.source_time_us);
  if(selection!==state.selection)return;
  if(a.frame_path)await displayFrame(a.id);
  if(selection!==state.selection)return;
  $('fetchEvidence').onclick=()=>guarded(async()=>{for(const m of result.media.filter(m=>!m.available))await send(`/api/media/${encodeURIComponent(m.id)}/fetch`);toast('已请求记录终端传输片段');setTimeout(()=>guarded(()=>showEvidence(id)),1500);});
  $('makeFrame').onclick=()=>guarded(async()=>{await send(`/api/anchors/${encodeURIComponent(id)}/frame`);await displayFrame(id);});
  $('makeFrame').disabled=!result.playback.video.length;
  $('compareEvidence').disabled=!result.playback.video.length;
  $('editNote').onclick=()=>guarded(async()=>{const note=await promptText('编辑这一步的笔记','原始口述与历史修改仍会保留。',a.note);if(note!==null){await send(`/api/anchors/${encodeURIComponent(id)}/note`,{note,revision:a.revision},'PATCH');await showEvidence(id);await loadTimeline();}});
  $('compareEvidence').onclick=()=>guarded(async()=>{if(!state.compare){state.compare=id;toast('已设为对照步骤，请选择另一条记录');return;}const r=await api(`/api/compare?left=${encodeURIComponent(state.compare)}&right=${encodeURIComponent(id)}`);const pair=[r.left.anchor,r.right.anchor];for(const x of pair)await send(`/api/anchors/${encodeURIComponent(x.id)}/frame`);const urls=await Promise.all(pair.map(x=>mediaURL(`/api/anchors/${encodeURIComponent(x.id)}/frame`)));$('keyframe').innerHTML=`<div class="comparison">${pair.map((x,i)=>`<div><img alt="操作对照 ${i+1}" src="${esc(urls[i])}"><small>${esc(x.note||x.asr_text||x.title)}</small></div>`).join('')}</div>`;state.compare=null;});
  document.querySelectorAll('[data-anchor]').forEach(el=>el.classList.toggle('selected',el.dataset.anchor===id));
}
async function displayFrame(id){const url=await mediaURL(`/api/anchors/${encodeURIComponent(id)}/frame`);if(state.anchor?.id!==id){URL.revokeObjectURL(url);state.urls=state.urls.filter(item=>item!==url);return;}$('keyframe').innerHTML=`<img class="media-frame" alt="原始视频关键帧" src="${esc(url)}">`;}
$('addProject').onclick=()=>guarded(async()=>{const name=await promptText('新建项目','给这段动手经历起个名字。');if(name?.trim()){state.project=await send('/api/projects',{name});state.session=null;clearEvidence();await loadProjects();}});
$('settings').onclick=()=>guarded(async()=>{const token=await promptText('连接令牌','仅网络部署需要。令牌保存在当前浏览器会话中。',sessionStorage.getItem('echotraceToken')||'');if(token!==null){sessionStorage.setItem('echotraceToken',token);await boot();}});
$('start').onclick=()=>guarded(async()=>{await refreshStatus();if(!state.project){toast('先创建一个项目');return;}const device=state.status.devices[0];if(!device){toast('请先连接 Echo-Mate，或启动文档中的模拟采集端。');return;}const title=await promptText('开始一次工作','会话将使用记录终端的时间基准。');if(title===null)return;state.session=await send('/api/sessions',{project_id:state.project.id,device_id:device.device_id,boot_id:device.boot_id,source_time_us:device.source_time_us,title});await loadSessions();});
$('mark').onclick=()=>guarded(async()=>{const note=await promptText('记下这一步','可以写下你现在做了什么，以及为什么。');if(note===null)return;await send(`/api/sessions/${encodeURIComponent(state.session.id)}/mark`,{note});toast('步骤已标记');await loadTimeline();});
async function changeState(next){if(!state.session)return;await refreshStatus();const device=state.status.devices.find(d=>d.device_id===state.session.device_id);await send(`/api/sessions/${encodeURIComponent(state.session.id)}/state`,{state:next,source_time_us:Math.max(state.session.last_us,device?.source_time_us||state.session.last_us)},'PATCH');await loadSessions();}
$('pause').onclick=()=>guarded(()=>changeState(state.session.state==='paused'?'recording':'paused'));
$('stop').onclick=()=>guarded(()=>changeState('stopped'));
$('summary').onclick=()=>guarded(async()=>{if(!state.session)return;const result=await send(`/api/sessions/${encodeURIComponent(state.session.id)}/summary`);$('summaryText').textContent=result.summary;$('summaryText').classList.remove('hidden');});
$('deleteSession').onclick=()=>guarded(async()=>{if(!state.session)return;const text=await promptText('删除工作记录','会一并删除原始片段、笔记和索引。输入“删除”确认。');if(text!=='删除')return;await api(`/api/sessions/${encodeURIComponent(state.session.id)}`,{method:'DELETE'});state.session=null;clearEvidence();$('evidence').textContent='记录已删除';await loadSessions();});
async function ask(){const text=$('question').value.trim();if(!text)return;$('ask').disabled=true;try{const result=await send('/api/query',{text,project_id:state.project?.id||null,semantic:true});$('answer').classList.remove('hidden');$('answer').innerHTML=`<p>${esc(result.answer)}</p>${result.warnings.map(w=>`<small>${esc(w)}</small>`).join('')}${result.hits.map((h,i)=>`<button data-result="${esc(h.anchor.id)}">${i+1}. ${esc((h.anchor.note||h.anchor.asr_text||h.anchor.caption||'操作标记').slice(0,32))}</button>`).join('')}`;document.querySelectorAll('[data-result]').forEach(el=>el.onclick=()=>guarded(()=>showEvidence(el.dataset.result)));if(result.hits.length)await showEvidence(result.hits[0].anchor.id);}finally{$('ask').disabled=false;}}
$('ask').onclick=()=>guarded(ask);$('question').onkeydown=e=>{if(e.key==='Enter')guarded(ask);};
$('export').onclick=()=>guarded(async()=>{if(!state.anchor){toast('先选择一条步骤记录');return;}const result=await send('/api/export',{anchor_ids:[state.anchor.id]});const url=await mediaURL(result.url);const link=document.createElement('a');link.href=url;link.download='EchoTrace-evidence.zip';link.click();toast('证据包已导出，包含原始记录与校验值');});
async function boot(){await refreshStatus();await loadProjects();}
guarded(boot);setInterval(()=>guarded(async()=>{await refreshStatus();if(state.session&&['recording','paused','interrupted'].includes(state.session.state))await loadTimeline();}),3000);
