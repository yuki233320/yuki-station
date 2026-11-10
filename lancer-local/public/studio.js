(()=>{'use strict';
 const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const token=document.querySelector('meta[name="local-token"]').content;
 const colors={opus:'#bc8b74',sol:'#6c8e84',gemini:'#7e99c9',deepseek:'#738bc0',image:'#ba8f9e',codex:'#83908c',native_codex:'#708d84'};
 const icons={opus:'claude-color.svg',gemini:'gemini-color.svg',deepseek:'deepseek-color.svg',sol:'openai.svg',codex:'openai.svg',image:'openai.svg',native_codex:'openai.svg'};
 const short={opus:'Opus',sol:'Sol',gemini:'Gemini',deepseek:'DeepSeek',image:'Sunburst',codex:'Codex',native_codex:'原生 Codex'};
 const phases={draft:'全局规划',review:'交叉审核',decision:'主模型裁决',build:'派生执行',verification:'产物独立审查',clarification:'定向协作',integration:'汇总交付',routing:'宿主规则路由'};
 const roleNames={'discussion-group':'讨论小组',coordinator:'总负责人',architecture:'架构负责人',frontend:'前端负责人',backend:'后端负责人',research:'研究员',writing:'内容负责人',design:'视觉负责人',data:'数据负责人',qa:'质量审查员'};
 const workNames={development:'软件开发',research:'研究',writing:'写作',design:'设计',data:'数据处理',general:'常规工作',image:'图像生成'};
 let triggerData=null,triggerProjectId=null,triggerEdit=null,triggerTargetDraft=null,triggerRuleInitial='',mcpPolicyDraft=null,workerPolicyDraft=null,roleGuideDraft={},editedRoleId='coordinator',editedCustomRole=null,teamDraft=null,teamEdit=null,hostAcceptanceDraft=null,hostAcceptanceProjectId=null,modelPolicyEdit=null;
 const familyNames={anthropic:'Anthropic',openai:'OpenAI',google:'Google',deepseek:'DeepSeek',custom:'自定义'},profileNames={balanced:'均衡',coding:'编程',reasoning:'推理',writing:'写作',vision:'视觉理解',image:'图像生成'};
 const statusNames={queued:'排队中',ready:'等待执行',running:'正在协作',completed:'已交付',failed:'需要处理',stopped:'已停止',interrupted:'可继续执行',conflict:'项目版本冲突',waiting:'等待依赖',done:'已完成'};
 let branchFocus=false,playback=null,selectedRunId=null,comparisonData=null,contributionEdit=null,workPolicyVersion=null,modelProbePolling=false,lastModelProbePoll=-Infinity,taskQueuePolling=false,lastTaskQueuePoll=-Infinity,taskRouteEditKey=null,taskRouteTouched=false,routeMessageActive=false,taskRouteProcessDraft=null;
 let index={projects:[],available:[],catalog:{models:[],skills:{},sources:{}}},project=null,session=null,pid=null,sid=null,selectedNode=null,busy=false,polling=false,loaded=false,lastSeq=0,toastTimer,selectedPane='tree',lastHeartbeat=0;
 const model=id=>id==='host'?{id:'host',name:'宿主路由',kind:'host'}:index.catalog.models.find(m=>m.id===id)||{name:short[id]||id};
 const logo=id=>{const icon=icons[id]||({anthropic:'claude-color.svg',openai:'openai.svg',google:'gemini-color.svg',deepseek:'deepseek-color.svg'})[model(id).family];return icon?`<img class="logo" src="/icons/${icon}" alt="${esc(model(id).name)}">`:`<span class="logo model-placeholder" aria-label="${esc(model(id).name)}">◇</span>`;};
 const time=t=>t?new Date(t).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'';
 const date=t=>t?new Date(t).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'}):'';
 const latestRun=()=>session?.runs.at(-1);
 const current=()=>session?.runs.find(r=>r.id===selectedRunId)||latestRun();
 const historicalRun=()=>!!selectedRunId&&selectedRunId!==(playback?.savedSession||session)?.runs.at(-1)?.id;
 const textValue=v=>typeof v==='string'?v:JSON.stringify(v??'');
 const roleDefinition=(id,r=current())=>(r?r.projectSnapshot?.teamConfig:project?.teamConfig)?.customRoles?.[id];
 const roleTitle=(id,r=current())=>roleDefinition(id,r)?.title||roleNames[id]||(r?r.teamSnapshot?.roles||[]:project?.team?.roles||[]).find(role=>(role.roleId||role.id)===id)?.title||(String(id||'').startsWith('reviewer-')?'独立审查 · '+model(String(id).slice(9)).name:id)||'待分派角色';
 const phaseName=n=>n?.repairReview?(n.recheckReason==='review_disagreement'?'同产物分歧复核':'同产物证据复审'):n?.discussionKind==='reflection_prompt'?'主模型深层追问':n?.discussionKind==='reflection'?'深层问题回应':n?.repairRound?'定向修复':String(n?.id||'').startsWith('clarify-')?'定向确认':phases[n?.phase]||n?.phase||'执行';
 const currentTeamConfig=()=>playback||historicalRun()?(current()?.projectSnapshot?.teamConfig||{}):(project?.teamConfig||{});
 const reviewStates=r=>{if(!playback)return Object.values(r?.moduleReviews||{});return (r?.plan?.modules||[]).filter(m=>m.reviewPolicy?.required).map(m=>({completedRounds:Math.max(0,...(session?.events||[]).filter(e=>e.runId===r.id&&e.type==='round_completed'&&e.moduleId===m.id).map(e=>e.round||0)),status:r.nodes.some(n=>n.id==='decision-'+m.id&&n.status==='done')?'decided':'running'}));};
 const reviewProgress=r=>reviewStates(r).map(v=>Number(v.completedRounds)||0);
 const query=extra=>new URLSearchParams({project:pid||'',session:sid||'',...extra}).toString();
 async function api(route='',body){const response=await fetch('/api/studio'+route,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Lancer-Token':token}:{},body:body?JSON.stringify(body):undefined});const result=await response.json();if(!response.ok)throw Error(result.error||'操作未完成');return result;}
 function toast(text){$('toast').textContent=text;$('toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('show'),4500);}
 function error(e){toast(e.message||String(e));}
 async function guarded(fn){if(busy)return;if(playback)stopPlayback();busy=true;renderControls();try{await fn();}catch(e){error(e);}finally{busy=false;renderControls();}}
 function locationState(){history.replaceState(null,'','/studio'+(pid?'?'+query(selectedRunId?{run:selectedRunId}:{}):''));localStorage.setItem('lancer-studio-selection',JSON.stringify({project:pid,session:sid,run:selectedRunId}));}
 function acceptSession(next){const previous=latestRun()?.id;session=next;if(selectedRunId&&!session?.runs.some(r=>r.id===selectedRunId)||previous&&previous!==latestRun()?.id){selectedRunId=null;selectedNode=null;}}
 function renderRunSelector(){const runs=(playback?.savedSession||session)?.runs||[];$('runSelectorControl').hidden=runs.length<2;$('runSelector').innerHTML=runs.map((r,i)=>`<option value="${esc(r.id)}">第 ${i+1} 轮 · ${esc(statusNames[r.status]||r.status)}${i===runs.length-1?' · 最新':''}</option>`).join('');$('runSelector').value=current()?.id||'';$('runSelector').disabled=busy;}
 function chooseRun(id){if(playback)stopPlayback();selectedRunId=session?.runs.some(r=>r.id===id)&&id!==latestRun()?.id?id:null;selectedNode=null;LancerMotion.clear();locationState();render();}
 async function refreshIndex(){index=await api();renderSidebar();renderConnectionSummary();renderNativeConnection();}
 function renderConnectionSummary(){const count=(index.available||[]).filter(m=>!['codex','native_codex'].includes(m)).length,native=index.native?.available&&index.native.enabled,configured=(index.providers||[]).filter(p=>p.connected).length;$('connectionText').textContent=[count?`${count} 个型号${Array.isArray(index.modelStates)?'可派工':'已配置'}`:configured?`${configured} 个服务已配置`:'',native?'原生已接入':''].filter(Boolean).join(' · ')||'连接模型';$('connectButton').classList.toggle('connected',count>0||!!native);}
 const readiness=id=>index.modelStates?.find(row=>row.id===id);
 const probeRunning=state=>state?.probeState==='running';
 const eligibleModel=id=>id==='native_codex'?!!(index.native?.available&&index.native.enabled):Array.isArray(index.modelStates)?readiness(id)?.eligible===true&&(index.available||[]).includes(id):(index.available||[]).includes(id);
 const selectableModel=m=>!Array.isArray(index.modelStates)||eligibleModel(m.id);
 const nativeConnectedScope=()=> '文件与终端'+(index.native?.available&&index.native.enabled&&index.native.capabilities?.includes('web_search')?' · 联网搜索':'');
 function renderNativeConnection(){const n=index.native||{},active=n.available&&n.enabled;$('nativeStatus').textContent=active?'已接入调度':n.available?'已就绪 · 尚未启用':'暂不可用';$('nativeStatus').classList.toggle('connected',!!active);$('nativeScopeNotice').textContent='使用本机已有的 Codex 登录，独立于下方第三方接口密钥。当前调度能力为'+nativeConnectedScope()+'。';$('nativeInfo').textContent=n.available?['沿用本机 Codex 登录',n.model?'当前模型：'+n.model:'本机默认模型',nativeConnectedScope()].join(' · '):(n.reason||'尚未检测到原生 Codex 连接。');$('nativeToggle').textContent=n.enabled?'停用原生执行':'接入调度';$('nativeToggle').disabled=busy||!n.available&&!n.enabled;$('nativeToggle').setAttribute('aria-pressed',String(!!n.enabled));const c=n.collection||{},target=$('nativeCollection');target.hidden=!c.configured&&!c.error;target.classList.toggle('native-warning',!!c.error);target.textContent=c.error?'自动归集配置有误：'+c.error:c.configured?'原生记录归属：'+(c.name||'原生 Codex 项目')+'\n成果目录：'+(c.root||'')+'\nCodex 侧边栏会话分组目前需手动移动。':'需先配置会话归集项目。';}
 const isImageModel=m=>m.id==='image'||m.kind==='image'||m.profile==='image'||m.protocol==='image';
 const canCoordinate=m=>m.auto!==false&&m.id!=='native_codex'&&m.kind!=='executor'&&!isImageModel(m);
 function nextTeamSelection(){
  if(teamDraft)return teamDraft;
  if(session?.teamSelection)return session.teamSelection;
  const candidates=index.catalog.models.filter(m=>canCoordinate(m)&&selectableModel(m)),connected=candidates.filter(m=>eligibleModel(m.id));
  const coordinatorModel=connected.find(m=>m.id==='opus')?.id||connected[0]?.id||candidates.find(m=>m.id==='opus')?.id||candidates[0]?.id||'';
  return {coordinatorModel,teamModels:[...new Set([coordinatorModel,...index.catalog.models.filter(m=>m.auto!==false&&eligibleModel(m.id)).map(m=>m.id)].filter(Boolean))]};
 }
 const coordinatorId=(r=current())=>r?(r.coordinatorModel||r.nodes.find(n=>n.phase==='draft'&&n.agent!=='host')?.agent||'opus'):nextTeamSelection().coordinatorModel;
 const coordinatorName=(r=current())=>model(coordinatorId(r)).name||'主模型';
 const teamLocked=()=>!!playback||historicalRun()||['queued','running','failed','stopped','interrupted'].includes(session?.status);
 function displayedTeam(){const r=current();return r?{coordinatorModel:coordinatorId(r),teamModels:r.teamModels||[...new Set(r.nodes.map(n=>n.agent))]}:nextTeamSelection();}
 function renderTeamSelection(){
  const r=current(),selected=displayedTeam(),next=nextTeamSelection(),names=selected.teamModels.map(id=>model(id).name).join('、');
  $('teamSelectionSummary').textContent=(r?(r.orchestrationVersion?'本轮已固定':'本轮记录'):'下轮工作小组')+' · 主模型 '+(model(selected.coordinatorModel).name||'待选择')+' · '+selected.teamModels.length+' 个成员';
  $('teamSelectionSummary').title=names;
  const changed=r&&!playback&&!historicalRun()&&(selected.coordinatorModel!==next.coordinatorModel||JSON.stringify([...selected.teamModels].sort())!==JSON.stringify([...next.teamModels].sort()));
  $('nextTeamSummary').hidden=!changed;$('nextTeamSummary').textContent=changed?'下轮配置：'+model(next.coordinatorModel).name+' 主导 · '+next.teamModels.length+' 个成员':'';
  $('coordinatorLabel').textContent='主模型 · '+coordinatorName();$('teamSetupButton').disabled=busy||!index.catalog.models.some(m=>canCoordinate(m)&&(teamLocked()||selectableModel(m)));
 }
 function renderTeamSetup(){
  const selection=teamLocked()?displayedTeam():(teamEdit||nextTeamSelection()),candidates=index.catalog.models.filter(m=>canCoordinate(m)&&(teamLocked()||selectableModel(m))),chosen=model(selection.coordinatorModel);
  const families=[...new Set(candidates.map(m=>m.family||'custom'))];
  $('coordinatorFamily').innerHTML=families.map(f=>'<option value="'+esc(f)+'">'+esc(familyNames[f]||f)+'</option>').join('');
  $('coordinatorFamily').value=families.includes(chosen.family)?chosen.family:families[0]||'';renderCoordinatorOptions(selection.coordinatorModel);
  const grouped=new Map();for(const m of index.catalog.models){const f=m.family||'custom';if(!grouped.has(f))grouped.set(f,[]);grouped.get(f).push(m);}
  $('teamMembers').innerHTML=[...grouped].map(([family,models])=>'<fieldset class="team-member-group"><legend>'+esc(familyNames[family]||family)+'</legend>'+models.map(m=>{
   const main=m.id===$('coordinatorModel').value,connected=eligibleModel(m.id),provider=(index.providers||[]).find(p=>p.id===m.providerId);
   const state=m.id==='native_codex'?(connected?'已接入 · '+nativeConnectedScope():'未接入 · 文件与终端'):Array.isArray(index.modelStates)?connected?'可派工 · API':readiness(m.id)?.reason||'当前不可派工':connected?'已配置 · API':'未连接 · 可先配置';
   return '<label class="team-member"><input type="checkbox" data-team-model="'+esc(m.id)+'" value="'+esc(m.id)+'"'+((selection.teamModels.includes(m.id)||main)&&(teamLocked()||selectableModel(m))?' checked':'')+(main||teamLocked()||m.auto===false||!selectableModel(m)?' disabled':'')+'>'+logo(m.id)+'<span><b>'+esc(m.name)+(main?' · 主模型':'')+'</b><small>'+esc(provider?.name||(m.id==='native_codex'?'本机登录':'连接待配置'))+' · '+esc(state)+'</small><small>'+esc(m.auto===false?'能力未验证，暂不参与自动分配':m.profile?'用户能力先验 · '+(profileNames[m.profile]||m.profile):(m.strengths||[]).join(' / '))+'</small></span></label>';
  }).join('')+'</fieldset>').join('');
  const locked=teamLocked();$('coordinatorFamily').disabled=locked;$('coordinatorModel').disabled=locked;$('saveTeamSelection').disabled=locked;
  $('teamSetupNotice').textContent=locked?'当前轮次小组已固定。继续本轮与回放均保留原配置；追加任务并保存后可设置新的小组。':'主模型只从选定成员中按能力分派。简单任务不讨论；复杂任务按岗位讨论 1–3 轮。代码产物另做独立审查，与讨论分别记录。'+(Array.isArray(index.modelStates)?' 当前不可派工型号不可加入新小组；目录和调用状态请在连接设置查看。':'');
  $('teamSetupFeedback').textContent='';renderToolCatalog();
 }
 function renderCoordinatorOptions(preferred){
  const candidates=index.catalog.models.filter(m=>canCoordinate(m)&&(teamLocked()||selectableModel(m))&&(m.family||'custom')===$('coordinatorFamily').value);
  $('coordinatorModel').innerHTML=candidates.map(m=>'<option value="'+esc(m.id)+'">'+esc(m.name)+' · '+esc((index.providers||[]).find(p=>p.id===m.providerId)?.name||'模型连接')+(index.available.includes(m.id)?'':' · 未连接')+'</option>').join('');
  $('coordinatorModel').value=candidates.some(m=>m.id===preferred)?preferred:candidates[0]?.id||'';
 }
 function readTeamSelection(){const selected=$('coordinatorModel').value,coordinatorModel=Array.isArray(index.modelStates)&&!eligibleModel(selected)?'':selected;return {coordinatorModel,teamModels:[...new Set([coordinatorModel,...[...$('teamMembers').querySelectorAll('[data-team-model]:checked')].map(input=>input.value)].filter(id=>id&&(!Array.isArray(index.modelStates)||eligibleModel(id))))]};}
 function changeCoordinator(){teamEdit=readTeamSelection();renderTeamSetup();}
 function renderToolCatalog(){
  $('toolCatalog').innerHTML=(index.tools||[]).map(t=>'<div class="tool-capability"><b>'+esc(t.name)+'</b><small>'+esc(t.statusLabel||({available:'已接入',not_connected:'未连接',planned:'尚未适配'})[t.status]||t.status)+'</small><p>'+esc((t.capabilities||[]).map(c=>({files:'文件读写',terminal:'终端命令',web_search:'联网搜索',plugins:'插件工具',browser:'网页操作'})[c]||c).join(' / '))+'</p><p>'+esc(t.reason||'')+'</p></div>').join('')||'<p class="muted">暂未返回工具接入状态。模型 API 连接不代表已获得浏览器或电脑操作能力。</p>';
 }
 function openMcpConnections(){
  if(!$('mcpConnectionSection'))$('connectDialog').insertAdjacentHTML('beforeend','<details id="mcpConnectionSection" class="model-registration"><summary>MCP 工具连接</summary><p class="muted">给 API 模型接入工具，不新增模型。这里只保存启动配置，不保存 API 密钥或环境变量。检测会启动对应服务并读取工具目录。</p><p id="mcpDependency" role="status"></p><div id="mcpServers"></div><form id="mcpForm"><div class="model-form-grid"><label>服务名称<input id="mcpName" required maxlength="80"></label><label>启动命令<input id="mcpCommand" required placeholder="例如：npx 或可执行文件路径"></label></div><label>参数 · 每行一个<textarea id="mcpArgs" rows="4" placeholder="每行是一个完整参数，参数内可含空格"></textarea></label><label class="team-member"><input id="mcpBrowser" type="checkbox">用于独立网页操作</label><p class="muted">启动命令与参数分别填写，不按 shell 命令解析。</p><div class="dialog-foot"><button id="mcpPreset" type="button">填入独立浏览器预设</button><button class="primary">保存工具连接</button></div></form><p id="mcpFeedback" role="status"></p></details>');
  $('mcpForm').onsubmit=e=>{e.preventDefault();guarded(saveMcpConnection);};
  $('mcpPreset').onclick=fillMcpPreset;renderMcpConnections();
 }
 function renderMcpConnections(){
  const m=index.mcp||{};$('mcpDependency').textContent=m.detected?'MCP 运行依赖已就绪':m.reason||'尚未安装 MCP 可选依赖。';
  $('mcpServers').innerHTML=(m.servers||[]).map(s=>'<section class="tool-capability"><b>'+esc(s.name)+'</b><small>'+esc(({configured:'已配置 · 未探测',ready:'已发现 '+s.tools.length+' 个工具',error:'探测失败'})[s.status]||s.status)+(s.browser?' · 网页操作':'')+'</small><p class="mono">'+esc([s.command,...s.args].join(' '))+'</p>'+(s.error?'<p class="native-warning">'+esc(s.error)+'</p>':'')+(s.lastProbedAt?'<p class="muted">最近探测：'+esc(s.lastProbedAt)+'</p>':'')+(s.tools.length?'<details><summary>已发现的工具</summary>'+s.tools.map(t=>'<p><b>'+esc(t.name)+'</b><br>'+esc(t.description||'')+'</p>').join('')+'</details>':'')+'<div class="dialog-foot"><button type="button" data-mcp-remove="'+esc(s.id)+'">移除</button><button type="button" data-mcp-probe="'+esc(s.id)+'"'+(!m.detected?' disabled':'')+'>检测并列出工具</button></div></section>').join('')||'<p class="muted">未配置 MCP 工具连接。</p>';
  $('mcpPreset').disabled=!m.browserPreset;
 }
 function fillMcpPreset(){const p=index.mcp.browserPreset;$('mcpName').value=p.name;$('mcpCommand').value=p.command;$('mcpArgs').value=p.args.join('\n');$('mcpBrowser').checked=true;$('mcpFeedback').textContent='已填入预设，尚未保存或启动浏览器。';}
 function mcpConnectionPayload(){return {name:$('mcpName').value.trim(),command:$('mcpCommand').value.trim(),args:$('mcpArgs').value.split(/\r?\n/).filter(value=>value.length),browser:$('mcpBrowser').checked};}
 async function saveMcpConnection(){
  try{index=await api('/mcp/save',mcpConnectionPayload());renderMcpConnections();renderToolCatalog();$('mcpFeedback').textContent='连接配置已保存；点击检测可读取实际工具目录。';}
  catch(e){$('mcpFeedback').textContent=e.message;throw e;}
 }
 async function manageMcpConnection(action,id){
  $('mcpFeedback').textContent=action==='probe'?'正在启动服务并探测工具…':'正在移除连接…';
  try{index=await api('/mcp/'+action,{id});renderMcpConnections();renderToolCatalog();const server=index.mcp.servers.find(s=>s.id===id);$('mcpFeedback').textContent=action==='remove'?'工具连接已移除。':server.status==='ready'?'已读取实际工具目录；这不代表所有工具都经过运行验收。':'探测失败：'+(server.error||'请核对启动配置与依赖。');}
  catch(e){$('mcpFeedback').textContent=e.message;throw e;}
 }
 function renderHostAcceptance(){
  const registry=index.hostAcceptance||{},checks=registry.checks||[];
  $('hostAcceptanceSupport').textContent=registry.supported===true?'登记受信任的本机脚本副本，项目明确选择后才在交付验收时运行。':'当前服务未接入宿主验收执行；已有登记和项目选择仍可查看。';
  $('hostAcceptanceRegister').disabled=registry.supported!==true;
  $('hostAcceptanceRegistry').innerHTML=checks.map(check=>'<section class="tool-capability"><b>'+esc(check.name||check.id)+'</b><small>本机已登记 · '+(check.criteria||[]).length+' 项标准</small>'+(check.scriptPath?'<p class="mono">'+esc(check.scriptPath)+'</p>':'')+'<p>验收文件：'+(check.files||[]).map(path=>'<code>'+esc(path)+'</code>').join('、')+'</p><p class="mono">SHA-256：'+esc(check.scriptSha256||'未返回指纹')+'</p><details><summary>固定验收标准 · '+esc(check.timeoutSeconds??30)+' 秒超时</summary>'+(check.criteria||[]).map(criterion=>'<p><code>'+esc(criterion.id)+'</code> · '+esc(criterion.description)+'</p>').join('')+'</details><div class="dialog-foot"><button type="button" data-host-acceptance-remove="'+esc(check.id)+'">移除本机登记</button></div></section>').join('')||'<p class="muted">尚未登记宿主验收脚本；不会自动选择或运行任何检查器。</p>';
 }
 function hostAcceptancePayload(){
  const lines=id=>$(id).value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
  const id=$('hostAcceptanceId').value.trim(),name=$('hostAcceptanceName').value.trim(),scriptPath=$('hostAcceptanceScript').value.trim(),files=lines('hostAcceptanceFiles');
  if(id&&!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(id))throw Error('原检查编号须与历史记录一致，使用 1–80 位字母、数字、下划线、点或短横线，并以字母或数字开头。');
  if(!name||!scriptPath)throw Error('请填写验收名称与本机 Python 脚本路径。');
  if(!files.length||files.some(path=>path.startsWith('/')||path.includes('\\')||path.split('/').some(part=>!part||part==='.'||part==='..')))throw Error('请逐行填写精确的交付相对文件路径，不使用目录或路径跳转。');
  if(new Set(files).size!==files.length)throw Error('验收文件路径不能重复。');
  const criteria=lines('hostAcceptanceCriteria').map(line=>{const split=line.indexOf('|');if(split<1||!line.slice(split+1).trim())throw Error('每项标准请按“标准ID | 验收说明”填写。');return {id:line.slice(0,split).trim(),description:line.slice(split+1).trim()};});
  if(!criteria.length||criteria.some(criterion=>!criterion.id)||new Set(criteria.map(criterion=>criterion.id)).size!==criteria.length)throw Error('请填写至少一项标准，标准 ID 不能为空或重复。');
  const timeoutSeconds=$('hostAcceptanceTimeout').value.trim()===''?30:Number($('hostAcceptanceTimeout').value);
  if(!Number.isInteger(timeoutSeconds)||timeoutSeconds<1||timeoutSeconds>300)throw Error('超时须为 1–300 的正整数秒。');
  return {...(id?{id}:{}),name,scriptPath,files,criteria,timeoutSeconds};
 }
 async function saveHostAcceptance(){
  try{if(index.hostAcceptance?.supported!==true)throw Error('当前服务未接入宿主验收执行。');index=await api('/host-acceptance/register',hostAcceptancePayload());renderHostAcceptance();renderProjectHostAcceptance();renderRoleWorker();$('hostAcceptanceFeedback').textContent='已保存本机授权，尚未执行脚本。请在项目资料中明确选择用于后续任务的检查器。';}
  catch(e){$('hostAcceptanceFeedback').textContent=e.message;throw e;}
 }
 async function removeHostAcceptance(id){
  try{index=await api('/host-acceptance/remove',{id});renderHostAcceptance();renderProjectHostAcceptance();renderRoleWorker();$('hostAcceptanceFeedback').textContent='已移除本机登记。项目中的原选择会显示未接入；历史验收记录保留。';}
  catch(e){$('hostAcceptanceFeedback').textContent=e.message;throw e;}
 }
 function renderProjectHostAcceptance(reset=false){
  const projectId=project?.id||pid,registry=index.hostAcceptance||{},checks=registry.checks||[];
  if(reset||hostAcceptanceProjectId!==projectId||!Array.isArray(hostAcceptanceDraft)){hostAcceptanceDraft=[...(project?.hostAcceptanceIds||[])];hostAcceptanceProjectId=projectId;}
  const ids=[...new Set([...checks.map(check=>check.id),...hostAcceptanceDraft])];
  $('projectHostAcceptance').innerHTML='<h3>前置宿主验收</h3><p class="muted">选择本项目交付前需要运行的检查器。新任务冻结脚本指纹与标准，已有运行不随这里的修改而变化。</p><fieldset class="team-member-group"><legend>本项目使用的检查器</legend>'+ids.map(id=>{const check=checks.find(item=>item.id===id),selected=hostAcceptanceDraft.includes(id),available=!!check&&registry.supported===true;return '<label class="team-member"><input type="checkbox" data-project-host-acceptance="'+esc(id)+'"'+(selected?' checked':'')+(!available&&!selected?' disabled':'')+'><span><b>'+esc(check?.name||id)+'</b><small>'+(available?(check.criteria||[]).length+' 项固定标准 · '+(check.files||[]).map(esc).join('、'):'未接入 · 可取消原选择')+'</small>'+(!available&&selected?'<small>原检查编号：<code>'+esc(id)+'</code></small>':'')+'</span></label>';}).join('')+(ids.length?'':'<p class="muted">尚未选择检查器。可在“连接模型 → 宿主验收脚本”中显式登记。</p>')+'</fieldset><div class="settings-actions"><button type="button" id="saveProjectHostAcceptance"'+(!project||!index.hostAcceptance?' disabled':'')+'>保存本项目验收选择</button></div><p id="projectHostAcceptanceFeedback" role="status"></p>';
  $('projectHostAcceptance').onchange=e=>{const id=e.target.dataset.projectHostAcceptance;if(!id)return;const check=checks.find(item=>item.id===id);if(e.target.checked&&(!check||registry.supported!==true))return;hostAcceptanceDraft=e.target.checked?[...new Set([...hostAcceptanceDraft,id])]:hostAcceptanceDraft.filter(value=>value!==id);renderProjectHostAcceptance();};
  $('saveProjectHostAcceptance').onclick=()=>guarded(saveProjectHostAcceptance);
 }
 function projectHostAcceptancePayload(){return {project:project?.id||pid,version:project?.version,checkIds:[...(hostAcceptanceDraft||[])]};}
 async function saveProjectHostAcceptance(){
  try{if(!project||!index.hostAcceptance)throw Error('请先选择项目并确认服务支持宿主验收。');project=await api('/project/host-acceptance',projectHostAcceptancePayload());renderProjectHostAcceptance(true);renderRoleWorker();$('projectHostAcceptanceFeedback').textContent='项目验收选择已保存，将用于后续新任务；历史运行与验收结果保持原样。';renderMemory();renderTeam();}
  catch(e){$('projectHostAcceptanceFeedback').textContent=e.message;throw e;}
 }
 function mcpEvidenceHtml(e){
  if(!e)return '';
  const status={running:'执行中',completed:'已返回',failed:'失败',cancelled:'已取消'};
  return '<details class="native-evidence"><summary>MCP 工具回执 · '+e.tools.length+' 次调用 · '+esc(status[e.status]||e.status)+'</summary><p class="native-caption">来自工具实际返回；模型摘要不作为工具执行结果。</p>'+e.tools.map(t=>'<div class="native-tool-row"><b>'+esc(t.serverId)+' / '+esc(t.name)+'</b><small>'+esc(({completed:'已返回',tool_error:'工具报告错误',protocol_error:'协议调用失败'})[t.status]||t.status)+(t.elapsedMs!=null?' · '+esc((t.elapsedMs/1000).toFixed(2))+' 秒':'')+'</small>'+(t.error?'<p class="native-warning">'+esc(textValue(t.error))+'</p>':'')+'<details><summary>调用参数与实际返回</summary><pre>'+esc(JSON.stringify({arguments:t.arguments,result:t.result},null,2))+'</pre></details>'+(t.evidenceRef?'<p class="native-ref">证据 <code>'+esc(t.evidenceRef)+'</code></p>':'')+'</div>').join('')+(e.receiptRef?'<p class="native-ref">完整回执 <code>'+esc(e.receiptRef)+'</code></p>':'')+'</details>';
 }
 function mcpProgressHtml(n,runId){
  const evidence=!playback||n.status==='done'?(n.mcpEvidence||n.result?.mcpEvidence):null;
  const completed=evidence?mcpEvidenceHtml(evidence):'';
  if(evidence?.status==='completed')return completed;
  const events=(session?.events||[]).filter(e=>e.node===n.id&&(!e.runId||e.runId===runId)&&['mcp_tool_started','mcp_tool_completed','mcp_tool_error'].includes(e.type));
  if(!events.length)return completed;
  return completed+'<details class="native-evidence"><summary>MCP 工具执行记录</summary>'+events.map(e=>'<p>'+esc(e.serverId)+' / '+esc(textValue(e.tool))+' · '+esc(({mcp_tool_started:'开始调用',mcp_tool_completed:'已返回',mcp_tool_error:'调用失败'})[e.type])+(e.evidenceRef?' · <code>'+esc(e.evidenceRef)+'</code>':'')+'</p>').join('')+'</details>';
 }
 function renderWorkPolicy(){
  const policy=index.workPolicy,guide=index.workPolicyGuide;
  workPolicyVersion=policy.version;
  $('policyFirstPrinciples').checked=policy.firstPrinciples;
  $('policyCodingDiscipline').checked=policy.codingDiscipline;
  $('policyDeepReview').checked=policy.deepReview;
  $('policyMultiplier').value=String(policy.deepReviewMultiplier);
  $('policyMultiplier').disabled=!policy.deepReview;
  $('policyDeepQuestion').textContent=guide.deepReviewQuestion;
  $('policyGuide').innerHTML='<h3>第一性原理</h3><p>'+esc(guide.firstPrinciples)+'</p><h3>编程规范</h3><pre>'+esc(guide.codingDiscipline)+'</pre>';
  $('workPolicyFeedback').textContent='当前配置版本 '+policy.version+' · 保存后用于新任务';
 }
 async function saveWorkPolicy(){
  try{
   const result=await api('/work-policy',{version:workPolicyVersion,firstPrinciples:$('policyFirstPrinciples').checked,codingDiscipline:$('policyCodingDiscipline').checked,deepReview:$('policyDeepReview').checked,deepReviewMultiplier:Number($('policyMultiplier').value)});index.workPolicy=result.workPolicy;
   renderWorkPolicy();$('workPolicyFeedback').textContent='已保存。后续新任务使用本配置；当前运行与历史记录保留原规范。';
  }catch(e){$('workPolicyFeedback').textContent=e.message;throw e;}
 }
 function renderProviderControls(){
  const providers=index.providers||[],currentProvider=$('provider').value,registrationProvider=$('registerProvider').value;
  const options=providers.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+(p.connected?' · 已配置':'')+'</option>').join('');
  $('provider').innerHTML=options;$('provider').value=providers.some(p=>p.id===currentProvider)?currentProvider:providers[0]?.id||'';
  $('registerProvider').innerHTML='<option value="">新建连接服务</option>'+options;$('registerProvider').value=registrationProvider;
  $('providerBase').textContent=providers.find(p=>p.id===$('provider').value)?.baseUrl||'';renderRegistrationFields();renderModelReadiness();renderModelPolicyOptions();
 }
 function renderRegistrationFields(){const existing=!!$('registerProvider').value;$('newProviderFields').hidden=existing;$('registerProviderName').required=!existing;$('registerBaseUrl').required=!existing;}
 function providerModels(provider=$('provider').value){return Array.isArray(index.modelStates)?index.modelStates.filter(row=>row.providerId===provider&&row.id!=='native_codex').map(row=>({...model(row.id),id:row.id,protocol:row.protocol,providerId:row.providerId})):index.catalog.models.filter(m=>m.providerId===provider&&m.id!=='native_codex');}
 function renderModelReadiness(){
  const provider=(index.providers||[]).find(p=>p.id===$('provider').value),supported=Array.isArray(index.modelStates);
  $('discoverModels').disabled=busy||!supported||!provider?.connected;
  const rows=providerModels();
  $('modelReadinessList').innerHTML=rows.map(m=>{
   const state=readiness(m.id),last=state?.lastCall;
   if(!state)return '<section class="tool-capability"><b>'+esc(m.name||m.id)+'</b><p>配置：'+(provider?.connected?'已保存密钥':'未配置密钥')+' · 目录：未知 · 调用：未返回记录</p></section>';
   const catalogLabel={unknown:'未探测',listed:'已列出',not_listed:'未列出',error:'探测失败'}[state.catalogState]||'未知';
   const callLabel=probeRunning(state)?'验证中':state.probeState==='superseded'?'配置已变化，需重新验证':state.probeState==='cancelled'?'验证已取消':{unverified:'未验证',succeeded:'最近调用成功',failed:'最近调用失败'}[state.callState]||'未知';
   return '<section class="tool-capability"><b>'+esc(m.name||m.id)+'</b><p class="mono">请求型号：'+esc(state.model||'未返回')+'</p><p>配置：'+(state.configured?'已保存密钥':'未配置密钥')+' · 目录：'+esc(catalogLabel)+' · 调用：'+esc(callLabel)+'</p><p>'+esc(state.eligible?'当前可派工'+(state.reason?' · '+state.reason:''):state.reason||'当前不可派工')+'</p>'+(state.catalogCheckedAt?'<p class="muted">目录探测时间：'+esc(state.catalogCheckedAt)+'</p>':'')+(last?'<p class="muted">最近调用记录：'+esc(last.checkedAt||'时间未记录')+'<br>实际返回型号：'+esc(last.returnedModel||'未知')+(last.errorClass?'<br>错误类别：'+esc(last.errorClass):'')+(last.httpStatus!=null?' · HTTP '+esc(last.httpStatus):'')+(typeof last.responseComplete==='boolean'?'<br>响应完整：'+(last.responseComplete?'是':'否'):'')+(typeof last.jsonValidated==='boolean'?' · JSON校验：'+(last.jsonValidated?'通过':'未通过'):'')+'</p>':'')+'<div class="dialog-foot"><button type="button" data-model-policy="'+esc(m.id)+'">请求参数</button>'+(isImageModel(m)?'<small>图像型号不提供验证调用</small>':'<button type="button" data-model-probe="'+esc(m.id)+'"'+(!state.configured||busy||probeRunning(state)?' disabled':'')+'>验证调用（计费）</button>')+(probeRunning(state)?'<button type="button" data-model-probe-stop="'+esc(m.id)+'"'+(busy?' disabled':'')+'>停止验证</button>':'')+'</div></section>';
  }).join('')||'<p class="muted">当前服务尚未登记型号。</p>';
  if(!supported)$('modelReadinessFeedback').textContent='当前服务未返回型号状态；既有连接记录仍可查看。';
 }
 function renderModelPolicyOptions(preferred){
  const rows=providerModels(),selected=preferred||$('modelPolicyModel').value;
  $('modelPolicyModel').innerHTML=rows.map(m=>'<option value="'+esc(m.id)+'">'+esc(m.name||m.id)+'</option>').join('');
  $('modelPolicyModel').value=rows.some(m=>m.id===selected)?selected:rows[0]?.id||'';renderModelPolicyEditor();
 }
 function renderModelPolicyEditor(){
  const id=$('modelPolicyModel').value,state=readiness(id),policy=state?.requestPolicy||{},protocol=state?.protocol||model(id).protocol;
  modelPolicyEdit=state?{modelId:id,version:state.policyVersion,protocol}:null;
  const image=protocol==='image',anthropic=protocol==='anthropic';
  $('modelPolicyProtocol').textContent=state?'协议：'+protocol+' · 参数版本 '+state.policyVersion+'。是否支持具体强度仍以该服务实际返回为准。':'当前型号未返回可编辑参数。';
  $('modelPolicyOutput').value=String(policy.maxOutputTokens??'');$('modelPolicyTimeout').value=String(policy.timeoutSeconds??'');
  const efforts=anthropic?['default','low','medium','high','xhigh','max']:['default','none','minimal','low','medium','high','xhigh','max'];
  $('modelPolicyReasoning').innerHTML=efforts.map(value=>'<option value="'+value+'">'+(value==='default'?'沿用服务默认':value)+'</option>').join('');
  $('modelPolicyReasoning').value=efforts.includes(policy.reasoningEffort)?policy.reasoningEffort:'default';
  $('modelPolicyJson').value=String(!anthropic&&policy.jsonMode===true);
  for(const field of ['Output','Reasoning','Json']){$('modelPolicy'+field+'Label').hidden=image;$('modelPolicy'+field).disabled=image||!state||(field==='Json'&&anthropic);}
  $('modelPolicyTimeout').disabled=!state;$('saveModelPolicy').disabled=!state||!Number.isInteger(state.policyVersion);
  $('modelPolicyFeedback').textContent=anthropic?'Anthropic Messages 不发送 JSON 模式参数。':image?'图像型号仅可设置超时；不会在此自动生成图片。':'保存只影响新轮次，当前运行与续跑保留冻结参数。';
 }
 function modelPolicyPayload(){
  if(!modelPolicyEdit||modelPolicyEdit.modelId!==$('modelPolicyModel').value)throw Error('请先选择有参数记录的型号。');
  const timeout=Number($('modelPolicyTimeout').value);if(!Number.isFinite(timeout)||timeout<1||timeout>3600)throw Error('请求超时须为1–3600秒。');
  const requestPolicy={timeoutSeconds:timeout};
  if(modelPolicyEdit.protocol!=='image'){
   const output=Number($('modelPolicyOutput').value);if(!Number.isInteger(output)||output<1||output>393216)throw Error('最大输出须为1–393216个Token。');
   const effort=$('modelPolicyReasoning').value,allowed=modelPolicyEdit.protocol==='anthropic'?['default','low','medium','high','xhigh','max']:['default','none','minimal','low','medium','high','xhigh','max'];
   if(!allowed.includes(effort))throw Error('思考强度不适用于当前协议。');
   if(!['true','false'].includes($('modelPolicyJson').value))throw Error('请选择启用或停用JSON模式。');
   Object.assign(requestPolicy,{maxOutputTokens:output,reasoningEffort:effort,jsonMode:modelPolicyEdit.protocol==='anthropic'?false:$('modelPolicyJson').value==='true'});
  }
  return {modelId:modelPolicyEdit.modelId,version:modelPolicyEdit.version,requestPolicy};
 }
 async function saveModelPolicy(){try{index=await api('/models/policy',modelPolicyPayload());renderConnectionSummary();renderModelReadiness();renderModelPolicyEditor();$('modelPolicyFeedback').textContent='型号参数已保存；仅影响后续新轮次，当前轮与历史记录保持原参数。';}catch(e){$('modelPolicyFeedback').textContent=e.message;throw e;}}
 async function discoverModelCatalog(provider=$('provider').value){
  $('modelReadinessFeedback').textContent='正在读取服务型号目录，不发起生成请求…';
  try{index=await api('/models/discover',{provider});renderConnectionSummary();renderProviderControls();renderRelay();const errors=(index.modelStates||[]).filter(row=>row.providerId===provider&&row.catalogState==='error');$('modelReadinessFeedback').textContent=errors.length?'目录探测失败：'+(errors[0].reason||'请核对接口与连接'):'目录查询已结束；可见型号不等于已经通过真实调用或能力验证。';return index;}
  catch(e){$('modelReadinessFeedback').textContent='目录探测失败：'+e.message;throw e;}
 }
 async function probeModel(id){
  const state=readiness(id);if(!state?.configured)throw Error('请先保存该服务密钥。');if(state.protocol==='image'||isImageModel({...model(id),protocol:state.protocol}))throw Error('图像型号不提供验证调用。');
  if(probeRunning(state))throw Error('该型号正在验证，请等待结果或停止本次验证。');
  $('modelReadinessFeedback').textContent='正在进行计费验证调用，请等待服务返回；测试不包含项目内容…';
  try{index=await api('/models/probe',{modelId:id});renderConnectionSummary();renderModelReadiness();renderRelay();showProbeFeedback(id);}
  catch(e){$('modelReadinessFeedback').textContent='验证调用未通过：'+e.message;throw e;}
 }
 function showProbeFeedback(id){const result=readiness(id);$('modelReadinessFeedback').textContent=probeRunning(result)?'验证正在后台运行，可停止本次验证；不会重复发起调用。':result?.probeState==='superseded'?'配置已变化，需重新验证。':result?.probeState==='cancelled'?'验证已取消，请查看保留的调用记录。':result?.callState==='succeeded'?'本次验证调用成功，当前服务已记录返回型号与参数指纹；调用成功不等于任务交付通过。':'验证调用未通过：'+(result?.reason||result?.lastCall?.errorClass||'请查看调用记录');}
 async function stopModelProbe(id){
  if(!probeRunning(readiness(id)))throw Error('该型号当前没有正在运行的验证。');
  $('modelReadinessFeedback').textContent='正在请求停止验证…';
  try{index=await api('/models/probe/stop',{modelId:id});renderConnectionSummary();renderModelReadiness();if(probeRunning(readiness(id)))$('modelReadinessFeedback').textContent='已请求停止，等待后台结束并保存记录…';else showProbeFeedback(id);}
  catch(e){$('modelReadinessFeedback').textContent='停止验证未完成：'+e.message;throw e;}
 }
 async function pollModelProbes(now=performance.now()){
  const running=(index.modelStates||[]).filter(probeRunning);
  if(!running.length||modelProbePolling||now-lastModelProbePoll<1500)return false;
  modelProbePolling=true;lastModelProbePoll=now;
  try{index=await api();renderConnectionSummary();renderModelReadiness();const shown=running.find(row=>row.providerId===$('provider').value);if(shown)showProbeFeedback(shown.id);return true;}
  catch(e){$('modelReadinessFeedback').textContent='验证状态暂未更新：'+e.message;return false;}
  finally{modelProbePolling=false;}
 }
 async function saveModelConnection(){
  const provider=$('provider').value;
  try{const saved=await api('/connect',{provider,key:$('apiKey').value});$('apiKey').value='';index=saved?.catalog?saved:await api();renderConnectionSummary();renderProviderControls();renderRelay();$('connectFeedback').textContent='密钥已保存到服务内存；正在读取型号目录，不自动验证调用。';
   try{await discoverModelCatalog(provider);$('connectFeedback').textContent='密钥已保存。目录和调用状态见下方；计费验证需显式点击。';}
   catch(e){$('connectFeedback').textContent='密钥已保存，目录探测失败：'+e.message;}
  }catch(e){$('apiKey').value='';$('connectFeedback').textContent=e.message;throw e;}
 }
 function requestPolicyEvidenceHtml(n,r){
  if(n.agent==='host'||n.executorId==='task_router')return '<p class="muted">宿主规则路由 · 规划模型调用为0；执行和验收另行记录。</p>';
  if(n.agent==='native_codex')return '';
  const reached=(session?.events||[]).some(event=>event.runId===r?.id&&event.node===n.id&&['node_failed','node_completed'].includes(event.type));
  const visible=!playback||n.status==='done'||reached,receipt=visible?(n.receipt||(['failed','stopped','interrupted'].includes(n.status)?n.attempts?.at(-1)?.receipt:null)):null;
  const frozen=r?.modelRequestProfiles?.[n.agent],policy=receipt?.requestPolicy||frozen?.requestPolicy;
  if(!policy)return '<p class="muted">本次请求参数未记录，不能用当前设置补充历史。</p>';
  const recorded=!!receipt?.requestPolicy;
  return '<details class="native-checks"><summary>'+ (recorded?'本次实际请求参数':'本轮冻结请求参数')+'</summary><p class="muted">'+(recorded?'来自本次调用回执；不使用连接设置中的最新参数。':'来自本轮启动快照；尚无可显示的实际请求参数回执。')+'</p><pre>'+esc(JSON.stringify(policy,null,2))+'</pre>'+(recorded&&receipt.effectiveRequestParameters?'<p>实际协议参数</p><pre>'+esc(JSON.stringify(receipt.effectiveRequestParameters,null,2))+'</pre>':'')+(receipt?.requestPolicyFingerprint?'<p class="mono">参数指纹 '+esc(receipt.requestPolicyFingerprint)+'</p>':'')+'</details>';
 }
 const directWorkSkills={development:{coding:.75,reasoning:.25},research:{reasoning:.6,writing:.4},writing:{writing:1},design:{visual:.6,writing:.4},data:{reasoning:.6,coding:.4},general:{reasoning:.5,writing:.5}};
 const directRoleTypes=['architecture','frontend','backend','research','writing','design','data','qa'];
 function routeLocked(){return !!playback||historicalRun()||['queued','running'].includes(session?.status)||(['failed','stopped','interrupted'].includes(session?.status)&&!$('message').value.trim());}
 function renderTaskRouteEditor(reset=false){
  const locked=routeLocked(),run=current(),key=(session?.id||'new')+':'+(locked?'frozen-'+(run?.id||'none'):'edit');
  if(reset||taskRouteEditKey!==key){
   const route=locked?(run?.taskRouting||{mode:'team'}):(session?.taskRouteSelection||{mode:'team'}),contract=route.contract||{};
   $('taskRouteMode').value=route.mode==='direct'?'direct':'team';
   const roles=[...new Set([...directRoleTypes,...(project?.roleDirectory?.roles||[]).map(row=>row.roleId),...Object.keys(project?.teamConfig?.customRoles||{}),contract.roleId].filter(id=>id&&id!=='coordinator'&&id!=='reviewer'&&!id.startsWith('reviewer-')))];
   $('directTaskRole').innerHTML=roles.map(id=>'<option value="'+esc(id)+'">'+esc(roleTitle(id,locked?run:null))+'</option>').join('');
   $('directTaskRole').value=contract.roleId||'backend';$('directTaskWorkType').value=contract.workType||'development';
   $('directTaskCapability').value=contract.requiredCapabilities?.includes('workspace')?'workspace':'text';$('directTaskComplexity').value=String(contract.complexity||2);
   $('directTaskScope').value=(contract.scope||[]).join('\n');$('directTaskAcceptance').value=(contract.acceptance||[]).join('\n');
   $('directTaskReview').checked=contract.workType?contract.workType==='development'||contract.reviewRequirements?.mode==='independent':true;
   taskRouteProcessDraft=contract.processRequirements?JSON.parse(JSON.stringify(contract.processRequirements)):null;
   taskRouteEditKey=key;taskRouteTouched=false;
  }
  const direct=$('taskRouteMode').value==='direct';$('directTaskContract').hidden=!direct;
  for(const id of ['taskRouteMode','directTaskRole','directTaskWorkType','directTaskCapability','directTaskComplexity','directTaskScope','directTaskAcceptance','directTaskReview'])$(id).disabled=busy||locked||(id==='directTaskReview'&&$('directTaskWorkType').value==='development');
  $('taskRouteStatus').textContent=(locked?'本轮已固定：':'下次启动：')+(direct?'明确单Worker任务':'小组规划');
  const process=taskRouteProcessDraft;
  $('directTaskProcessSummary').textContent=process?'本任务额外必做：'+(process.initialFailure?'先复现 '+process.initialFailure.checkId+' 的 '+process.initialFailure.criterionIds.join('、')+' 失败；':'')+(process.requireExecutionChild?'执行子任务、完整检查、接纳及父重验。':''):'本任务未增加额外流程要求；仍会继承岗位已配置的必做步骤。';
  $('directTaskClearProcess').hidden=!process;$('directTaskClearProcess').disabled=busy||locked;

  if(locked)$('taskRouteFeedback').textContent='排队、执行和继续本轮均保留冻结路线与合同；追加新要求后可重新选择。';
 }
 function taskRoutePayload(){
  if(historicalRun()&&$('message').value.trim())return {mode:'team'};
  if($('taskRouteMode').value!=='direct')return {mode:'team'};
  const scope=$('directTaskScope').value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean),acceptance=$('directTaskAcceptance').value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
  const role=$('directTaskRole').value,workType=$('directTaskWorkType').value,complexity=Number($('directTaskComplexity').value);
  if(!role||!scope.length||!acceptance.length)throw Error('独立任务须填写岗位、交付范围和验收标准；请补齐或明确改选小组规划。');
  if(scope.length>12||acceptance.length>12||new Set(scope).size!==scope.length||new Set(acceptance).size!==acceptance.length)throw Error('交付范围与验收标准各限1–12条，不能重复。');
  if(![1,2].includes(complexity)||!directWorkSkills[workType])throw Error('此入口仅用于复杂度1–2的明确独立任务。');
  const contract={roleId:role,workType,scope,acceptance,skills:{...directWorkSkills[workType]},requiredCapabilities:$('directTaskCapability').value==='workspace'?['text','workspace']:['text'],complexity};
  const base=directRoleTypes.includes(role)?role:project?.teamConfig?.customRoles?.[role]?.baseRoleType;if(base)contract.roleType=base;
  if(workType==='development'||$('directTaskReview').checked)contract.reviewRequirements={mode:'independent',criterionIds:acceptance.map((_,i)=>'direct-task-c'+(i+1)),reason:'用户明确独立任务保留对应产物复核要求'};
  if(taskRouteProcessDraft){if(!contract.requiredCapabilities.includes('workspace'))throw Error('本任务必做过程要求使用Worker文件与固定脚本；请保留能力或明确清除额外要求。');contract.processRequirements=JSON.parse(JSON.stringify(taskRouteProcessDraft));}
  return {mode:'direct',contract};
 }
 async function persistTaskRoute(route){
  if(routeLocked())throw Error('当前轮次路由已固定，不能修改。');
  try{acceptSession(await api('/session/route',{project:project?.id||pid,session:session?.id||sid,version:session.version,taskRoute:route}));renderTaskRouteEditor(true);$('taskRouteFeedback').textContent='任务路线与单模块合同已保存，启动时冻结；不会在保存时调用模型。';}
  catch(e){$('taskRouteFeedback').textContent=e.message;throw e;}
 }
 function taskMessageChanged(){
  const entered=!!$('message').value.trim();
  if(entered&&!routeMessageActive&&session?.taskRouteSelection&&!taskRouteTouched){
   taskRouteEditKey=null;const saved=session.taskRouteSelection;session.taskRouteSelection={mode:'team'};renderTaskRouteEditor(true);session.taskRouteSelection=saved;
   $('taskRouteFeedback').textContent='追加新要求默认使用小组规划；若仍是明确独立任务，请重新选择范围与标准。';
  }
  routeMessageActive=entered;renderControls();
  if(entered&&historicalRun())$('taskRouteFeedback').textContent='新要求默认走小组规划；如需明确独立任务，请先切回最新轮次并填写新合同，历史范围不会自动沿用。';
 }
 function renderTaskQueue(){
  const target=$('taskDispatchStatus'),run=current(),queue=index.taskQueue,capacity=index.executionCapacity;
  if(playback||historicalRun()||(!queue&&!capacity&&!run?.admissionVersion)){target.hidden=true;return;}
  target.hidden=false;
  const row=(queue?.jobs||[]).find(job=>job.runId===run?.id&&job.sessionId===session?.id),queued=run?.status==='queued'||session?.status==='queued',attempt=run?.executionAttempts?.at(-1);
  const wait=Number.isFinite(row?.queueWaitMs)?row.queueWaitMs:run?.admissionVersion===1?run.queueWaitMs:attempt?.queueWaitMs,queuedAt=row?.queuedAt||attempt?.queuedAt||run?.queuedAt;
  $('taskQueueSummary').textContent=queued?'本次执行已排队 · 等待调度':queue?.closed?'任务队列已停止接纳':'任务调度与执行资源';
  const liveWait=queuedAt?Date.now()-Date.parse(queuedAt):NaN,shownWait=Number.isFinite(wait)?wait:queued&&Number.isFinite(liveWait)&&liveWait>=0?liveWait:null;
  let html='<p>'+(queued?'正在等待任务调度，尚未进行本次模型执行。':'')+(row?.waitingFor?' '+esc({project:'等待同项目任务完成。',capacity:'等待任务执行位。',admission:'等待调度接纳确认。'}[row.waitingFor]||'等待调度。'):'')+(queued&&row?.position!=null?' 当前顺位 '+esc(row.position)+'（随调度变化，不是完成时间）。':'')+(Number.isFinite(shownWait)?(queued?' 当前已等待约 ':' 已记录最近一次排队等待 ')+esc((shownWait/1000).toFixed(2))+' 秒。':queuedAt?' 入队时间 '+esc(queuedAt)+'；等待耗时尚未记录。':'')+'</p>';
  if(queue)html+='<p>当前任务：执行中 '+esc(queue.active??'未记录')+' / '+esc(queue.activeLimit??'未记录')+' · 排队 '+esc(queue.queued??'未记录')+(queue.activePerProjectLimit!=null?' · 每项目同时 '+esc(queue.activePerProjectLimit)+' 个':'')+'</p>';
  if(capacity){html+='<p>当前请求槽：'+esc(capacity.totalActive??'未记录')+' / '+esc(capacity.totalLimit??'未记录')+'</p>';for(const [key,label] of [['control','规划/复核请求'],['worker','执行请求']]){const pool=capacity.pools?.[key];if(pool)html+='<p>'+label+'：'+esc(pool.active??'未记录')+' / '+esc(pool.limit??'未记录')+' · 等待资源 '+esc(pool.waiting??'未记录')+'</p>';}html+='<p class="muted">等待资源的是请求数，与排队任务数分别统计；不表示任务已完成。</p>';}
  $('taskQueueContent').innerHTML=html;
 }
 async function pollTaskQueue(now=performance.now()){
  if(taskQueuePolling||now-lastTaskQueuePoll<1500||!index.taskQueue||!(index.taskQueue.active||index.taskQueue.queued||session?.status==='queued'))return false;
  taskQueuePolling=true;lastTaskQueuePoll=now;
  try{const snapshot=await api('/queue');index={...index,taskQueue:snapshot.taskQueue,executionCapacity:snapshot.executionCapacity};renderTaskQueue();return true;}
  catch(e){$('taskQueueContent').textContent='调度状态暂未更新：'+e.message;return false;}
  finally{taskQueuePolling=false;}
 }
 async function stopCurrentTask(){
  const wasQueued=session?.status==='queued';const result=await api('/session/stop',{project:project?.id||pid,session:session?.id||sid});
  if(result?.runs)acceptSession(result);
  if(index.taskQueue)try{const snapshot=await api('/queue');index={...index,taskQueue:snapshot.taskQueue,executionCapacity:snapshot.executionCapacity};}catch{}
  renderControls();renderTaskQueue();
  $('taskStatus').textContent=statusNames[session?.status]||session?.status||'等待状态';
  const attempt=latestRun()?.executionAttempts?.at(-1),neverAdmitted=attempt?.admissionVersion===1&&attempt.admittedAt==null;
  toast(wasQueued&&session?.status==='stopped'&&neverAdmitted?'已取消排队，本次未开始执行。':session?.status==='completed'?'任务已结束，未改变交付结果。':'已请求停止，等待当前执行收尾。');
 }
 async function persistTeamSelection(selection){acceptSession(await api('/session/team',{project:pid,session:sid,version:session.version,...selection}));teamDraft=null;}
 async function chooseProject(id,sessionId=null,runId=null){pid=id;sid=sessionId;session=null;teamDraft=null;selectedNode=null;selectedRunId=null;lastSeq=0;taskRouteEditKey=null;taskRouteTouched=false;routeMessageActive=false;LancerMotion.clear();project=pid?await api('/project?'+query({})):null;if(sid){session=await api('/session?'+query({}));lastSeq=session.events.at(-1)?.seq||0;if(session.runs.some(r=>r.id===runId)&&runId!==latestRun()?.id)selectedRunId=runId;}$('message').value='';locationState();render();}
 async function chooseSession(id){await chooseProject(pid,id);}
 function renderSidebar(){
  if(session&&!playback){const p=index.projects.find(p=>p.id===session.projectId),row=p?.sessions.find(s=>s.id===session.id);if(row)Object.assign(row,{status:session.status,version:session.version,updated:session.updated});}
  const search=$('projectSearch').value.trim().toLowerCase();
  const projects=index.projects.filter(p=>(p.title+' '+p.goal+' '+p.sessions.map(s=>s.title).join(' ')).toLowerCase().includes(search));
  $('projects').innerHTML=projects.length?projects.map(p=>`<button class="project-row ${p.id===pid?'selected':''}" data-project="${p.id}"><span class="project-symbol">▧</span><span><b>${esc(p.title)}</b><small>${p.sessions.length} 段会话 · ${date(p.updated)}</small></span><i>${p.id===pid?'⌄':'›'}</i></button>`).join(''):'<p class="sidebar-empty">还没有项目。点击 ＋ 创建。</p>';
  const rows=index.projects.find(p=>p.id===pid)?.sessions||[];
  $('sessions').innerHTML=rows.filter(s=>s.title.toLowerCase().includes(search)||!search||projects.some(p=>p.id===pid)).map(s=>`<button class="session-row ${s.id===sid?'selected':''}" data-session="${s.id}"><i class="session-dot ${s.status}"></i><span><b>${esc(s.title)}</b><small>${statusNames[s.status]||s.status} · ${date(s.updated)}</small></span></button>`).join('')||'<p class="sidebar-empty">在这个项目里开始一段会话。</p>';
  if(session&&current()?.nodes.length)$('sessions').insertAdjacentHTML('beforeend','<div class="agent-task-list">'+current().nodes.map(n=>`<button data-task-node="${n.id}" class="agent-task ${selectedNode===n.id?'selected':''}">${logo(n.agent)}<span><b>${esc(short[n.agent]||model(n.agent).name)} · ${esc(n.title)}</b><small>${phaseName(n)} · ${statusNames[n.status]||n.status}</small></span><i class="session-dot ${n.status==='done'?'completed':n.status}"></i></button>`).join('')+'</div>');
  $('newSession').disabled=!pid;$('projectSettings').disabled=!pid;
 }
 function renderControls(){
  const queued=session?.status==='queued',running=['queued','running'].includes(session?.status)||!!playback,resumable=['failed','stopped','interrupted'].includes(session?.status),ready=session?.status==='ready';
  $('message').disabled=running||busy;$('taskKind').disabled=running||busy;$('saveTask').disabled=running||busy;$('runTask').disabled=running||busy;
  $('stopTask').hidden=!running||!!playback;$('stopTask').disabled=busy;$('stopTask').textContent=queued?'取消排队':'停止';
  $('runTask').textContent=queued?'等待调度':resumable&&!$('message').value.trim()?'继续本轮 ↑':ready&&!$('message').value.trim()?'执行已保存任务 ↑':'发送并执行 ↑';
  $('exportSession').disabled=!session||busy;
  $('replayStart').disabled=busy||!current()?.nodes.length||!playback&&['queued','running'].includes(current()?.status);$('replayStart').textContent=playback?'重新回放 ↻':'回放流程 ↻';
  updatePlaybackControls();
  $('composerHint').textContent=playback?'正在回放已保存记录':historicalRun()?'正在查看历史轮次；新输入将继续最新任务':queued?'本次执行已排队，尚未开始执行':running?'当前模型正在使用独立上下文':session?.status==='conflict'?'项目已更新，请追加要求重新规划':'一个任务是一段完整会话';
  renderRunSelector();
  renderNativeConnection();renderTeamSelection();renderTaskRouteEditor();renderTaskQueue();renderModelReadiness();$('saveModelPolicy').disabled=busy||!modelPolicyEdit||!Number.isInteger(modelPolicyEdit.version);$('comparisonStart').disabled=busy;$('comparisonRefresh').disabled=busy;$('comparisonCase').disabled=busy;$('comparisonRepetitions').disabled=busy;$('controlledStart').disabled=busy;$('controlledModels').disabled=busy;$('saveCustomRole').disabled=busy;$('createCustomRole').disabled=busy||!project;$('saveContribution').disabled=busy||!!playback||session?.status==='running';$('workPolicyButton').disabled=busy||!index.workPolicy;$('saveWorkPolicy').disabled=busy;
 }
 function renderRelay(){
  const r=current(),running=r?.nodes.filter(n=>n.status==='running')||[],assignments=r?.plan?.modules||[];
  const rows=[{id:'plan',name:'规划与裁决',c:'#bc8b74',models:[coordinatorId(r)]},{id:'review',name:r?.audit?'架构互审':r?.workflowVersion===2?'局部互审':'开发互审',c:'#927bb3',models:r?.reviewers?.length?r.reviewers:['sol','gemini','deepseek']},{id:'build',name:r?.audit?'评价整理':'派生执行',c:'#ba8e9e',models:[...new Set(assignments.map(m=>m.assignment?.model).filter(Boolean))]}];
  if(!rows[2].models.length)rows[2].models=r?(r.teamModels||['sol','gemini','deepseek','image']):nextTeamSelection().teamModels;
  if(r?.workflowVersion===2){rows[0].name='规划与协调';rows[0].models=[...new Set([coordinatorId(r),...r.nodes.filter(n=>n.phase==='decision').map(n=>n.agent)])];rows[1].models=[...new Set(r.nodes.filter(n=>['review','verification'].includes(n.phase)).map(n=>n.agent))];if(rows[1].models.length&&r.nodes.some(n=>n.phase==='verification'))rows[1].name='审查与验收';if(!rows[1].models.length)rows.splice(1,1);}
  if(!r){rows.splice(1,1);rows[1].name='选定成员';}
  const hasNative=r?r.nodes.some(n=>n.agent==='native_codex')||r.teamModels?.includes('native_codex'):nextTeamSelection().teamModels.includes('native_codex');if(hasNative){for(const row of rows)row.models=row.models.filter(id=>id!=='native_codex');rows.push({id:'native',name:'原生工具',c:colors.native_codex,models:['native_codex']});}
  $('relayModels').innerHTML=rows.map(row=>`<div class="relay-row"><span class="relay-label" style="--c:${row.c}"><i></i>${row.name}</span>${row.models.map(id=>{
   const active=running.some(n=>n.agent===id&&(row.id==='native'||(row.id==='plan'?['draft','decision','integration'].includes(n.phase):row.id==='review'?['review','verification'].includes(n.phase):n.phase===row.id)));
   const connected=id==='native_codex'?index.native?.available&&index.native.enabled:index.available.includes(id),isDemo=r?.mode==='demo';
   return `<button id="relay-${row.id}-${id}" class="model-chip ${active?'running':''}" data-model="${id}">${logo(id)}<span>${esc(short[id]||model(id).name)}</span><small>${active?'处理中':isDemo?'演练':connected?id==='native_codex'?'原生登录':'已配置':'未连接'}</small></button>`;
  }).join('')}</div>`).join('');
 }
 function handoff(n,r){if(n.phase==='routing')return '宿主根据用户明确合同建立单模块计划，未请求模型规划。';const parents=n.deps.map(id=>r.nodes.find(x=>x.id===id)).filter(Boolean),names=[...new Set(parents.map(p=>model(p.agent).name))];if(!parents.length)return '我已接下任务，先读取项目约定与已有文件，理解交付目标。';return `我接手了 ${names.join('、')} 的${n.phase==='verification'?'待验收的实际产物与固定标准':n.repairRound?'未通过项与定向修复要求':n.phase==='review'?'方案与上一轮论据':n.phase==='decision'?(String(n.id).startsWith('clarify-')?'交接问题与上游资料':r.workflowVersion===2?'当前模块的审核论据':'三轮审核论据'):n.phase==='integration'?'各模块产物与本地检查结果':'执行方案和上游模块产物'}，接下来负责${n.title}。`;}
 function nextText(n,r){if(n.phase==='routing')return '由能力规则选择执行者；后续保留固定合同、宿主检查与质量验收。';const next=r.nodes.filter(x=>x.deps.includes(n.id)),names=[...new Set(next.map(x=>model(x.agent).name))];if(n.phase==='integration'&&!next.length)return r.status==='completed'?'成果已汇入项目，后续会话可以继续使用。':r.repairRoutingVersion===1?'将按失败原因决定修复、同产物复审或停止处理；质量通过后才交付。':'将依据验收结果决定交付或在限定轮次内修复。';if(names.length)return `交给 ${names.join('、')}：${[...new Set(next.map(phaseName))].join('与')}。`;if(n.phase==='draft'&&r.workflowVersion===2)return '将按工作需求分配模型；复杂任务按岗位开展有限讨论，其余模块直接执行。';if(n.phase==='draft')return ['development','audit'].includes(r.classification.kind)?'将把候选方案交给代码模型，进行三轮有限互审。':`将由 ${coordinatorName(r)} 敲定分工，派生执行者。`;if(n.phase==='verification')return r.repairRoutingVersion===1?`审查结果交给 ${coordinatorName(r)} 汇总；按失败原因决定修复、同产物复审或停止处理。`:`审查结果交给 ${coordinatorName(r)} 汇总；未通过项在限定轮次内定向修复。`;if(n.phase==='review'&&r.workflowVersion===2)return `本模块讨论最多三轮；形成可执行结论后交给 ${coordinatorName(r)} 裁决。`;if(n.phase==='review')return n.round<3?'本轮结论将交给下一轮审核，共用证据，保持各自独立上下文。':`第三轮结束后交给 ${coordinatorName(r)}，逐条裁决并敲定执行方案。`;if(n.phase==='decision')return '将按能力权重与依赖关系，分配独立模块执行者。';return '完成后将把文件和检查情况交给主模型汇总。';}
 const nativeStatus=s=>({running:'执行中',inProgress:'执行中',completed:'已结束',done:'已结束',passed:'通过',failed:'失败',blocked:'受阻',pending:'待验证',verified:'已核验',rejected:'未通过',skipped:'未执行',not_run:'未执行',approval_required:'等待授权',interrupted:'已中断'})[s]||s||'已记录';
 function nativeToolRows(tools){return tools.map(t=>`<div class="native-tool-row"><div><b>${esc(({commandExecution:'终端命令',fileChange:'文件变更'})[t.type]||t.type||'工具')}</b><small>${esc(nativeStatus(t.status))}${Number.isInteger(t.exitCode)?' · 退出码 '+t.exitCode:''}</small></div>${t.command?`<code>${esc(Array.isArray(t.command)?t.command.join(' '):textValue(t.command))}</code>`:''}</div>`).join('');}
 function nativeEvents(n,runId){return (session?.events||[]).filter(e=>e.node===n.id&&(!e.runId||e.runId===runId)&&['native_tool_started','native_tool_completed','native_output','tool_output','native_approval_required','native_result','native_collection_completed','native_collection_failed','native_execution_phase','native_runtime_error'].includes(e.type));}
 function nativeProgressHtml(n,runId){if(n.agent!=='native_codex')return '';const events=nativeEvents(n,runId),tools=new Map();for(const e of events){if(e.tool){const key=e.tool.id||'event-'+e.seq;tools.set(key,{...tools.get(key),...e.tool});}}const approval=events.findLast(e=>e.type==='native_approval_required'),output=events.findLast(e=>['native_output','tool_output'].includes(e.type)),result=events.findLast(e=>e.type==='native_result'),collection=events.findLast(e=>['native_collection_completed','native_collection_failed','native_execution_phase','native_runtime_error'].includes(e.type));return `<div class="native-evidence"><p class="native-caption">原生登录 · 文件与终端 · 已记录 ${tools.size} 次工具调用</p>${executionTimingsHtml(Object.fromEntries(events.filter(e=>e.type==='native_execution_phase').map(e=>[e.phase+'Ms',e.elapsedMs])),true)}${events.some(e=>e.type==='native_runtime_error')?'<details><summary>原生连接重试记录</summary>'+events.filter(e=>e.type==='native_runtime_error').map(e=>'<p class="native-warning">'+esc(textValue(e.runtimeError))+'</p>').join('')+'</details>':''}${nativeToolRows([...tools.values()])}${approval?`<p class="native-warning">执行需要授权：${esc(textValue(approval.request||approval.detail||'请查看原生执行器状态'))}</p>`:''}${output?.text?`<details><summary>最近一条工具输出</summary><pre>${esc(output.text)}</pre></details>`:''}${result?`<p>原生回执：${esc(nativeStatus(result.status))}${result.receiptRef?` · <code>${esc(result.receiptRef)}</code>`:''}</p>`:''}${nativeCollectionHtml(collection?.collection)}${!events.length?'<p>等待原生工具事件。</p>':''}</div>`;}
 function workerEvidenceHtml(n,runId){
  const events=(session?.events||[]).filter(event=>event.node===n.id&&event.runId===runId&&String(event.type||'').startsWith('worker_'));
  const candidate=n.result?.workerEvidence||n.workerEvidence,terminal=events.find(event=>event.seq===candidate?.eventSeq&&['worker_loop_completed','worker_loop_failed'].includes(event.type));
  const evidence=candidate&&candidate.nodeId===n.id&&candidate.runId===runId&&terminal?candidate:null;
  if(!events.length&&!evidence)return '';
  const labels={list:'列目录',read:'读取',search:'搜索',write:'修改',test:'固定预检',delegate:'派生子任务',accept_child:'接纳子任务',inspect_child:'查看子任务候选'},states={running:'执行中',completed:'已返回',passed:'通过',failed:'未通过',error:'执行错误',unknown:'结果未确认',cancelled:'已取消',timeout:'超时'};
  const ref=value=>typeof value==='string'&&!value.startsWith('/')&&!value.includes('\\')&&!value.split('/').includes('..')?value:'未提供可显示的相对引用';
  let html='<details class="native-evidence"><summary>Worker文件与固定脚本 · '+esc(evidence?states[evidence.status]||evidence.status:'已到达的执行记录')+'</summary><p class="native-caption">以下是执行岗位的本地预检，不等于交付通过；之后仍需正式宿主验收、独立复核及质量关卡。</p>';
  const tools=new Map();for(const event of events)if(event.toolId&&['worker_tool_started','worker_tool_completed','worker_tool_failed'].includes(event.type))tools.set(event.toolId,{id:event.toolId,name:event.name,status:event.status||(event.type==='worker_tool_started'?'running':event.type==='worker_tool_failed'?'failed':'completed'),rawRef:event.receiptRef,checkId:event.checkId,testStatus:event.testStatus,testPassed:event.testPassed});
  if(evidence)for(const item of evidence.tools||[])tools.set(item.id,{...tools.get(item.id),...item});
  if(evidence){
   html+='<p>'+Object.entries(labels).map(([key,label])=>label+' '+(Number.isInteger(evidence.toolCounts?.[key])?evidence.toolCounts[key]:'未记录')).join(' · ')+'</p>';
   const tests=evidence.tests||[];if(tests.length)html+='<div class="native-checks"><b>已记录预检 '+tests.length+' 条</b>'+tests.map((item,i)=>'<p>第 '+(i+1)+' 条 · '+esc(item.checkId)+' · '+esc(item.status==='passed'&&item.passed!==true?'结果未确认':states[item.status]||item.status)+' · 修订 '+esc(item.revision??'未记录')+'<br><small>回执 <code>'+esc(ref(item.receiptRef))+'</code></small></p>').join('')+'</div>';
   if(evidence.historyTruncated)html+='<p class="muted">动作摘要已截断，完整历史保留在原始回执。</p>';
   if(evidence.subagents?.length){
    const executable=evidence.subagents.some(child=>child.mode==='execute');
    html+='<details><summary>'+(executable?'同型号子任务':'同型号只读子任务')+' · '+evidence.subagents.length+' 个</summary><p class="muted">子任务使用独立上下文、顺序复用父Worker容量；不属于独立模型质量复核。执行子任务在独立副本中修改与预检，父Worker接纳后还须重验。</p>'+evidence.subagents.map(child=>{
     const adopted=(evidence.childAdoptions||[]).find(row=>row.childId===child.id);
     return '<p><b>'+esc(child.id)+' · '+esc(states[child.status]||child.status)+'</b><br>'+esc(model(child.model).name)+' · 实际调用 '+esc(child.callCount??'未知')+' 次 · '+(child.mode==='execute'?'独立副本执行 · '+(adopted?'父Worker已接纳':'候选待父Worker判断'):'只读分析')+'<br>'+esc(String(child.summary||child.error||'').slice(0,600))+(String(child.summary||child.error||'').length>600?'（摘要已截断）':'')+(child.scope?.length?'<br>修改范围：'+child.scope.map(esc).join('、'):'')+'<br><small>原始记录 <code>'+esc(ref(child.rawRef))+'</code></small></p>';
    }).join('')+'</details>';
   }
   html+='<p class="native-ref">Worker原始回执 <code>'+esc(ref(evidence.receiptRef))+'</code></p>';
  }else html+='<p>已记录 '+tools.size+' 个动作；尚无已到达的最终Worker回执。</p>';
  if(tools.size)html+='<details><summary>已到达的动作</summary>'+[...tools.values()].slice(-32).map(item=>'<p>'+esc(labels[item.name]||item.name)+' · '+esc(states[item.status]||item.status)+(item.name==='test'&&item.testStatus?' · '+esc(item.checkId||'固定标准')+' · 预检'+esc(item.testStatus==='passed'&&item.testPassed!==true?'结果未确认':states[item.testStatus]||item.testStatus):'')+(item.rawRef?'<br><small>'+esc(ref(item.rawRef))+'</small>':'')+'</p>').join('')+'</details>';
  return html+'</details>';
 }
 function executionTimingsHtml(t,native=false){
  if(!t)return '';
  const labels=native?[['连接完成','connectedMs'],['提交任务','submittedMs'],['首次工具','firstToolMs'],['首次文本','firstOutputMs'],['结束','totalMs']]:[['响应头','headersMs'],['首次文本','firstTextMs'],['结束','totalMs']];
  const rows=labels.filter(([,key])=>t[key]!=null).map(([label,key])=>label+' '+esc((t[key]/1000).toFixed(2))+' 秒');
  return rows.length?'<p class="native-caption">从本次请求起计：'+rows.join(' · ')+'</p>':'';
 }
 function nativeCollectionHtml(c){if(!c)return '';if(c.status==='archived')return `<p class="native-caption">成果已归档至：${esc(c.name||'原生 Codex 项目')}${c.archivePath?`<br>成果目录：<code>${esc(c.archivePath)}</code>`:''}</p>`;if(c.status==='failed')return `<p class="native-warning">归集未完成：${esc(c.error||'请检查归档配置。')}</p>`;return '';}
 function nativeChildrenHtml(evidence){
  if(!evidence.subAgents?.length&&evidence.childEvidenceStatus!=='unverified')return '';
  const rows=evidence.subAgents||[];
  return '<details class="native-checks"><summary>原生子任务证据 · '+rows.length+' 个 · '+(evidence.childEvidenceStatus==='verified'?'已核实关联':'尚未完整核实')+'</summary><p class="native-caption">来自已发生子任务的原始工具记录，不重新执行任务。嵌套或缺失回执单独标为未验证。</p>'+rows.map(row=>'<div class="native-tool-row"><b>'+esc(row.agentPath||row.threadId)+' · '+(row.status==='completed'?'记录完整':'未验证')+'</b><p>父会话 <code>'+esc(row.parentThreadId)+'</code><br>子会话 <code>'+esc(row.threadId)+'</code></p>'+(row.receiptRef?'<p>原始回执 <code>'+esc(row.receiptRef)+'</code><br>SHA-256 <code>'+esc(row.receiptSha256)+'</code></p>':'')+(row.error?'<p class="native-warning">'+esc(row.error)+'</p>':'')+(row.nestedThreadIds?.length?'<p>尚未归集的嵌套子任务：'+row.nestedThreadIds.map(esc).join('、')+'</p>':'')+'</div>').join('')+'</details>';
 }
 function nativeCheckBindingsHtml(binding){
  if(!binding)return '';
  let html='<details class="native-checks"><summary>必要验收命令 · '+binding.checks.length+' 项</summary><p class="native-caption">按固定验收项匹配命令回执；命令成功只证明该检查覆盖的范围。</p><p class="mono">契约 '+esc(binding.contractId)+'</p>';
  for(const check of binding.checks)html+='<div class="native-tool-row"><p><b>'+esc(check.id)+' · '+esc({passed:'通过',failed:'未通过',not_run:'未验证'}[check.status])+'</b><br>验收项 '+check.criterionIds.map(esc).join('、')+'</p><code>'+esc(textValue(check.command))+'</code><p>工作目录 '+esc(check.cwd)+'<br>绑定回执 '+esc(check.toolId||'无匹配回执')+'<br>实际退出码 '+(check.exitCode===null?'未记录':esc(check.exitCode))+' · 预期 '+esc(check.expectedExitCode)+'</p></div>';
  if(binding.operationIssues.length)html+='<h4>操作异常，不直接决定验收</h4>'+binding.operationIssues.map(issue=>'<div class="native-tool-row"><p><b>'+esc(issue.toolId)+'</b> · '+esc(nativeStatus(issue.status))+(Number.isInteger(issue.exitCode)?' · 退出码 '+issue.exitCode:' · 退出码未记录')+'</p><code>'+esc(textValue(issue.command))+'</code><p>工作目录 '+esc(issue.cwd)+'</p></div>').join('');
  if(!binding.allOperationsComplete)html+='<p class="native-warning">存在尚未完成或状态不明的操作，本次不能验收通过。</p>';
  return html+'</details>';
 }
 function nativeSearchHtml(evidence){
  if(!evidence)return '';
  return '<details class="native-checks"><summary>真实联网搜索 '+evidence.searches.length+' 次</summary><p class="native-caption">来自实际工具回执；次数包含失败或未完成的尝试，不代表结果已验证。</p>'+evidence.searches.map(item=>'<div class="native-tool-row"><b>'+esc(item.toolId)+' · '+esc(nativeStatus(item.status))+'</b><p>'+esc(item.kind==='mcp'?'MCP 搜索':'原生搜索')+(item.query?' · 查询：'+esc(textValue(item.query)):'')+'</p>'+(item.action?'<p>动作：'+esc(textValue(item.action))+'</p>':'')+'<p>事件引用 <code>'+esc(item.eventRef)+'</code></p>'+(item.resultRef?'<p>结果引用 <code>'+esc(item.resultRef)+'</code></p>':'')+(item.resultExcerpt?'<pre>'+esc(item.resultExcerpt)+'</pre>':'')+(item.resultChars!==undefined?'<small>结果 '+esc(item.resultChars)+' 字符'+(item.resultTruncated?' · 当前仅显示节选':'')+'</small>':'')+'</div>').join('')+'</details>';
 }
 function nativeEvidenceHtml(e,binding,search){if(!e)return '';const tools=Array.isArray(e.tools)?e.tools:[],checks=Array.isArray(e.checks)?e.checks:[],artifacts=Array.isArray(e.verifiedArtifacts)?e.verifiedArtifacts:[];return `<details class="native-evidence"><summary>原生执行回执 · ${tools.length} 次工具调用 · ${esc(nativeStatus(e.status))}</summary><p class="native-caption">沿用本机 Codex 登录 · ${esc(e.model||'本机默认模型')} · 文件与终端${search?.searches.length?' · 联网搜索':''}</p>${executionTimingsHtml(e.timings,true)}${nativeCollectionHtml(e.collection)}${nativeChildrenHtml(e)}${nativeSearchHtml(search)}${nativeCheckBindingsHtml(binding)}${nativeToolRows(tools)}<p class="native-caption">退出码表示对应工具进程的结果。模型填写的验收项需结合下方证据核对。</p>${checks.length?'<div class="native-checks"><b>'+(binding?'模型填写的检查说明':'回执验收项')+'</b>'+checks.map(c=>`<div><strong>${esc(c.name||'验收项')} · ${esc(nativeStatus(c.status))}</strong><p>${esc(textValue(c.evidence||'未附证据'))}</p></div>`).join('')+'</div>':''}${artifacts.length?'<div class="native-checks"><b>已核验文件 · '+artifacts.length+'</b>'+artifacts.map(a=>`<p><code>${esc(typeof a==='string'?a:a.path||a.name||textValue(a))}</code>${a.sha256?`<small>SHA-256 ${esc(a.sha256)}</small>`:''}</p>`).join('')+'</div>':''}${typeof e.inputUnchanged==='boolean'?`<p>${e.inputUnchanged?'输入快照核对：未改动':'输入快照核对：存在改动'}</p>`:''}${e.receiptRef?`<p class="native-ref">回执 <code>${esc(e.receiptRef)}</code></p>`:''}${e.manifestRef?`<p class="native-ref">文件清单 <code>${esc(e.manifestRef)}</code></p>`:''}<details><summary>完整原生执行凭据</summary><pre>${esc(JSON.stringify(e,null,2))}</pre></details></details>`;}
 function qualityEvidenceHtml(refs){return refs.map(ref=>'<span class="mono">'+esc(typeof ref==='string'?ref:ref.path)+(ref.startLine?' · L'+esc(ref.startLine)+(ref.endLine!==ref.startLine?'–'+esc(ref.endLine):''):'')+'</span>').join('<br>');}
 function qualityCriteriaHtml(criteria){return criteria.map(c=>'<div class="finding"><strong>'+esc(c.criterionId)+' · '+esc({met:'满足',unmet:'未满足',unverified:'未验证'}[c.status])+'</strong><p>'+esc(c.reason||'')+'</p><p>'+qualityEvidenceHtml(c.evidence)+'</p></div>').join('');}
 function resultHtml(m){const r=m.result||{};let html=`<p>${esc(m.text||r.summary||'')}</p>`;
  if(m.origin==='workflow_prompt')html+='<p class="muted">全局规范中的固定主持提问，不是一次模型生成。</p>';
  if(r.reflection){const f=r.reflection;html+='<div class="finding"><strong>'+esc(f.assessment==='deeper_issue'?'识别到的深层问题':'未发现有依据的新问题')+'</strong><p>'+esc(f.issue)+'</p><p>具体依据：'+esc(f.basis)+'</p><p>建议做法：'+esc(f.proposal)+'</p><p>验证方式：'+esc(f.verification)+'</p></div>';}
  if(r.approach)html+=`<p>${esc(typeof r.approach==='string'?r.approach:JSON.stringify(r.approach))}</p>`;
  if(m.phase==='verification'&&r.criteria?.length)html+='<details><summary>按固定标准审查产物 · '+r.criteria.length+' 项</summary>'+qualityCriteriaHtml(r.criteria)+'</details><p class="muted">文件审查判断；不等同于运行测试。</p>';
  if(r.findings?.length)html+=`<details><summary>${r.findings.length} 条审核论据 · 优先级${m.phase==='verification'?'':' / 复杂度'}</summary><div class="findings">${r.findings.map(f=>`<div class="finding"><strong><span class="priority ${esc(f.priority)}">${esc(f.priority)}</span>${esc(f.claim)}</strong><p>证据：${m.phase==='verification'?qualityEvidenceHtml(f.evidence):esc(f.evidence)}</p><p>建议：${esc(f.proposal)}${f.complexity?' · 复杂度 '+esc(f.complexity)+'/5':''}</p>${f.respondsTo?`<small>回应 ${esc(f.respondsTo)}</small>`:''}</div>`).join('')}</div></details>`;
  if(r.responses?.length)html+=`<details><summary>逐条回应上一模型 · ${r.responses.length} 项</summary>${r.responses.map(x=>`<div class="finding"><strong>${esc(({confirm:'确认成立',refute:'质询驳回',uncertain:'保留待验证'})[x.verdict]||x.verdict)} · ${esc(x.findingId)}</strong><p>${esc(x.reason)}</p><small>${(x.evidenceRefs||[]).map(f=>esc(f.path)+':'+esc(f.line)).join(' · ')}</small></div>`).join('')}</details>`;
  if(r.scores)html+=`<details><summary>七维评分与证据</summary>${Object.entries(r.scores).map(([k,v])=>`<div class="finding"><strong>${esc(({architecture:'架构边界',maintainability:'可维护性',security:'安全与权限',reliability:'可靠性',performance:'性能与扩展',testability:'测试与证据',delivery:'部署与运维'})[k]||k)} · ${v.score==null?'待验证':esc(v.score)+' / 10'}</strong><p>${esc(v.reason)}</p><small>${(v.evidenceRefs||[]).map(x=>esc(x.path)+':'+esc(x.line)).join(' · ')}</small></div>`).join('')}</details>`;
  if(r.modules?.length)html+=`<div class="turn-meta">已敲定 ${r.modules.length} 个模块 · ${r.decisions?.length||0} 条论据已裁决</div><button class="context-link" data-show-pane="plan">查看完整执行方案 ↗</button>`;
  if(r.files?.length)html+=`<div class="attachments">${r.files.map((f,i)=>`<button class="attachment" data-file-message="${m.id}" data-file-index="${i}">▱ ${esc(f.path)}</button>`).join('')}</div>`;
  if(r.risks?.length)html+=`<details><summary>剩余风险与验证范围</summary>${r.risks.map(x=>`<p>${esc(x)}</p>`).join('')}</details>`;
  if(r.moduleAcceptance?.length)html+='<details><summary>逐模块完成度验收 · '+r.moduleAcceptance.length+' 项</summary>'+r.moduleAcceptance.map(a=>'<div class="finding"><strong>'+(a.accepted?'✓ 已接纳':'待修正')+' · '+esc(a.moduleId)+'</strong><p>'+(Array.isArray(a.evidence)?a.evidence:[a.evidence]).filter(Boolean).map(x=>esc(textValue(x))).join('<br>')+'</p></div>').join('')+'</details>';
  if(r.nextSteps?.length)html+=`<details><summary>下一步验证</summary>${r.nextSteps.map(x=>`<p>${esc(x)}</p>`).join('')}</details>`;
  if(r.mcpEvidence&&session?.runs.find(run=>run.id===m.runId)?.nodes.some(n=>n.id===m.node&&n.status==='done'))html+=mcpEvidenceHtml(r.mcpEvidence);
  if(r.nativeEvidence&&session?.runs.find(run=>run.id===m.runId)?.nodes.some(n=>n.id===m.node&&n.status==='done'))html+=nativeEvidenceHtml(r.nativeEvidence,r.nativeCheckEvidence,r.nativeSearchEvidence);
  return html;
 }
 function renderConversation(){
  const feed=$('feed'),scroll=$('conversation'),nearEnd=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<110,oldTop=scroll.scrollTop;
  const expanded=new Set([...feed.querySelectorAll('details[open]')].map(e=>e.closest('[data-turn]')?.dataset.turn+'|'+e.querySelector('summary')?.textContent));
  if(!session){feed.innerHTML=`<div class="empty"><div class="empty-icon">雪.</div><h2>${pid?'让项目记住来路，也接住下一步。':'一个项目，多段会话。'}</h2><p>${pid?'描述这次要完成的任务。主模型会先读项目资料，再按能力分工。复杂开发模块最多三轮互审；普通任务直接分派、执行和验收。':'创建项目，保存业务约定、资料和成果。每个模型拥有独立工作上下文，交接时共享任务记忆和明确的证据。'}</p><div class="guide"><span>01 · 项目资料</span><span>02 · 有限互审</span><span>03 · 派生实现</span></div></div>`;return;}
  feed.innerHTML=session.messages.map(m=>{
   if(m.role==='user')return `<article class="user-turn" data-turn="${m.id}"><small>你的任务 · ${time(m.time)}</small>${esc(m.text)}</article>`;
   const r=session.runs.find(r=>r.id===m.runId),n=r?.nodes.find(n=>n.id===m.node);
   return `<article class="turn" data-turn="${m.id}"><div class="turn-head">${logo(m.model)}<b>${esc(model(m.model).name)}</b><time>${time(m.time)}</time><span class="round">${n?.repairReview?'同产物复审':n?.phase==='verification'?(n.qualityRound?'修复后复审 '+n.qualityRound:'首次产物审查'):n?.repairRound?'修复 '+n.repairRound:m.round?`审核 ${m.round}/3`:`任务第 ${session.runs.indexOf(r)+1} 轮`}</span></div><div class="turn-body"><div class="turn-meta"><span class="phase-tag" style="--c:${colors[m.model]||'#7b899b'}">${phaseName(n||{phase:m.phase})}</span><span>${m.origin==='workflow_prompt'?'固定主持提问':m.mode==='demo'?'固定样例 · 流程演练':'独立上下文'}</span></div>${n&&!n.fixedPrompt?`<p class="handoff-text">${esc(handoff(n,r))}</p>`:''}${resultHtml(m)}${n&&!n.fixedPrompt?`<p class="handoff-text">${esc(nextText(n,r))}</p>`:''}${m.contextRef?`<button class="context-link" data-context="${m.contextRef}">⌘ ${esc(m.agentId)} · 查看本次上下文 ↗</button>`:''}</div></article>`;
  }).join('');
  const r=playback?current():latestRun();
  for(const n of r?.nodes.filter(n=>n.status==='running'||(n.agent==='native_codex'||n.mcpEvidence)&&['failed','stopped','interrupted'].includes(n.status))||[])feed.insertAdjacentHTML('beforeend',`<article class="turn processing-turn"><div class="turn-head">${logo(n.agent)}<b>${esc(model(n.agent).name)}</b><time>${time(n.started)}</time><span class="round">${n.repairReview?'同产物复审':n.phase==='verification'?(n.qualityRound?'修复后复审 '+n.qualityRound:'首次产物审查'):n.repairRound?'修复 '+n.repairRound:n.round?`审核 ${n.round}/3`:`任务第 ${session.runs.length} 轮`}</span></div><div class="turn-body"><p>${esc(handoff(n,r))}</p><p class="${n.status==='running'?'processing':'native-warning'}">${n.status==='running'?'<i></i>正在处理':esc(statusNames[n.status]||n.status)} · ${esc(n.title)}${n.partialChars?` · 已接收 ${n.partialChars.toLocaleString()} 字符`:''}</p>${nativeProgressHtml(n,r.id)}${mcpProgressHtml(n,r.id)}${n.contextRef?`<button class="context-link" data-context="${n.contextRef}">查看独立上下文 ↗</button>`:''}</div></article>`);
  if(session.error)feed.insertAdjacentHTML('beforeend',`<div class="error-notice">${esc(session.error)}${session.status==='conflict'?'<br>项目在执行期间发生更新。候选文件已保留；补充一条要求，即可基于最新项目重新规划。':''}</div>`);
  if(r?.status==='completed')feed.insertAdjacentHTML('beforeend',`<div class="delivery-summary"><b>✓ ${r.mode==='demo'?'演练':'本轮'}交付已保存</b><span>${r.artifacts.length} 个文件 · 项目版本 ${esc(r.mergedRevision?.slice(0,8))}</span><button data-export>下载产物与记录 ↗</button><small>${r.audit?'检查范围：三轮覆盖、七维评分、证据行号与报告写入边界；未修改被审查源码。':r.nodes.some(n=>n.mcpEvidence||n.result?.mcpEvidence)?esc(r.final?.verificationNotice||'实际工具执行范围见 MCP 回执与主模型验收；未执行部分未验证。'):r.nodes.some(n=>n.result?.nativeEvidence)?'各模块的实际检查范围见原生执行回执与主模型验收。':'检查范围：文件归属、Python / JSON 语法；运行效果需按交付说明验证。'}</small></div>`);
  for(const e of feed.querySelectorAll('details'))if(expanded.has(e.closest('[data-turn]')?.dataset.turn+'|'+e.querySelector('summary')?.textContent))e.open=true;
  scroll.scrollTop=nearEnd?scroll.scrollHeight:oldTop;
 }
 function laneId(n){
  if(current()?.audit&&n.auditLane)return n.auditLane;
  if(['draft','routing'].includes(n.phase))return 'research';if(['decision','integration'].includes(n.phase))return 'release';
  if(n.phase==='verification')return 'engineering';if(n.phase==='review')return n.agent==='gemini'?'research':'engineering';
  const m=current()?.plan?.modules.find(m=>m.id===n.moduleId||'build-'+m.id===n.id),skills=m?.skills||{};
  return skills.image||skills.visual>=(skills.coding||0)?'visual':skills.writing>(skills.coding||0)&&skills.writing>(skills.reasoning||0)?'research':'engineering';
 }
 function plannedNodes(r){
  if(!r?.audit)return r?.nodes||[];
  const areas=r.auditPlan?.areas||r.nodes.find(n=>n.id==='draft')?.result?.areas;if(!areas)return r.nodes;
  const pending=[r.nodes.find(n=>n.id==='draft')];
  areas.forEach((a,i)=>{
   const decor={auditArea:a.id,auditLane:['research','engineering','visual','release'][i%4],auditSlot:Math.floor(i/4),status:'waiting'};
   for(let round=1;round<=3;round++)pending.push({...decor,id:`review-${round}-${a.id}`,title:`${a.title} · 审核 ${round}/3`,agent:round===2?a.secondary:a.primary,phase:'review',round,deps:[round===1?'draft':`review-${round-1}-${a.id}`]});
   pending.push({...decor,id:'decision-'+a.id,title:a.title+' · Opus裁决',agent:'opus',phase:'decision',deps:['review-3-'+a.id]});
   pending.push({...decor,id:'build-'+a.id,title:a.title+' · 整理评价',agent:'deepseek',phase:'build',deps:['decision-'+a.id]});
  });
  pending.push({id:'integration',title:'Opus · 总评与建议交付',agent:'opus',phase:'integration',status:'waiting',deps:areas.map(a=>'build-'+a.id)});
  return [...new Map([...pending,...r.nodes].filter(Boolean).map(n=>[n.id,n])).values()];
 }
 function graph(){
  const r=current(),baseNodes=plannedNodes(r),levels=new Map();
  function level(n){if(levels.has(n.id))return levels.get(n.id);const parents=n.deps.map(id=>baseNodes.find(p=>p.id===id)).filter(Boolean),d=parents.length?Math.max(...parents.map(level))+1:0;levels.set(n.id,d);return d;}
  const nodes=baseNodes.map(n=>({...n,laneId:laneId(n),layoutDepth:level(n)+(r.audit?(n.auditSlot||0)*.55:0),slot:r.audit?(n.auditSlot||0):n.phase==='review'?(n.agent==='sol'?1:n.agent==='deepseek'?2:0):0}));
  LancerStudioTree.render({runId:r?.id,nodes,models:[...index.catalog.models,{id:'host',name:'宿主路由',kind:'host'}],events:(session?.events||[]).filter(e=>!e.runId||e.runId===r?.id),selected:selectedNode||nodes.filter(n=>n.status==='running').at(-1)?.id||nodes.filter(n=>n.status==='done').at(-1)?.id,branchFocus,plan:r?.plan,
   onChoose:(id,seq)=>{selectedNode=id;graph();renderSidebar();const event=session?.events.find(e=>e.seq===seq),n=r.nodes.find(n=>n.id===id);if(event?.artifact){const f=n?.result?.files?.find(f=>f.path===event.artifact);if(f){showDetail(f.path,`<pre>${esc(f.content||'图片附件保留在交付包中。')}</pre>`);return;}}if(seq&&n?.contextRef)contextDetail(n.contextRef).catch(error);renderTeam();},
   onLane:id=>{selectedNode=nodes.find(n=>n.laneId===id)?.id;graph();},onFile:path=>{const n=r.nodes.find(n=>n.result?.files?.some(f=>f.path===path));const f=n?.result.files.find(f=>f.path===path);if(f)showDetail(path,`<pre>${esc(f.content||'图片附件')}</pre>`);}
  });
  $('nodeCount').textContent=nodes.length?`${nodes.filter(n=>n.status==='done').length} / ${nodes.length} 已完成`:'等待任务';
  $('motionStatus').textContent=r?.status==='running'?'事件轨迹 · 实时接力':r?.status==='completed'?'事件轨迹 · 已保存':'事件轨迹';
  $('elapsed').textContent=r?.workflowVersion===2?(reviewProgress(r).length?`${reviewProgress(r).length} 个局部模块 · 最多 3 轮`:'直接分派 · '+coordinatorName(r)+' 验收'):r?`${r.completedRounds} / ${['development','audit'].includes(r.classification.kind)?3:0} 轮互审`:'';renderNodeDetail();
 }
 function renderNodeDetail(){const n=plannedNodes(current()).find(n=>n.id===selectedNode);$('nodeDetail').innerHTML=n?`<strong>${esc(n.title)}</strong><br>${esc(model(n.agent).name)} · ${statusNames[n.status]||n.status}<br>接收 ${n.deps.length} 个依赖 · 独立代理 ${esc(n.agentId||'等待派生')}${executionTimingsHtml(n.receipt?.timings||(!playback&&['failed','stopped','interrupted'].includes(n.status)?n.attempts?.at(-1)?.receipt?.timings:null),n.agent==='native_codex')}${requestPolicyEvidenceHtml(n,current())}${n.contextRef?`<br><button data-context="${n.contextRef}">查看上下文与载入文件 ↗</button>`:''}${mcpProgressHtml(n,current()?.id)}${workerEvidenceHtml(n,current()?.id)}${n.agent==='native_codex'?n.status==='done'?nativeEvidenceHtml(n.result?.nativeEvidence,n.result?.nativeCheckEvidence,n.result?.nativeSearchEvidence):nativeProgressHtml(n,current()?.id):''}`:'选择节点查看模型、独立上下文与交接范围。';}

 function reviewRequirementHtml(requirements,schedule){
  if(!requirements||!schedule)return '';
  const independent=requirements.mode==='independent',mode=schedule.mode;
  let html='<div class="review-policy"><b>'+esc(independent?'固定要求：独立产物复核':mode==='independent'?'按任务风险安排独立产物复核':mode==='same_model'?'单模型对照：同型号产物复核':'由主模型逐项验收')+'</b>';
  if(requirements.reason)html+='<p>'+esc(requirements.reason)+'</p>';
  if(requirements.criterionIds?.length)html+='<p>关联标准：'+requirements.criterionIds.map(esc).join('、')+'</p>';
  if(mode!=='none')html+='<p>计划审核者：'+esc(schedule.reviewer?model(schedule.reviewer).name:'尚未分配')+(schedule.nodeId?' · 节点 '+esc(schedule.nodeId):'')+'</p><small>计划已安排；是否执行和通过，以本轮审查回执与质量关卡为准。'+(mode==='same_model'?'同型号复核不能满足独立模型复核要求。':'')+'</small>';
  return html+'</div>';
 }

 function renderPlan(){
  const r=current(),p=r?.plan;if(!p){$('planPane').innerHTML='<p class="muted">'+esc(coordinatorName(r))+' 完成规划后，这里会展示模块职责、模型匹配依据、局部审查范围和验收标准。</p>';return;}
  const localDecisions=r.workflowVersion===2?Object.entries(r.moduleReviews||{}).filter(([id])=>!playback||r.nodes.some(n=>n.id==='decision-'+id&&n.status==='done')).flatMap(([,value])=>value.decisions||[]):p.decisions||[];
  const decisions=new Map(localDecisions.map(d=>[d.findingId,d]));
  const visibleFindingIds=new Set((r.nodes||[]).filter(n=>n.status==='done').flatMap(n=>(n.result?.findings||[]).map(f=>f.id)));
  const evidence=(r.evidence||[]).filter(f=>!playback||r.workflowVersion!==2||visibleFindingIds.has(f.id));
  let html='<p>'+esc(textValue(p.summary))+'</p><p class="notice">'+(r.audit?'Opus 主导裁决；代码模型交叉审核，以下为评价文档的整理任务。原始源码只读。':r.workflowVersion===2?'按模块需要选择模型；复杂任务按岗位开展有限讨论。匹配分数是本地分配依据，不是官方排名或已验证成功率。':'能力分数是本工作台的分配策略，已扣除当前负载；不是官方排行榜。')+'</p>';
  for(const m of [...(p.modules||[])].sort((a,b)=>(a.priority||'').localeCompare(b.priority||'')||b.complexity-a.complexity)){
   html+='<section class="plan-module"><span class="priority '+esc(m.priority)+'">'+esc(m.priority)+'</span><small>复杂度 '+esc(m.complexity)+'/5'+(m.workType?' · '+esc(workNames[m.workType]||m.workType):'')+'</small><h3>'+esc(m.title)+'</h3>';
   html+='<div class="assignment">'+(m.assignment?.model?logo(m.assignment.model)+esc(model(m.assignment.model).name)+(r.audit?' · 评价整理':Number.isFinite(m.assignment.score)?' · 匹配 '+esc(m.assignment.score):''):'等待分派')+'</div>';
   if(m.roleId)html+='<p class="role-caption">项目角色：'+esc(roleTitle(m.roleId))+'</p>';
   html+='<p>'+esc(m.instructions)+'</p><div class="mono">'+(m.scope||[]).map(esc).join(' · ')+'</div><p>'+(m.dependsOn?.length?'依赖：'+m.dependsOn.map(esc).join('、'):'可独立并行执行')+'</p>';
   if(m.reviewPolicy)html+='<p class="review-policy '+(m.reviewPolicy.required?'scoped':'')+'">'+(m.reviewPolicy.required?'局部交叉审查 · 最多 '+esc(m.reviewPolicy.roundLimit||3)+' 轮':r.reviewRequirementVersion===1?'不做执行前讨论':'直接执行 · '+esc(coordinatorName(r))+' 验收')+'<br><small>'+esc(m.reviewPolicy.reason||'')+'</small></p>';
   if(r.reviewRequirementVersion===1)html+=reviewRequirementHtml(m.reviewRequirements,m.reviewSchedule);
   else if(m.artifactReview!==undefined)html+='<p class="muted">'+(m.artifactReview?(m.reviewMode==='same_model'?'产物完成后另做同型号复核；不属于独立模型确认。':'产物完成后另做独立代码审查；与执行前讨论分别记录。'):'产物完成后由主模型按验收标准审阅。')+'</p>';
   html+='<details><summary>验收标准与分配依据</summary>'+(m.acceptance||[]).map(a=>'<p>· '+esc(textValue(a))+'</p>').join('')+'<p>'+Object.entries(m.skills||{}).map(([k,v])=>esc(index.catalog.skills[k]||k)+' '+Math.round(v*100)+'%').join(' / ')+'</p><p>'+esc(m.assignment?.reason||'等待规划完成')+'</p><p>候选：'+((m.alternatives||[]).map(a=>esc(model(a.model).name)+(Number.isFinite(a.score)?' '+a.score:'')).join('、')||'暂无其他可用模型')+'</p></details></section>';
  }
  html+='<h3>集成方式</h3><p>'+esc(textValue(p.integration))+'</p><h3>'+evidence.length+' 条论据的最终裁决</h3>';
  for(const f of evidence){const d=decisions.get(f.id);html+='<div class="finding"><strong><span class="priority '+esc(f.priority)+'">'+esc(f.priority)+'</span>'+esc(f.claim)+'</strong><p>'+esc(model(f.model).name)+' · 复杂度 '+esc(f.complexity)+'/5</p><p>'+({accept:'采纳',reject:'驳回',defer:'保留'}[d?.decision]||'未决定')+'：'+esc(d?.reason)+'</p></div>';}
  $('planPane').innerHTML=html;
 }
 function renderMemory(){
  $('memoryPane').innerHTML=`<div class="memory-item"><h3>项目长期约定</h3><p>${esc(project?.instructions||'尚未设置。可以从左侧「项目资料与约定」补充。')}</p><span class="muted">${playback?'历史回放不载入当前项目文件清单':(project?.files.length||0)+' 个项目文件 · 按相关度加载文本'}</span></div><div class="memory-item"><h3>同一任务，共享记忆</h3><p>所有模型共享你的任务要求、项目约定、已确认成果及明确交接的论据；完整会话保存在本机。</p><p>模型的独立历史只带入它自己的上下文，不拼接其他模型的原始对话。</p></div>${Object.values(session?.agents||{}).map(a=>`<div class="memory-item"><div class="assignment">${logo(a.model)}${esc(model(a.model).name)}</div><p class="mono">${esc(a.id)}</p><p>${a.turns.length} 次工作 · 最近 3 次历史 + 更早工作摘要</p>${a.turns.at(-1)?`<button class="context-link" data-context="${a.turns.at(-1).contextRef}">查看最近一次上下文 ↗</button>`:''}</div>`).join('')}<h3>跨会话成果记忆</h3>${(project?.memory||[]).slice().reverse().map(m=>`<div class="memory-item"><small>${date(m.time)} · ${esc(m.source)}</small><p>${esc(m.summary)}</p><p>${m.artifacts.length} 个文件 · ${esc(m.revision.slice(0,8))}</p></div>`).join('')||'<p class="muted">任务交付后，摘要和文件引用会保留在项目中。</p>'}`;
 }

 function hostAcceptanceHistoryHtml(run,moduleId,visibleSeq){
  if(!run?.hostAcceptanceChecks?.length&&!run?.hostAcceptanceHistory?.length)return '';
  const reached=new Set((session?.events||[]).filter(event=>event.type==='host_acceptance'&&event.runId===run.id&&(!playback||(Number.isFinite(event.seq)&&event.seq<=visibleSeq))).map(event=>event.hostAcceptanceFingerprint).filter(Boolean));
  const history=(run.hostAcceptanceHistory||[]).filter(record=>reached.has(record.fingerprint)).map(record=>({...record,checks:(record.checks||[]).filter(check=>!moduleId||(check.moduleIds||[]).includes(moduleId))})).filter(record=>record.checks.length);
  let html='<details class="team-acceptance"><summary>宿主验收 · '+history.length+' 份已记录回执</summary><p class="muted">宿主结果只覆盖本轮登记标准与对应产物，不代替其他业务验收。原始失败保留，修复后的结果另行记录。</p>';
  if(!history.length)return html+'<p class="muted">当前视图尚无已到达的宿主验收回执。</p></details>';
  const labels={passed:'通过',failed:'未通过',error:'执行错误',timeout:'超时',unknown:'未确认'};
  const ref=value=>typeof value==='string'&&value&&!value.startsWith('/')&&!/^[A-Za-z]:/.test(value)&&!value.includes('\\')&&!value.split('/').includes('..')?value:'未记录安全的相对引用';
  for(const record of history){
   html+='<section class="team-discussion"><h3>'+(record.round===0?'首次交付':'修复第 '+esc(record.round)+' 轮')+'</h3><p class="mono">产物指纹：'+esc(String(record.artifactFingerprint||'未记录').slice(0,16))+'</p>';
   for(const check of record.checks){
    const receipt=check.receipt||{},errors=check.validationErrors||[],status=check.status==='passed'&&(check.passed!==true||errors.length)?'unknown':check.status;
    html+='<details class="team-business"><summary>'+esc(check.name||check.checkId)+' · '+esc(labels[status]||labels.unknown)+'</summary><p>'+(receipt.executed===true?'已实际执行':receipt.executed===false?'未执行脚本':'执行状态未记录')+' · 退出码 '+(Number.isInteger(receipt.exitCode)?esc(receipt.exitCode):'未记录')+' · 耗时 '+(Number.isFinite(receipt.elapsedMs)&&receipt.elapsedMs>=0?esc(Number((receipt.elapsedMs/1000).toFixed(3)))+' 秒':'未记录')+'</p>';
    if(receipt.error)html+='<p class="native-warning">'+esc(receipt.error)+'</p>';
    if(errors.length)html+='<p class="native-warning">宿主核验：'+errors.map(esc).join('；')+'</p>';
    const criteria=receipt.criteria||[];
    html+=criteria.length?'<p class="muted">以下为原始逐项记录，不覆盖上方宿主核验状态。</p>'+criteria.map(criterion=>'<p><b>'+esc(criterion.criterionId)+' · '+esc(labels[criterion.status]||labels.unknown)+'</b><br>'+esc(criterion.description||'')+(criterion.descriptionTruncated===true?' <small class="muted">（说明已截取）</small>':'')+(criterion.detail?'<br>'+esc(criterion.detail):'')+(criterion.detailTruncated===true?' <small class="muted">（结果详情已截取）</small>':'')+'</p>').join(''):'<p class="muted">没有已记录的逐项验收结果。</p>';
    html+='<p class="mono">原始回执：'+esc(ref(receipt.receiptRef))+'</p></details>';
   }
   html+='</section>';
  }
  return html+'</details>';
 }
 function repairRouteHtml(route){
  if(!route||route.version!==1)return '';
  const labels={code_repair:'修复产物',evidence_review:'核对证据并复审',review_disagreement:'澄清复核分歧',configuration:'检查配置',needs_input:'等待补充信息',replan:'重新规划',none:'无需返工'};
  const actions={repair:'按原契约定向修复，之后重新验收。',review:'保持产物和验收标准，核对原始回执后最多追加一次复审。',stop:'已停止自动返工，需先处理以下原因。',none:'当前没有需要返工的项目。'};
  return '<div class="finding"><strong>处理方式 · '+esc(labels[route.kind]||'待核实')+'</strong><p>'+esc(route.summary||'')+'</p><p class="muted">'+esc(actions[route.action]||'请核对本次处理记录。')+'</p></div>';
 }
 function qualityHistoryHtml(run,moduleId,visibleSeq,nodes){
  if(!run?.qualityHistory)return '';
  const history=run.qualityHistory.filter(entry=>!playback||entry.eventSeq<=visibleSeq);
  let html='<section class="team-acceptance"><h3>质量验收与修复记录</h3><p class="muted">标准满足率由逐项结论统计，不是任务成功概率。文件审查、静态检查和原生工具回执分别保留，未运行的场景不算通过。</p>';
  if(!history.length)return html+'<p class="muted">产物完成验收后显示结果。</p></section>';
  for(const [index,entry] of history.entries()){
   const rows=entry.modules.filter(row=>!moduleId||row.moduleId===moduleId);
   if(!rows.length)continue;
   html+='<details class="team-business"'+(index===history.length-1?' open':'')+'><summary>'+(entry.reviewKind==='evidence_review'?'同产物证据复审':entry.reviewKind==='review_disagreement'?'同产物分歧复核':entry.reviewId?'补证独立复审':entry.round?'修复第 '+entry.round+' 轮后':'首次产物验收')+' · '+(entry.passed?'整体验收通过':'整体验收未通过')+'</summary><p>'+esc(entry.summary||'')+'</p>';
   for(const row of rows){
    const metric=row.metrics,review=nodes.find(node=>node.id===row.reviewNodeId);
    html+='<section class="team-discussion"><h3>'+esc(row.title)+'</h3><p><b>'+(row.gatePassed?'✓ 本模块通过':'本模块待处理')+'</b> · '+(row.basis==='same_model_artifact_review'?'同模型复核（独立上下文）：'+esc(model(row.reviewer).name):row.basis==='independent_artifact_review'?'独立模型审查：'+esc(model(row.reviewer).name):row.basis==='coordinator_pending'?'等待 '+esc(coordinatorName(run))+' 审阅':esc(coordinatorName(run))+' 审阅')+'</p>';
    html+='<p>标准满足率 <b>'+metric.criterionCoveragePct+'%</b> · '+metric.criteriaMet+'/'+metric.criteriaTotal+' 项满足<br>未验证 '+metric.criteriaUnverified+' 项 · 阻断问题 '+metric.blockingFindings+' 项<br>已记录检查 '+metric.checksPassed+'/'+metric.checksTotal+' 项通过</p>';
    html+=repairRouteHtml(row.repairRoute);
    html+='<details><summary>逐项标准与证据</summary>'+qualityCriteriaHtml(row.criteria)+'</details>';
    if(row.findings.length)html+='<details><summary>审查发现 · '+row.findings.length+' 项</summary>'+row.findings.map(f=>'<div class="finding"><strong><span class="priority '+esc(f.priority)+'">'+esc(f.priority)+'</span>'+esc(f.claim)+'</strong><p>'+esc(f.proposal)+'</p><p>'+qualityEvidenceHtml(f.evidence)+'</p></div>').join('')+'</details>';
    html+='<details><summary>检查范围与结果</summary>'+row.checks.map(check=>'<p><b>'+(check.pass?'通过':'未通过')+'</b> · '+esc(check.name)+'<br><small>'+esc(check.type==='host_acceptance'?'宿主验收回执':({native_artifact_receipt:'文件指纹回执',native_tool_receipt:'原生工具退出状态',native_reported_checks:'原生模型报告，需结合工具回执',native_required_checks:'必要命令绑定验收'})[check.basis]||'本地静态检查')+'</small>'+(check.detail?'<br>'+esc(check.detail):'')+'</p>'+nativeCheckBindingsHtml(check.bindingEvidence)).join('')+'</details>';
    html+='<details><summary>本次验收的文件版本 · '+row.artifactManifest.length+' 个</summary><p>契约 '+esc(row.contractId)+'</p>'+row.artifactManifest.map(file=>'<p><span class="mono">'+esc(file.path)+'</span><br>'+file.size+' 字节 · SHA-256 <span class="mono" title="'+esc(file.sha256)+'">'+esc(file.sha256.slice(0,16))+'…</span></p>').join('')+'</details>';
    if(review?.contextRef&&review.status==='done')html+='<button class="context-link" data-context="'+esc(review.contextRef)+'">'+(row.basis==='same_model_artifact_review'?'查看同模型复核上下文':'查看独立审查上下文')+' ↗</button>';
    html+='</section>';
   }
   html+='</details>';
  }
  return html+'</section>';
 }
 function visibleContributions(run,visibleSeq){return (run?.contributionHistory||[]).filter(entry=>!playback||entry.eventSeq<=visibleSeq).at(-1);}
 function canRequestEvidenceReview(run=current()){
  return !playback&&!historicalRun()&&run?.mode==='live'&&run.workflowVersion===2&&['failed','interrupted','stopped'].includes(session?.status)&&run.final?.accepted===false&&!!run.qualityHistory?.length&&!!run.taskContracts&&run.nodes.some(node=>node.agent==='native_codex'&&node.status==='done');
 }
 async function requestEvidenceReview(){
  if(!canRequestEvidenceReview())throw Error('请在最新未通过的原生任务中发起补证复审。');
  acceptSession(await api('/session/evidence-review',{project:session.projectId,session:session.id,version:session.version}));
  renderTeam();renderControls();toast('已开始收集原始证据并独立复审，原生工具不会重新执行。');
 }
 function evidenceReviewHtml(run,visibleSeq){
  const reviews=(run?.evidenceReviews||[]).filter(entry=>!playback||entry.eventSeq<=visibleSeq),available=canRequestEvidenceReview(run);
  if(!reviews.length&&!available)return '';
  let html='<section class="team-acceptance"><h3>补充证据与独立复审</h3><p class="muted">从已保存的原始工具记录补全证据，再请主模型重新验收。原执行、失败结论与文件版本继续保留。</p>';
  if(available)html+='<button class="context-link" data-evidence-review'+(busy?' disabled':'')+'>收集原始证据并复审 ↗</button>';
  for(const entry of reviews){
   const finished=!playback||entry.completedEventSeq&&entry.completedEventSeq<=visibleSeq,status=finished?entry.status:'running';
   html+='<details class="team-business"><summary>'+esc(({running:'正在补证复审',completed:'复审已完成',failed:'复审需要处理',stopped:'复审已停止',interrupted:'复审已中断',conflict:'复审遇到项目版本冲突'})[status]||status)+' · '+esc(time(entry.created))+'</summary><p class="mono">复审 '+esc(entry.id)+' · 原终审 '+esc(entry.sourceFinalNodeId)+'</p>';
   if(finished){html+='<p>工具记录 '+esc(entry.toolCount??0)+' 条 · 缺失项 '+esc(entry.missingCount??0)+' 项</p>'+(entry.qualityPassed!==undefined?'<p>'+(entry.qualityPassed?'✓ 整体验收通过':'整体验收未通过')+'</p>':'');if(entry.error)html+='<p>'+esc(entry.error)+'</p>';if(entry.supplementRef)html+='<p class="mono">证据包 '+esc(entry.supplementRef)+'</p>';if(entry.contextRef)html+='<button class="context-link" data-context="'+esc(entry.contextRef)+'">查看实际复审上下文 ↗</button>';}
   html+='</details>';
  }
  return html+'</section>';
 }
 function runtimeBuildHtml(run){
  if(!run)return '';
  const build=run.runtimeBuild,workers=run.nodes.filter(node=>node.result?.nativeEvidence?.workerBuild);
  let html='<details class="team-business"><summary>本轮运行版本</summary><p class="muted">服务版本是启动时的源码快照。旧记录没有版本字段时保持未知。</p>';
  html+=build?'<p>服务指纹 <span class="mono">'+esc(build.fingerprint)+'</span><br>任务启动时源码'+(run.sourceMatchesRuntimeBuild?'与服务启动快照一致':'与服务启动快照不一致或未确认')+'</p>':'<p>服务版本：旧记录未保存</p>';
  for(const node of workers)html+='<p>'+esc(node.title||node.id)+' · 原生 worker<br><span class="mono">'+esc(node.result.nativeEvidence.workerBuild.fingerprint)+'</span></p>';
  return html+'</details>';
 }
 function canVerifyContribution(record,run=current()){
  return !busy&&!playback&&session?.status!=='running'&&run?.status!=='running'&&record?.canVerify===true;
 }
 const canReviewContribution=(record,run=current())=>!busy&&!playback&&session?.status!=='running'&&run?.status!=='running'&&!!record&&(record.state==='pending'||canVerifyContribution(record,run));
 function contributionEvidenceHtml(row){return '<details><summary>建议来源与文件版本</summary>'+row.sources.map(source=>'<p>'+esc(source.kind==='objective_failed_criterion'?'客观评测器':model(source.model).name||'来源未记录')+' · '+esc(({accepted_suggestion:'已采纳建议',identified_unmet_criterion:'发现未满足验收项',objective_failed_criterion:'客观评测发现',review:'审查意见'})[source.kind]||source.kind)+'<br>'+esc(source.kind==='objective_failed_criterion'?'被评估产物节点：':'来源节点：')+esc(source.nodeId)+'<br>'+esc(source.claim)+(source.evaluatorFingerprint?'<br>评测器指纹：'+esc(source.evaluatorFingerprint):'')+'</p>').join('')+'<p>'+(row.beforeKind==='plan'?'原方案快照（尚无旧产物）':'修改前文件')+'</p>'+(row.beforeKind==='plan'?'<p class="muted">人工核验需核对原方案缺口与实际成果；从方案变成文件本身不代表改善。</p>':'')+'<pre>'+esc(JSON.stringify(row.beforeManifest,null,2))+'</pre><p>修改后文件</p><pre>'+esc(JSON.stringify(row.afterManifest,null,2))+'</pre><p class="mono">证据指纹 '+esc(row.evidenceFingerprint)+'</p></details>';}
 function contributionHistoryHtml(run,moduleId,visibleSeq){
  if(!run?.contributionHistory)return '';
  const ledger=visibleContributions(run,visibleSeq);
  let html='<section class="team-acceptance"><h3>贡献记账</h3><p class="muted">每项固定验收标准只记一次。普通贡献 1 分，深层建议采用本轮保存的权重；已验证与待验证候选分开记录，无独立证据不计分。</p>';
  if(!ledger)return html+'<p class="muted">尚无此时已发生的贡献记录。</p></section>';
  const summary=ledger.summary,rows=ledger.records.filter(row=>!moduleId||row.moduleId===moduleId);
  html+='<p><b>本轮已验证贡献 '+esc(summary.verifiedPoints)+' 分</b> · 待验证 '+esc(summary.pendingCount)+' 项 · 已否决 '+esc(summary.rejectedCount)+' 项<br>本轮首次通过率 '+(summary.firstPass.ratePct===null?'暂无样本':esc(summary.firstPass.ratePct)+'%')+'（'+esc(summary.firstPass.passed)+'/'+esc(summary.firstPass.total)+'）<br>返工调用 '+esc(summary.repairCalls)+' 次 · 讨论调用 '+esc(summary.discussionCalls)+' 次</p>';
  for(const [label,key] of [['返工','repairElapsedMs'],['讨论','discussionElapsedMs']])html+='<p>'+label+'请求耗时合计：'+(summary[key]==null?'暂无记录':esc((summary[key]/1000).toFixed(1))+' 秒')+'</p>';
  html+='<p class="muted">以上为本轮全部模块累计。调用次数不是贡献分；请求耗时合计不是任务总历时，也不表示节省时间。'+(summary.missingLatencyCount?'有 '+esc(summary.missingLatencyCount)+' 次请求缺少耗时记录。':'')+'</p>';
  if(ledger.notice)html+='<p class="muted">'+esc(ledger.notice)+'</p>';
  html+='<p>'+ (moduleId?'当前模块':'本轮')+'贡献明细 · '+rows.length+' 项</p>';
  for(const row of rows){
   html+='<details class="team-discussion"><summary>'+esc(row.verification?.basis==='covered_by_objective'?'已由客观记录计分，不重复累计':{verified:'已验证',pending:'待独立核验',rejected:'已否决'}[row.state])+' · '+esc(row.title||row.criterion)+' · '+esc(row.points)+' 分</summary><p>'+esc(row.criterionId||'尚未关联固定标准')+' · '+esc(row.criterion||'')+'<br>'+esc(row.kind==='correction'?'纠正问题':'改进建议')+'</p><p>'+esc(row.verification?.reason||'尚无客观验证证据，等待独立核验。')+'</p>';
   if(row.weight>1)html+='<p>深层建议权重 '+esc(row.weight)+'× · '+(row.potentialPoints?'通过独立验证才计 '+esc(row.potentialPoints)+' 分':'尚未关联固定标准，不能计分')+'</p>';
   if(row.verification?.basis)html+='<p class="muted">核验依据：'+esc(({human_review:'用户核验（非自动测试）',objective_evaluation:'客观评测',covered_by_objective:'已由客观记录计分，不重复累计',unverified:'尚无独立核验'})[row.verification.basis]||row.verification.basis)+'</p>';
   if(row.coveredBy?.length)html+='<p class="mono">对应客观记录：'+row.coveredBy.map(esc).join('、')+'</p>';
   if(row.verification?.basis&&row.verification.basis!=='unverified')html+='<details><summary>核验方法与结果原始记录</summary><pre>'+esc(JSON.stringify(row.verification,null,2))+'</pre></details>';
   html+=contributionEvidenceHtml(row);
   if(canReviewContribution(row,run))html+='<button class="context-link" data-contribution-verify="'+esc(row.id)+'">核验此贡献'+(row.state==='verified'?' / 撤回认定':!canVerifyContribution(row,run)?' / 仅可否决':'')+' ↗</button>';
   html+='</details>';
  }
  return html+'<details class="team-business"><summary>独立效率记录</summary><p class="muted">局部贡献没有同条件基线，暂不计效率分。整体耗时改善仅在协作量化对照中按双方达标的匹配记录单独展示。</p></details></section>';
 }
 function projectContributionHtml(){
  if(playback||!project?.contributionMetrics)return '';
  const metrics=project.contributionMetrics;
  return '<details class="team-business"><summary>项目贡献累计</summary><p class="muted">'+(historicalRun()?'实时项目累计，并非当前历史轮次当时的数据。':'当前项目累计；不是模型排名。')+'</p><p>已验证贡献 '+esc(metrics.verifiedPoints)+' 分 · 待验证 '+esc(metrics.pendingCount)+' 项 · 已否决 '+esc(metrics.rejectedCount)+' 项<br>客观评测确认：'+(metrics.objectiveVerifiedPoints==null?'未记录':esc(metrics.objectiveVerifiedPoints)+' 分')+'<br>用户人工核验：'+(metrics.humanVerifiedPoints==null?'未记录':esc(metrics.humanVerifiedPoints)+' 分')+'<br>记录覆盖 '+esc(metrics.runCount)+' 个执行轮次</p><p class="muted">'+esc(metrics.notice)+'</p></details>';
 }
 function openContributionVerification(id){
  const run=current(),record=visibleContributions(run,Infinity)?.records.find(row=>row.id===id);
  if(!canReviewContribution(record,run))return;
  contributionEdit={project:pid,session:sid,runId:run.id,recordId:record.id,evidenceFingerprint:record.evidenceFingerprint,version:session.version};
  $('contributionTarget').textContent=(record.title||record.criterion)+' · '+record.criterionId;
  $('contributionEvidence').innerHTML=contributionEvidenceHtml(record);
  $('contributionBeforeLabel').textContent=record.beforeKind==='plan'?'原方案缺口的观察结果':'修改前的观察结果';$('contributionBefore').placeholder=record.beforeKind==='plan'?'原方案存在什么缺口？为何不能满足此验收标准？':'修改前如何不满足此验收标准？';
  for(const field of ['contributionBefore','contributionAfter','contributionMethod'])$(field).value='';
  $('contributionVerifiedOption').disabled=!canVerifyContribution(record,run);$('contributionVerdict').value=record.state==='verified'||!canVerifyContribution(record,run)?'rejected':'verified';$('contributionFeedback').textContent='';$('contributionDialog').showModal();
 }
 async function saveContributionVerification(){
  await guarded(async()=>{try{
   const next=await api('/contribution/verify',{...contributionEdit,verdict:$('contributionVerdict').value,beforeObservation:$('contributionBefore').value,afterObservation:$('contributionAfter').value,method:$('contributionMethod').value});
   acceptSession(next);lastSeq=session.events.at(-1)?.seq||0;project=await api('/project?'+query({}));await refreshIndex();$('contributionDialog').close();contributionEdit=null;toast('用户核验已记入历史，原记录保留。');
  }catch(e){$('contributionFeedback').textContent=e.message;throw e;}});renderTeam();
 }
 function projectMetricsHtml(){
  if(playback||!project?.qualityMetrics)return '';
  const metrics=project.qualityMetrics;
  let html='<details class="team-business"><summary>项目协作度量</summary><p class="muted">'+(historicalRun()?'实时项目累计，并非当前历史轮次当时的数据。':'当前项目的真实执行累计。')+'演练不计入统计。</p>';
  if(!metrics.rows.length&&!metrics.requests.length)return html+'<p>暂无真实执行样本；演练不计入。</p></details>';
  if(metrics.notice)html+='<p class="muted">'+esc(metrics.notice)+'</p>';
  if(!metrics.rows.length)html+='<p>暂无真实质量验收样本。</p>';
  for(const row of metrics.rows){
   html+='<section class="team-discussion"><div class="assignment">'+logo(row.model)+'<b>'+esc(model(row.model).name)+' · '+esc(workNames[row.workType]||row.workType)+'</b></div>'+(row.policyLabel?'<small class="muted">'+esc(row.policyLabel)+'</small>':'')+'<p>'+row.qualitySamples+' 个模块样本 · 来自 '+row.independentSessions+' 个会话<br>首次通过 '+row.firstPassCount+'/'+row.qualitySamples+'（'+row.firstPassRatePct+'%）<br>最终通过 '+row.finalPassCount+'/'+row.qualitySamples+'（'+row.finalPassRatePct+'%）<br>已合入 '+row.mergedCount+'/'+row.qualitySamples+'（'+row.mergedRatePct+'%）<br>累计修复 '+row.repairRounds+' 轮</p>';
   if(row.insufficientSamples)html+='<p class="muted">样本不足，仅作观察，不据此判断模型优劣。</p>';
   html+='</section>';
  }
  if(metrics.requests.length)html+='<h3>请求记录</h3><p class="muted">请求返回不代表质量验收通过。耗时是已记录请求的平均时间，包含不同任务长度。</p>';
  for(const row of metrics.requests)html+='<section class="team-discussion"><p><b>'+esc(model(row.model).name)+' · '+(row.adapter==='native'?'原生工具':'API')+'</b><br>'+row.requestCount+' 次请求 · 错误 '+row.errorCount+' 次（'+row.errorRatePct+'%） · 取消 '+row.cancelledCount+' 次<br>平均耗时 '+(row.averageElapsedMs===null?'未记录':(row.averageElapsedMs/1000).toFixed(1)+' 秒')+' · '+row.latencySamples+' 条耗时记录</p></section>';
  return html+'</details>';
 }
 function comparisonEntryHtml(){
  if(!project||playback)return '';
  return '<section class="team-acceptance"><h3>协作量化对照</h3><p class="muted">固定案例的工作流对照，以及不分摊额度的 SAS / MAS 交付对照。'+(historicalRun()?'这是当前项目的试验入口，不属于此历史轮次。':'')+'</p><button class="context-link" data-comparison-open>查看或运行真实对照 ↗</button></section>';
 }
 function roleCooperationHtml(){
  if(playback||!project?.roleCooperation)return '';
  const data=project.roleCooperation,count=value=>value===null?'未知':esc(value),percent=value=>value===null?'未知':esc(Number(value.toFixed(2)))+'%';
  const rate=(label,item,passed)=>'<p><b>'+label+'</b> '+percent(item.ratePct)+'（'+esc(passed)+'/'+esc(item.observed)+' 已观测）<br><small class="muted">机会 '+esc(item.total)+' · 未知 '+esc(item.unknown)+'</small></p>';
  let html='<details class="team-business"><summary>岗位配合 · '+esc(data.rows.length)+' 组有向关系</summary><p class="muted">'+(historicalRun()?'当前项目累计，并非当前历史轮次当时的数据。':'当前项目的真实交接累计。')+'演练不计入；按岗位方向、模型组合、工作类型和规范版本分别观察，不是因果收益或模型排名。</p>';
  if(data.notice)html+='<p class="muted">'+esc(data.notice)+'</p>';
  html+='<p class="muted">覆盖 '+esc(data.coverage.eligibleRuns)+' 次真实业务运行 · 已规划 '+esc(data.coverage.plannedEdges)+' 条依赖边 · 下游尚未开始 '+esc(data.coverage.notReachedEdges)+' 条；未到达不算接收岗位失败。</p>';
  html+='<p class="muted">首交与最近关卡分别指最早与最近已记录的关卡；运行中结果仍可能变化。</p>';
  html+='<p>协作交互效应：'+(data.interaction.status==='missing_matched_ablations'?'未测（缺少匹配的四臂对照）':esc(data.interaction.status))+'</p><p class="muted">'+esc(data.interaction.notice)+'</p>';
  if(!data.rows.length)return html+'<p class="muted">暂无可比较的真实岗位交接；没有机会不记为零分。</p></details>';
  for(const row of data.rows){
   const questions=row.clarifications,sessions=row.sessionSuccess;
   html+='<section class="team-discussion"><h3>'+esc(roleTitle(row.sourceRole,null))+' → '+esc(roleTitle(row.targetRole,null))+(row.sameRole?' · 同岗位交接':'')+'</h3><p>'+esc(model(row.sourceModel).name)+' → '+esc(model(row.targetModel).name)+'<br>'+esc(workNames[row.sourceWorkType]||row.sourceWorkType)+' → '+esc(workNames[row.targetWorkType]||row.targetWorkType)+'</p><p class="muted">'+esc(row.opportunities)+' 次交接机会 · '+esc(row.sessionCount)+' 个会话</p>';
   html+=rate('交接证据率',row.handoff,row.handoff.verified)+'<small class="muted">已核实 '+esc(row.handoff.verified)+' · 不匹配 '+esc(row.handoff.mismatched)+'</small>';
   html+=rate('下游首交关卡',row.firstGate,row.firstGate.passed)+'<small class="muted">未通过 '+esc(row.firstGate.failed)+'</small>';
   html+=rate('下游最近关卡',row.finalGate,row.finalGate.passed)+'<small class="muted">未通过 '+esc(row.finalGate.failed)+'</small>';
   html+='<p>下游修复启动 '+count(row.repairs.startedBuilds)+' 次<br><small class="muted">已观测机会 '+esc(row.repairs.observedOpportunities)+' · 未知机会 '+esc(row.repairs.unknownOpportunities)+'</small></p>';
   html+='<p><b>定向澄清</b> '+count(questions.questions)+' 个问题 · 答复率 '+percent(questions.answeredRatePct)+'<br>答复标记已回答 '+count(questions.answered)+' · 标记未解决 '+count(questions.unresolved)+' · 未答复 '+count(questions.unanswered)+' · 未知 '+count(questions.unknown)+'<br><small class="muted">已观测交接机会 '+esc(questions.observedOpportunities)+' · 未知机会 '+esc(questions.unknownOpportunities)+'；收到答复不等于问题已经验证解决。</small></p>';
   html+=rate('会话内交接证据及首交全部通过',sessions,sessions.passed)+'<p class="muted">Wilson 95% 区间：'+(sessions.wilson95?percent(sessions.wilson95.lowPct)+'–'+percent(sessions.wilson95.highPct):'未知（暂无已观测会话）')+'。以会话为样本，区间假设会话独立；未排除历史相关性。</p>';
   if(sessions.insufficientSamples)html+='<p class="muted">会话样本不足，仅作观察，不用于调整岗位能力权重。</p>';
   if(row.sharedDownstreamOpportunities)html+='<p class="muted">'+esc(row.sharedDownstreamOpportunities)+' 次机会共享下游结果，不能把同一通过结果分别归功于各上游。</p>';
   html+='<details><summary>岗位标识与规范分组</summary><p>'+esc(row.sourceRole)+' → '+esc(row.targetRole)+'</p><p class="mono">'+esc(row.contextFingerprint)+'</p>'+(row.contextComplete===false?'<p class="muted">岗位规范记录不完整；不据此认定不同会话条件完全一致。</p>':'')+(row.contextNotice?'<p class="muted">'+esc(row.contextNotice)+'</p>':'')+'</details>';
   if(row.evidence?.length)html+='<details><summary>本组交接依据 · '+esc(row.evidence.length)+' 条</summary><pre>'+esc(JSON.stringify(row.evidence,null,2))+'</pre></details>';
   html+='</section>';
  }
  return html+'</details>';
 }
 function comparisonContentHtml(data,caseId=null){
  const selected=(data.cases||[data.case]).find(c=>c.id===caseId)||data.case,records=data.records.filter(r=>(r.caseId||data.case.id)===selected.id||r.evaluationNotice);
  let html='<h3>原工作流历史对照</h3><p class="muted">这里的单模型仍分多个岗位与上下文，不是严格 SAS；历史结果不作为等预算 MAS 优势证据。</p><p class="muted">'+esc(data.notice||'')+'</p><p class="muted">本案例按 '+esc(selected.rubric?.length||0)+' 项固定等权标准验收；通过率不是生产力总分。</p><details class="team-business"><summary>固定案例 · '+esc(selected.title)+'</summary><p>'+esc(selected.instructions)+'</p><details><summary>案例文件与固定验收标准</summary><pre>'+esc(JSON.stringify({files:selected.files,rubric:selected.rubric},null,2))+'</pre></details></details>'+comparisonSummariesHtml((data.summaries||[]).filter(g=>g.caseId===selected.id));
  if(!records.length)return html+'<p class="muted">暂无对照记录。单次结果用于观察，重复同条件试验后再判断协作收益。</p>';
  for(const record of records){
   const invalid=!!record.evaluationNotice;
   html+='<section class="team-acceptance"><h3>'+esc(record.title)+' · '+esc(record.status==='completed'?'对照结束':statusNames[record.status]||record.status)+(record.repeatCount?' · 第 '+esc(record.repeatIndex)+'/'+esc(record.repeatCount)+' 轮':'')+'</h3><p class="muted">'+esc(record.created)+' · 顺序：'+record.order.map(k=>k==='single'?'单模型':'多模型').join(' → ')+'</p>';
   if(invalid)html+='<p class="native-warning"><b>历史无效评分记录</b><br>'+esc(record.evaluationNotice)+'<br>原案例：'+esc(record.caseId)+'</p>';
   for(const kind of ['single','multi']){
    const arm=record.arms[kind],evaluation=arm?.evaluation,metrics=arm?.metrics;
    html+='<details class="team-business" open><summary>'+(kind==='single'?'单模型':'多模型协作')+' · '+esc(arm?(statusNames[arm.status]||arm.status):'等待执行')+'</summary>';
    if(arm?.teamModels)html+='<p class="muted">已选成员：'+arm.teamModels.map(id=>esc(model(id).name)).join('、')+'</p>';
    if(selected.evaluationKind==='python_runtime')html+='<p class="muted">'+(kind==='single'?'同一型号分角色、独立上下文复核，不属于跨模型独立审查。':'成员按岗位协作，保留实际讨论、分工与审查记录。')+'</p>';
    if(arm?.error)html+='<p class="native-warning">'+esc(arm.error)+'</p>';
    if(selected.evaluationKind==='python_runtime'&&evaluation&&!evaluation.runtime)html+='<p class="native-warning">代码运行验收未执行：尚无完整源码交付。下方通过项数不代表已测试的代码质量。</p>';
    if(evaluation)html+='<p><b>'+(invalid?'历史无效评分 ':'固定验收通过率 ')+esc(evaluation.scorePct)+'%</b> · '+esc(evaluation.passed)+'/'+esc(evaluation.total)+' 项<br>'+(invalid?'不作为模型失败判断；保留原始评分供追溯':evaluation.accepted?'验收通过':'验收未通过')+'</p><details><summary>'+(invalid?'原始逐项评分（本记录无效）':'逐项验收结果')+'</summary>'+evaluation.criteria.map(c=>'<p><b>'+esc(c.id)+' · '+esc(c.title)+' · '+esc({passed:'通过',failed:'未通过',not_run:'未执行'}[c.status]||c.status)+'</b><br>'+esc(c.detail)+'</p>').join('')+'<p class="mono">产物指纹 '+esc(evaluation.artifactFingerprint)+'</p></details>';
    else html+='<p class="muted">尚无实际验收结果。</p>';
    if(evaluation?.runtime)html+='<details class="team-business"><summary>实际代码运行回执</summary><pre>'+esc(JSON.stringify(evaluation.runtime,null,2))+'</pre></details>';
    if(metrics?.evaluationMs!==undefined)html+='<p class="muted">总历时包含宿主代码验收 '+esc((metrics.evaluationMs/1000).toFixed(2))+' 秒。</p>';
    if(metrics)html+='<p>任务耗时 '+(metrics.wallMs===null?'未记录':esc((metrics.wallMs/1000).toFixed(1))+' 秒')+'<br>调用 '+esc(metrics.callCount)+' 次 · 错误 '+esc(metrics.errorCount)+' 次 · 取消 '+esc(metrics.cancelledCount)+' 次 · 修复 '+esc(metrics.repairCount)+' 次<br>协调调用 '+esc(metrics.coordinationCallCount)+' 次 · '+(metrics.coordinationMs===null?'耗时未记录':esc((metrics.coordinationMs/1000).toFixed(1))+' 秒')+'<br>输入 / 输出 Token：'+esc(metrics.usage.inputTokens)+' / '+esc(metrics.usage.outputTokens)+' · 已知 '+esc(metrics.usage.knownCalls)+'/'+esc(metrics.usage.totalCalls)+' 次'+(metrics.usage.complete?'':'（统计不完整）')+(metrics.requestedModels?'<br>实际请求成员：'+(metrics.requestedModels.map(id=>esc(model(id).name)).join('、')||'无'):'')+'<br>提供商回执型号：'+(metrics.actualModels.map(m=>esc(textValue(m))).join('、')||'未记录')+'<br>'+(metrics.modelEvidenceComplete===undefined?'型号回执完整性未记录':'型号回执 '+esc(metrics.returnedModelCalls)+'/'+esc(metrics.callCount)+' 次'+(metrics.modelEvidenceComplete?'（完整）':'（不完整）'))+'<br>费用未知，未据此计算成本收益。</p>';
    if(arm?.url)html+='<a class="context-link" href="'+esc(arm.url)+'" target="_blank" rel="noopener noreferrer">打开这组会话与回放 ↗</a>';
    html+='</details>';
   }
   if(record.comparison&&!invalid)html+='<p><b>'+esc(record.comparison.observedOutcome)+'</b><br>验收通过率差 '+(record.comparison.qualityDeltaPoints===null?'未形成':esc(record.comparison.qualityDeltaPoints)+' 个百分点')+' · 单模型 / 多模型耗时比 '+(record.comparison.speedupRatio===null?'未形成':esc(record.comparison.speedupRatio))+'</p><p class="muted">'+esc(record.comparison.notice||'')+'</p>';
   if(record.comparison?.participationNotice&&!invalid)html+='<p class="muted">'+esc(record.comparison.participationNotice)+'</p>';
   if(record.comparison?.efficiency&&!invalid){const efficiency=record.comparison.efficiency;html+='<details class="team-business"><summary>独立效率记录 · 整体对照</summary><p>历时减少比例：'+(efficiency.durationReductionPct===null?'暂无匹配基线':esc(efficiency.durationReductionPct)+'%')+'<br>整体效率分：'+(efficiency.globalPoints===null?'不计分':esc(efficiency.globalPoints))+'</p><p>依据：'+esc(efficiency.basis)+'</p><p class="muted">'+esc(efficiency.notice)+'</p></details>';}
   if(record.error)html+='<p class="native-warning">'+esc(record.error)+'</p>';
   if(['running','queued'].includes(record.status))html+='<button class="context-link" data-comparison-stop="'+esc(record.id)+'">停止该批次</button>';
   html+='</section>';
  }
  return html;
 }
 function comparisonSummariesHtml(groups){
  if(!groups.length)return '';
  return '<section class="team-acceptance"><h3>同条件重复汇总</h3><p class="muted">案例、输入、评测器、模型配置与配置岗位规范完全相同才归为一组；不跨组平均。</p>'+groups.map(g=>'<details class="team-business"><summary>'+esc(g.title)+' · '+esc(g.assessedPairs)+'/'+esc(g.scheduledPairs)+' 组已评测</summary><p>主模型：'+esc(model(g.coordinatorModel).name)+'<br>成员：'+g.teamModels.map(id=>esc(model(id).name)).join('、')+'<br>未完成 '+esc(g.incompletePairs)+' 组<br>两臂达标 '+esc(g.bothPassed)+' · 仅单模型达标 '+esc(g.singleOnlyPassed)+' · 仅多模型达标 '+esc(g.multiOnlyPassed)+' · 两臂均未达标 '+esc(g.neitherPassed)+'</p><p>平均验收差：'+(g.meanQualityDeltaPoints===null?'未形成':esc(g.meanQualityDeltaPoints)+' 个百分点')+'<br>单模型 / 多模型耗时比中位数：'+(g.medianSpeedupRatio===null?'未形成':esc(g.medianSpeedupRatio))+' · 来自 '+esc(g.matchedSuccessTimingPairs)+' 组双方达标且有完整计时的记录</p>'+(g.globalPoints!==undefined?'<p>整体效率分：'+(g.globalPoints===null?'不计分':esc(g.globalPoints))+' · 历时减少比例中位数：'+(g.medianDurationReductionPct===null?'暂无匹配基线':esc(g.medianDurationReductionPct)+'%')+'</p>':'')+'<p class="muted">'+esc(g.notice)+'</p><small class="mono">组标识 '+esc(g.groupId)+'<br>记录 '+g.recordIds.map(esc).join('、')+'</small></details>').join('')+'</section>';
 }
 function controlledContentHtml(data,caseId){
  const rows=(data?.records||[]).filter(r=>r.caseId===caseId),value=(v,digits=2)=>v===null||v===undefined?'未知':esc(typeof v==='number'?Number(v.toFixed(digits)):v);
  if(!rows.length)return '<p class="muted">暂无交付对照。相同案例的多次运行只衡量稳定性，不增加独立任务样本数。</p>';
  return rows.map(r=>{
   const q=r.quantification||{},p=r.process||{},a=r.arms||{},res=q.resources||{},audit=q.budgetAudit||{};
   let html='<details class="team-business" open><summary>'+esc(r.title||r.caseId)+' · '+esc(r.status==='completed'?'实验结束':statusNames[r.status]||r.status)+'</summary><p class="muted">'+esc(r.created||'')+' · '+esc(r.id)+' · '+(r.homogeneous===false||r.selection?.homogeneous===false?'异构小组，模型差异也是变量':'同构模型，独立智能体上下文')+'</p>';
   if(r.evaluationNotice)html+='<p class="native-warning"><b>协议无效记录 · 不参与优劣汇总</b><br>'+esc(r.evaluationNotice)+'</p>';
   html+='<table class="controlled-results"><thead><tr><th>记录</th><th>SAS 单智能体</th><th>MAS 协作小组</th></tr></thead><tbody>';
   for(const [title,read] of [['执行状态',arm=>esc(arm?.status==='completed'?'已完成':arm?.status==='failed'?'请求失败，未交付':statusNames[arm?.status]||'未执行')],[r.evaluationNotice?'历史原始验收（协议无效）':'固定验收',arm=>arm?.evaluation?esc(arm.evaluation.passed)+' / '+esc(arm.evaluation.total)+(arm.status&&arm.status!=='completed'?'（无最终交付）':''):'未完成'],['调用数',arm=>value(arm?.metrics?.callCount,0)],['实际输入 / 输出 Token',arm=>value(arm?.metrics?.usage?.inputTokens,0)+' / '+value(arm?.metrics?.usage?.outputTokens,0)+(arm?.metrics?.usage?.complete?'':'（不完整）')],['墙钟时间（秒）',arm=>value(arm?.metrics?.wallMs===undefined?null:arm.metrics.wallMs/1000)]])html+='<tr><td>'+title+'</td><td>'+read(a.single)+'</td><td>'+read(a.multi)+'</td></tr>';
   html+='</tbody></table><p>'+ (r.budget?.mode==='provider_default'?'额度策略：不设任务总量，不按阶段分摊；沿用协议单次技术参数。<br>未控制计算预算，不能视为等预算比较。':'历史每臂声明输出额度：'+value(r.budget?.cap,0)+' Token<br>等额输出上限：'+(audit.equalOutputAllowance?'已记录':'待核对')+'；完整思考量与账单成本仍需独立证据。')+'<br>验收差：'+value(q.qualityDeltaPoints)+' 个百分点 · 总 Token 开销比 COR：'+value(res.resourceOverheadRatio)+'</p>';
   html+='<p>每成功任务 Token（含失败消耗）：SAS '+value(res.tokensPerSuccessfulTask?.single?.value)+' / MAS '+value(res.tokensPerSuccessfulTask?.multi?.value)+'<br>实际费用 / 成功任务：SAS '+value(res.costPerSuccessfulTask?.single?.value)+' / MAS '+value(res.costPerSuccessfulTask?.multi?.value)+(q.egt?.enabled===false?'<br>成本阈值已关闭；费用由用户按提供商账单判断。':'<br>历史 EGT Token 代理差值：'+value(q.egt?.tokenProxyMargin)+'（不是费用收益判定）')+'</p>';
   if(q.decision)html+='<p class="native-warning"><b>严格优势判定：'+(q.decision.provesSuperiority?'已满足条件':'证据不足，不能宣称 MAS 更优')+'</b><br>'+esc((q.decision.reasons||[]).join('；'))+'</p>';
   if(p.lexicalDiversity){const d=p.lexicalDiversity,g=p.criterionGains;html+='<h4>过程诊断</h4><p>消息边词法多样性：'+value(d.value)+' · 同级候选差异：'+value(d.peerValue)+'<br>测得 '+esc(d.measuredEdges)+' / '+esc(d.totalEdges)+' 条交接边；高低均不直接计分。<br>UPR：未测，缺少路径贡献标签或消融。</p>';
    if(g?.status==='observed')html+='<p>最终结果相对最佳成员候选：'+value(g.netOverBestCandidate,0)+' 项<br>所有候选都没通过、汇总后通过：'+esc(g.newBeyondAllCandidates.join('、')||'无')+'<br>候选曾通过、汇总后丢失：'+esc(g.lostFromAnyCandidate.join('、')||'无')+'</p>';
    html+='<p class="muted">'+esc(d.notice)+' '+esc(p.notice||'')+'</p>';
   }
   for(const name of ['single','multi'])if(a[name]?.error)html+='<p class="native-warning">'+esc(name==='single'?'SAS':'MAS')+'：'+esc(a[name].error)+'</p>';
   html+='<details><summary>算法字段、调用回执与来源索引</summary><pre>'+esc(JSON.stringify({quantification:q,process:p,arms:a},null,2))+'</pre></details><a class="context-link" href="/api/studio/controlled-export?'+esc(new URLSearchParams({project:r.projectId,id:r.id}).toString())+'">下载本次请求、产物与验收证据</a>';
   if(['running','queued'].includes(r.status))html+='<button class="context-link" data-controlled-stop="'+esc(r.id)+'">停止本批实验</button>';
   return html+'</details>';
  }).join('')+'<details><summary>同条件汇总（不把重复案例当独立样本）</summary><pre>'+esc(JSON.stringify(data.summaries||[],null,2))+'</pre></details>';
 }
 async function refreshComparisons(){const params=new URLSearchParams({project:pid}),[data,controlled]=await Promise.all([api('/comparisons?'+params),api('/controlled-experiments?'+params)]),selection=nextTeamSelection(),chosen=$('comparisonCase').value,cases=data.cases||[data.case];comparisonData={...data,controlled};$('comparisonCase').innerHTML=cases.map(c=>'<option value="'+esc(c.id)+'">'+esc(c.title)+'</option>').join('');$('comparisonCase').value=cases.some(c=>c.id===chosen)?chosen:data.case.id;$('comparisonTeam').textContent='当前小组：'+model(selection.coordinatorModel).name+' 主导 · '+selection.teamModels.map(id=>model(id).name).join('、');$('comparisonContent').innerHTML=comparisonContentHtml(data,$('comparisonCase').value);$('controlledContent').innerHTML=controlledContentHtml(controlled,$('comparisonCase').value);}
 async function openComparisons(){await refreshComparisons();$('comparisonFeedback').textContent='';$('comparisonDialog').showModal();}
 function customRoleDefinitionHtml(definition){
  if(!definition)return '';
  return '<details class="team-business"><summary>岗位定义 · '+esc(definition.title)+'</summary><p><b>业务内容与位置</b><br>'+esc(definition.businessContext)+'</p><p><b>职责要求</b><br>'+esc(definition.responsibilities)+'</p><p><b>交付要求</b><br>'+esc(definition.deliverables)+'</p><p><b>验收要求</b><br>'+esc(definition.acceptance)+'</p>'+(definition.baseRoleType?'<p>继承岗位：'+esc(roleNames[definition.baseRoleType]||definition.baseRoleType)+'</p>':'')+'</details>';
 }
 function mcpBindingHtml(binding){
  if(!binding)return '';
  const tools=binding.servers.flatMap(s=>s.tools.map(name=>s.serverId+'/'+name));
  return '<p class="muted">本轮岗位工具：'+(binding.mode==='workspace'?'工作区共享':'按岗位分配')+' · '+(tools.map(esc).join('、')||(binding.mode==='roles'?'未分配':'本轮无可用工具'))+(binding.sourceRoleId&&binding.sourceRoleId!==binding.roleId?' · 继承 '+esc(binding.sourceRoleId):'')+'<br>以上为本轮冻结范围，实际调用以工具回执为准。</p>';
 }
 function workFrameworkHtml(framework,binding=framework?.mcpBinding){
  if(!framework)return mcpBindingHtml(binding);
  const f=framework,relations=rows=>rows.map(r=>esc(r.title||r.moduleId)+' · '+esc(r.roleId)).join('<br>')||'无';
  return '<details class="team-business"><summary>当前模块岗位框架 · '+esc(f.roleTitle||roleTitle(f.roleId))+'</summary>'+mcpBindingHtml(binding)+customRoleDefinitionHtml(f.roleDefinition)+'<p><b>目标</b><br>'+esc(f.objective)+'</p><p><b>本岗工作</b><br>'+esc(f.instructions)+'</p>'+(f.roleType?'<p>基础岗位类型：'+esc(roleTitle(f.roleType))+'</p>':'')+'<p><b>计划输入范围</b><br>'+f.inputs.map(i=>esc(i.moduleId)+'：'+i.expectedPaths.map(esc).join('、')).join('<br>')+'</p><p class="muted">这是计划期望的输入范围。实际交接文件、版本和指纹以交接记录为准。</p><p><b>输出范围</b><br>'+f.outputs.map(esc).join('<br>')+'</p><p><b>工作边界</b><br>'+f.boundaries.map(esc).join('<br>')+'</p><p><b>完成标准</b><br>'+f.completionCriteria.map(esc).join('<br>')+'</p><p>上游：'+relations(f.upstream)+'<br>下游：'+relations(f.downstream)+'</p><p>可联系：'+(f.communication.contacts.map(c=>esc(c.roleId)+(c.moduleId?' · '+esc(c.moduleId):'')).join('、')||'无')+'<br>澄清最多 '+esc(f.communication.maxClarificationRounds)+' 轮 · 每轮最多 '+esc(f.communication.maxQuestions)+' 个问题<br>讨论上限 '+esc(f.communication.discussionRoundLimit)+' 轮</p></details>';
 }
 function guideInheritanceHtml(inheritance,sources){
  if(!inheritance)return '';
  return '<details class="team-business"><summary>本次岗位规范继承</summary><p>'+inheritance.roleIds.map(esc).join(' → ')+'</p><p>'+esc(inheritance.precedence)+'</p>'+sources.filter(s=>['role_rules','configured_role_rules'].includes(s.kind)).map(s=>'<details><summary>'+esc(s.path)+' · '+(s.status==='loaded'?'已载入':'未载入')+'</summary><p class="mono">版本 '+esc(s.revision)+(s.sha256?' · SHA-256 '+esc(s.sha256):'')+'</p>'+(s.text?'<pre>'+esc(s.text)+'</pre>':'')+'</details>').join('')+'</details>';
 }
 function renderTeam(){
  const r=current(),config=currentTeamConfig(),modules=r?.plan?.modules||[],nodes=r?.nodes||[];
  const savedRoles=!playback&&!historicalRun()&&Array.isArray(project?.team?.roles)?project.team.roles.filter(x=>x.latest||x.acceptedSamplesInView>0||(x.roleId||x.id)==='coordinator'):[];
  const roster=new Map(savedRoles.map(x=>[x.roleId||x.id,{...x,id:x.roleId||x.id,title:roleDefinition(x.roleId||x.id,r)?.title||x.title,model:x.latest?.model||x.model,modules:[]}]));
  if(!roster.has('coordinator'))roster.set('coordinator',{id:'coordinator',title:'总负责人',model:coordinatorId(r),modules:[]});
  const coordinatorNode=[...nodes].reverse().find(n=>n.agent&&(n.roleId==='coordinator'||(!n.roleId&&['draft','integration'].includes(n.phase))));
  const coordinator=roster.get('coordinator');coordinator.model=coordinatorNode?.agent||coordinatorId(r);
  for(const m of modules){const id=m.roleId||m.id,role=roster.get(id)||{id,title:roleTitle(id),modules:[]};role.modules.push(m);if(m.assignment?.model)role.model=m.assignment.model;roster.set(id,role);}
  for(const n of nodes.filter(n=>['review','verification'].includes(n.phase)&&n.roleId)){const id=n.roleId,role=roster.get(id)||{id,title:roleTitle(id),model:n.agent,modules:[]},m=modules.find(m=>m.id===n.moduleId);if(m&&!role.modules.some(x=>x.id===m.id))role.modules.push(m);roster.set(id,role);}
  const selected=nodes.find(n=>n.id===selectedNode)||nodes.find(n=>n.status==='running')||nodes.filter(n=>n.status==='done').at(-1);
  const module=modules.find(m=>m.id===selected?.moduleId||'build-'+m.id===selected?.id),relatedModule=module?.id||selected?.moduleId;
  const parentNodes=selected?(selected.deps||[]).map(id=>nodes.find(n=>n.id===id)).filter(Boolean):[];
  const nextNodes=selected?nodes.filter(n=>(n.deps||[]).includes(selected.id)):[];
  const visibleNodeIds=new Set(nodes.filter(n=>n.status==='done'||n.status==='running').map(n=>n.id));
  const visibleSeq=Math.max(0,...(session?.events||[]).map(e=>e.seq||0));
  const discussions=(r?.discussions||[]).filter(d=>!playback||(Number.isFinite(d.eventSeq)?d.eventSeq<=visibleSeq:visibleNodeIds.has(d.nodeId||d.node)));
  const relevant=relatedModule?discussions.filter(d=>d.moduleId===relatedModule):discussions;
  const contract=r?.taskContracts?.[relatedModule];
  const handoffs=(r?.handoffs||[]).filter(h=>(!relatedModule||h.target.moduleId===relatedModule)&&(!playback||h.eventSeq<=visibleSeq&&nodes.some(n=>n.id===h.nodeId&&n.status!=='waiting')));
  const acceptedRows=(!playback||nodes.some(n=>n.id===(r.finalNodeId||'integration')&&n.status==='done'))?(r?.final?.moduleAcceptance||[]):[];
  const history=!playback&&!historicalRun()?(project?.team?.memories||[]):[];
  const relationList=items=>items.length?items.map(n=>esc(n.title)+' · '+esc(roleTitle(n.roleId||n.agent))).join('<br>'):'无';
  let html='<div class="team-intro"><small class="eyebrow">同一业务 · 分工协作</small><h3>项目专项小组</h3><p>'+(r?.workflowVersion===2?'常规工作直接执行；复杂任务按岗位有限讨论，再由 '+esc(coordinatorName(r))+' 验收。':r?'当前为历史流程记录。项目角色规范可继续配置，新流程使用新的协作规则。':'先补充共同业务说明，再由 '+esc(coordinatorName(r))+' 按模块职责组成小组。')+'</p></div>';
  html+='<details class="team-business"><summary>共同业务说明'+(config.businessBrief?' · 已配置':' · 待补充')+'</summary><p>'+esc(config.businessBrief||'尚未配置独立的业务说明。可在「项目资料与约定」中补充用户、完整业务流程、关键数据和模块关系。')+'</p></details><div class="team-roster">';
  for(const role of [...roster.values()].sort((a,b)=>(a.id==='coordinator'?-2:a.modules.length?-1:0)-(b.id==='coordinator'?-2:b.modules.length?-1:0))){
   html+='<section class="team-role"><div class="assignment">'+(role.model?logo(role.model):'<span class="role-placeholder">◇</span>')+'<strong>'+esc(roleDefinition(role.id,r)?.title||role.title||roleTitle(role.id,r))+'</strong><small>'+(role.model?(role.modules.length||role.id==='coordinator'?'':'最近由 ')+esc(model(role.model).name):'待分派')+'</small></div>';
   html+='<p>'+(role.modules.length?role.modules.map(m=>esc(m.title)).join(' · '):role.id==='coordinator'?'全局规划、争议裁决、完成度验收':'已有项目角色；本轮暂无负责模块')+'</p>';
   html+=customRoleDefinitionHtml(roleDefinition(role.id,r));
   if(config.roleGuides?.[role.id])html+='<details><summary>角色规范</summary><p>'+esc(config.roleGuides[role.id])+'</p></details>';
   if(role.models?.length>1)html+='<small class="muted">历史承担模型：'+role.models.map(id=>esc(model(id).name)).join('、')+'</small>';
   for(const m of role.modules)html+='<button class="context-link" data-team-node="'+esc(nodes.filter(n=>n.moduleId===m.id&&n.roleId===role.id).at(-1)?.id||nodes.find(n=>n.moduleId===m.id&&n.phase==='build')?.id||'build-'+m.id)+'">'+esc(m.title)+' · 查看位置与协作 ↗</button>';
   html+='</section>';
  }
  html+='</div>'+(r?.plan?.businessUnderstanding?'<details class="team-business"><summary>'+esc(coordinatorName(r))+' 对本次业务的理解</summary><p>'+esc(r.plan.businessUnderstanding)+'</p></details>':'')+'<section class="team-location"><h3>当前工作位置</h3>';
  if(selected){html+='<p><b>'+esc(selected.title)+'</b><br><small>'+esc(phaseName(selected))+' · '+esc(statusNames[selected.status]||selected.status)+(selected.round?' · 本模块第 '+esc(selected.round)+'/3 轮':'')+'</small></p><dl><dt>接收上游</dt><dd>'+relationList(parentNodes)+'</dd><dt>交给下游</dt><dd>'+relationList(nextNodes)+'</dd></dl>';if(module)html+='<p>验收目标：'+(module.acceptance||[]).map(x=>esc(textValue(x))).join('；')+'</p>';}
  else html+='<p class="muted">执行后显示各角色的位置、输入和交付对象。</p>';
  if(contract&&(!playback||contract.eventSeq<=visibleSeq)){
   html+='<details class="team-business"><summary>执行前固定的验收标准 · '+contract.criteria.length+' 项</summary><p>来源项目版本 <span class="mono">'+esc(contract.sourceRevision)+'</span><br>依据节点 '+esc(contract.sourceNodeId)+'</p>';
   if(contract.reviewRequirementVersion===1)html+=reviewRequirementHtml(contract.reviewRequirements,contract.reviewSchedule);
   for(const c of contract.criteria)html+='<p><b>'+esc(c.id)+'</b> · '+esc(c.text)+(c.required?'（必需）':'')+'</p>';
   html+='<p class="mono">契约 '+esc(contract.id)+'<br>指纹 <span title="'+esc(contract.fingerprint)+'">'+esc(contract.fingerprint.slice(0,16))+'…</span></p></details>';
   html+=workFrameworkHtml(contract.workFramework,contract.mcpBinding||contract.workFramework?.mcpBinding);
   if(contract.workerToolsVersion===1)html+=workerBindingHtml(contract.workerBinding);
  }
  html+='</section>';
  if(r?.handoffs){
   html+='<section class="team-discussions"><h3>'+(relatedModule?'当前模块':'本轮')+'交接记录 <small>'+handoffs.length+'</small></h3>';
   for(const h of handoffs){
    const receiver=nodes.find(n=>n.id===h.nodeId),sameRequest=receiver?.contextRef===h.contextRef;
    let state='已装入请求';
    if(!sameRequest)state='历史请求';
    else if(receiver.status==='done'&&receiver.responseValidation==='passed')state=receiver.result?.files?.length?'交付通过结构校验':'回复通过结构校验';
    else if(receiver.status==='failed')state=receiver.responseValidation==='failed'?'回复未通过校验':'请求未完成';
    else if(['stopped','interrupted'].includes(receiver.status))state='请求中断';
    html+='<details class="team-discussion"><summary>'+esc(model(h.target.model).name)+' · '+esc(state)+'</summary><p>项目版本 '+esc(h.projectRevision)+'<br>交接单 '+esc(h.id)+'<br>验收契约 '+esc(h.contractId)+'</p>';
    for(const source of h.sources){
     html+='<p><b>'+esc(modules.find(m=>m.id===source.moduleId)?.title||source.moduleId)+'</b> · '+esc(model(source.model).name)+'<br>来源节点 '+esc(source.nodeId)+'</p>';
     for(const file of source.files)html+='<p><span class="mono">'+esc(file.path)+'</span><br>'+file.size+' 字节 · SHA-256 <span class="mono" title="'+esc(file.sha256)+'">'+esc(file.sha256.slice(0,16))+'…</span></p>';
    }
    if(!h.sources.length)html+='<p>无上游产物，本次读取项目资料与执行要求。</p>';
    html+='<button class="context-link" data-context="'+esc(h.contextRef)+'">查看这次交接的实际上下文 ↗</button></details>';
   }
   if(!handoffs.length)html+='<p class="muted">开始执行后显示实际交接的版本、文件与验收标准。</p>';
   html+='</section>';
  }
  html+=(nodes||[]).filter(node=>!relatedModule||node.moduleId===relatedModule).map(node=>workerEvidenceHtml(node,r?.id)).join('');
  html+=evidenceReviewHtml(r,visibleSeq)+runtimeBuildHtml(r)+hostAcceptanceHistoryHtml(r,relatedModule,visibleSeq)+qualityHistoryHtml(r,relatedModule,visibleSeq,nodes)+contributionHistoryHtml(r,relatedModule,visibleSeq);
  html+='<section class="team-discussions"><h3>'+(relatedModule?'当前模块的':'本轮')+'定向讨论 <small>'+relevant.length+'</small></h3>';
  if(relevant.length)for(const d of relevant.slice(-24)){
   html+='<article class="team-discussion"><div><b>'+esc(roleTitle(d.fromRole))+' → '+esc(roleTitle(d.toRole))+'</b><small>'+(d.round?'第 '+esc(d.round)+' 轮':esc(d.status||'已记录'))+'</small></div><p>'+esc(textValue(d.message))+'</p>';
   if(d.kind==='reflection_prompt')html+='<small>固定主持提问 · 未调用模型生成</small>';
   if(d.kind==='reflection')html+='<p><b>深层回应</b><br>建议做法：'+esc(d.proposal)+'<br>验证方式：'+esc(d.verification)+'</p>';
   if(d.evidence?.length)html+='<details><summary>讨论依据</summary><p>'+esc(textValue(d.evidence))+'</p></details>';
   if(d.respondsTo)html+='<small>回应 '+esc(d.respondsTo)+'</small>';
   html+='</article>';
  }else html+='<p class="muted">暂无定向讨论。常规任务无需为形成对话而增加讨论。</p>';
  html+='</section><section class="team-acceptance"><h3>'+esc(coordinatorName(r))+' 完成度验收</h3>';
  if(acceptedRows.length)for(const a of acceptedRows){
   html+='<div class="acceptance-row '+(a.accepted?'accepted':'rejected')+'"><strong>'+(a.accepted?'✓ 已接纳':'待修正')+' · '+esc(modules.find(m=>m.id===a.moduleId)?.title||a.moduleId)+'</strong><p>'+(Array.isArray(a.evidence)?a.evidence:[a.evidence]).filter(Boolean).map(x=>esc(textValue(x))).join('<br>')+'</p>';
   if(a.contractId)html+='<details><summary>按固定标准逐项核对</summary>'+a.criteria.map(c=>'<p><b>'+esc(c.criterionId)+'</b> · '+esc({met:'满足',unmet:'未满足',unverified:'未验证'}[c.status])+'<br>'+esc(c.criterion)+'<br>'+c.evidence.map(esc).join(' · ')+'</p>').join('')+'</details>';
   html+='</div>';
  }
  else html+='<p class="muted">尚未形成逐模块验收记录。模型自述与实际检查证据分别保留。</p>';
  html+='</section><section class="team-history"><h3>角色已接纳的项目记忆</h3>';
  if(history.length)for(const m of history.slice(-10).reverse()){const source=m.source||{};html+='<div class="memory-item"><small>'+esc(roleTitle(m.roleId||m.role))+' · '+date(m.recordedAt||m.time||m.created)+'</small><p>'+esc(m.summary||m.acceptanceSummary||'已接纳的项目交付')+'</p><small>版本 '+esc((source.revision||m.revision||m.projectRevision||'').slice(0,12))+(source.session||m.sessionId||m.session?' · 来源会话 '+esc(source.session||m.sessionId||m.session):'')+'</small></div>';}
  else html+='<p class="muted">'+(r?.mode==='demo'?'演练不写入正式角色记忆，不影响模型项目经验。':playback?'回放仅展示当时已经发生的节点与讨论，不混入项目最新的角色记忆。':historicalRun()?'历史轮次使用当时的项目规范，不混入项目最新的角色记忆。':'完整交付验收并保存后，角色记忆才进入后续会话。')+'</p>';
  $('teamPane').innerHTML=html+'</section>'+projectContributionHtml()+projectMetricsHtml()+roleCooperationHtml()+comparisonEntryHtml();
 }

 const triggerModes={observe:'观察（不自动调用模型）',review:'研判后人工接单',auto:'按授权自动接单'};
 const triggerStates={active:'已激活',awaiting_resume:'待显式恢复',collecting:'等待合并/评估',evaluating:'正在研判',decided:'已形成决定',discarded:'已作废',unknown:'结果未确认',attached:'已关联任务',pending:'待派工',dispatching:'正在接单',dispatched:'已接单',reconciled:'已核对关联',accept:'建议接单',ignore:'忽略',needs_input:'需补充信息',failed:'失败',running:'运行中',completed:'已结束'};
 const triggerCount=value=>Number.isFinite(value)?String(value):'未知';
 function triggerWhen(value){const d=new Date(typeof value==='number'?value*1000:value);return Number.isFinite(d.getTime())?d.toLocaleString('zh-CN'):'时间未记录';}
 function renderTriggerPanel(){
  const projectId=project?.id||pid;
  if(triggerProjectId!==projectId){triggerProjectId=projectId;triggerData=null;triggerEdit=null;triggerTargetDraft=null;$('triggerSourceEditor').hidden=true;$('triggerToken').value='';}
  $('newTriggerSource').disabled=!projectId;
  $('triggerStats').innerHTML=triggerData?triggerStatsHtml(triggerData):'<p class="muted">点击刷新读取本项目来源与记录，不会因此调用模型。</p>';
  $('triggerSources').innerHTML=(triggerData?.sources||[]).map(source=>'<section class="team-business"><b>'+esc(source.name)+'</b> · '+esc({github:'GitHub',custom:'本机事件',timer:'定时'}[source.kind]||source.kind)+' · '+esc(triggerModes[source.mode]||source.mode)+'<p>策略版本 '+esc(source.revision)+' · '+(source.enabled?'来源已启用':'来源已停用')+' · '+esc(triggerStates[source.runtimeState]||source.runtimeState||'状态未返回')+'</p>'+(source.kind==='github'?'<p class="mono">'+esc(source.config.repository)+' · '+esc(source.config.branch)+'</p>':'')+'<div class="settings-actions"><button type="button" data-trigger-edit="'+esc(source.id)+'">设置</button><button type="button" data-trigger-toggle="'+esc(source.id)+'">'+(source.enabled?'停用来源':source.mode==='observe'?'启用观察':'启用（后续可能计费）')+'</button>'+(source.kind!=='custom'?'<button type="button" data-trigger-poll="'+esc(source.id)+'"'+(!source.enabled?' disabled':'')+'>同步读取</button>':'')+(source.runtimeState==='awaiting_resume'?'<button type="button" data-trigger-resume="'+esc(source.id)+'">恢复来源'+(source.mode==='observe'?'':'（可能计费）')+'</button>':'')+'</div></section>').join('')||(triggerData?'<p class="muted">本项目尚未配置事件来源。</p>':'');
  renderTriggerCandidates();renderTriggerEvents();renderTriggerEffects();renderTriggerConnection();renderTriggerJobs();renderTriggerSubscriptions();
 }
 function triggerStatsHtml(data){
  const s=data.stats||{},pending=(data.candidates||[]).filter(row=>['collecting','awaiting_resume'].includes(row.status)).length;
  return '<div class="model-form-grid"><p>唯一事件 '+triggerCount(s.received)+'<br>规则过滤 '+triggerCount(s.filtered)+'（这一层不调用模型）</p><p>候选事件 '+triggerCount(s.candidates)+'<br>当前列表待评估批次 '+pending+'</p><p>第二层研判模型调用 '+triggerCount(s.modelCalls)+'<br>已接单 '+triggerCount(s.dispatched)+'</p><p>研判输入Token '+triggerCount(s.inputTokens)+'<br>研判输出Token '+triggerCount(s.outputTokens)+(s.usageComplete===false?'<br><small>用量不完整；已知输入 '+triggerCount(s.knownInputTokens)+'、已知输出 '+triggerCount(s.knownOutputTokens)+'，不补零。</small>':'')+'</p></div><p class="muted">模型用量仅属事件研判，Worker全链用量在对应任务查看。评估会按上限抽样与截断，不代表每条事件都被模型读取。</p>';
 }
 async function refreshTriggers(){
  const projectId=project?.id||pid;if(!projectId)throw Error('请先选择本地项目。');
  $('triggerFeedback').textContent='正在读取来源与观察记录…';
  try{const value=await api('/triggers?'+new URLSearchParams({project:projectId}));if((project?.id||pid)!==projectId)return;triggerProjectId=projectId;triggerData=value;renderTriggerPanel();$('triggerFeedback').textContent='已刷新本地记录；刷新不会启动模型评估。';}
  catch(e){$('triggerFeedback').textContent=e.message;throw e;}
 }
 function renderTriggerKind(){
  const kind=$('triggerKind').value,mode=$('triggerMode').value;
  $('triggerGithubFields').hidden=kind!=='github';$('triggerCustomFields').hidden=kind!=='custom';$('triggerTimerFields').hidden=kind!=='timer';
  $('triggerActorsLabel').textContent=kind==='github'?'作者/CI触发者白名单 · 每行一个':'允许用户 · 每行一个';$('triggerEditorPermission').hidden=kind!=='github';$('triggerAllowUnverifiedEditors').disabled=kind!=='github';
  $('triggerAllowDraftPr').disabled=kind!=='github';if(kind!=='github')$('triggerAllowDraftPr').checked=false;
  $('triggerSubscriptionEnabled').disabled=mode!=='auto';if(mode!=='auto')$('triggerSubscriptionEnabled').checked=false;
  $('triggerModeNotice').textContent=mode==='observe'?'观察模式不自动调用模型或派工；手动评估须显式点击计费按钮。':mode==='review'?'启用后，符合规则的新候选会自动调用评估模型并可能计费；决定仍须人工接单。':'启用后，符合规则的新候选可能计费评估，并仅在所选动作与路径内自动创建任务；草稿PR还须质量核验。';
  renderTriggerConnection();
 }
 function beginTriggerSource(id=null){
  if(!project)throw Error('请先选择项目。');
  const source=id?(triggerData?.sources||[]).find(row=>row.id===id):null;if(id&&!source)throw Error('来源已变化，请刷新。');
  triggerEdit=source?JSON.parse(JSON.stringify(source)):{id:null,revision:0,kind:'github',mode:'observe',enabled:false,policy:{},config:{},target:{projectId:project.id||pid,coordinatorModel:null,teamModels:[],taskRoute:{mode:'team'},allowedPaths:[],allowedActions:[]},evaluatorModel:null};
  triggerTargetDraft=JSON.parse(JSON.stringify(triggerEdit.target));const policy=triggerEdit.policy||{},config=triggerEdit.config||{};
  $('triggerSourceEditor').hidden=false;$('triggerSourceEditor').open=true;$('triggerEditorTitle').textContent=source?'编辑来源 · '+source.name+' · 版本 '+source.revision:'新增来源 · 默认仅观察';
  $('triggerSubscriptionEnabled').checked=source?.subscription?.enabled===true;$('triggerName').value=source?.name||'';$('triggerKind').value=triggerEdit.kind;$('triggerMode').value=triggerEdit.mode;$('triggerEnabled').checked=!!triggerEdit.enabled;
  $('triggerRepository').value=config.repository||'';$('triggerBranch').value=config.branch||'main';$('triggerPollSeconds').value=String(config.pollSeconds??120);
  $('triggerCustomActor').value=config.actor||'local';$('triggerCustomContainer').value=config.container||'custom';$('triggerCustomType').value=config.eventType||'custom.message';
  $('triggerDueAt').value=config.dueAt||'';$('triggerInterval').value=config.intervalSeconds==null?'':String(config.intervalSeconds);
  for(const [id,key] of [['triggerActors','actors'],['triggerContainers','containers'],['triggerEventTypes','eventTypes'],['triggerSelfActors','selfActors'],['triggerExcludeActors','excludeActors']])$(id).value=(policy[key]||[]).join('\n');
  $('triggerAllowUnverifiedEditors').checked=policy.allowUnverifiedEditors===true;$('triggerAllowBots').checked=policy.allowBots===true;$('triggerRules').value=(policy.rules||[]).map(rule=>(rule.enabled===false?'!':'')+rule.id+' | '+rule.description).join('\n');triggerRuleInitial=$('triggerRules').value;
  $('triggerDebounce').value=String(policy.debounceSeconds??10);$('triggerCooldown').value=String(policy.cooldownSeconds??120);
  $('triggerContractSummary').innerHTML=triggerContractHtml(triggerEdit.target.taskRoute);$('triggerAllowedPaths').value=(triggerEdit.target.allowedPaths||[]).join('\n');$('triggerAllowTask').checked=triggerEdit.target.allowedActions?.includes('create_task')||false;$('triggerAllowDraftPr').checked=triggerEdit.target.allowedActions?.includes('draft_pr')||false;
  const choices=index.catalog.models.filter(m=>canCoordinate(m)&&eligibleModel(m.id));if(triggerEdit.evaluatorModel&&!choices.some(m=>m.id===triggerEdit.evaluatorModel))choices.push({...model(triggerEdit.evaluatorModel),id:triggerEdit.evaluatorModel});
  $('triggerEvaluator').innerHTML='<option value="">未选择（观察可留空）</option>'+choices.map(m=>'<option value="'+esc(m.id)+'">'+esc(m.name)+(eligibleModel(m.id)?'':' · 当前不可派工')+'</option>').join('');$('triggerEvaluator').value=triggerEdit.evaluatorModel||'';
  $('triggerToken').value='';$('triggerCustomPayload').value='';$('triggerSaveFeedback').textContent='';renderTriggerTeam();renderTriggerKind();
 }
 function renderTriggerTeam(){const team=triggerTargetDraft||{};$('triggerTeamSummary').textContent=team.coordinatorModel?'后续主模型：'+model(team.coordinatorModel).name+'；成员：'+(team.teamModels||[]).map(id=>model(id).name).join('、'):'后续任务小组尚未选择，不能自动派工。';}
 function triggerContractHtml(route){
  if(route?.mode!=='direct')return '<p class="muted">当前使用小组规划。先在任务输入区选择单Worker路线并填写岗位、范围和验收标准，再将合同用于此来源。</p>';
  const contract=route.contract;
  return '<p>接单岗位 '+esc(roleTitle(contract.roleId))+'</p><p>范围 '+contract.scope.map(esc).join('、')+'</p><details><summary>固定要求与验收标准</summary><p>'+esc(contract.instructions||'')+'</p>'+contract.acceptance.map(item=>'<p>'+esc(item)+'</p>').join('')+'</details>';
 }
 function useTriggerDirectContract(){
  if(!triggerEdit)throw Error('请先打开来源设置。');
  const route=taskRoutePayload();
  if(route?.mode!=='direct')throw Error('请先在任务输入区选择单Worker路线并填写完整合同。');
  triggerTargetDraft={...(triggerTargetDraft||{}),taskRoute:JSON.parse(JSON.stringify(route))};
  $('triggerAllowedPaths').value=route.contract.scope.join('\n');
  $('triggerContractSummary').innerHTML=triggerContractHtml(route);
 }
 function useTriggerTeam(){const team=nextTeamSelection();if(!team.coordinatorModel||!team.teamModels.length||team.teamModels.some(id=>!eligibleModel(id)))throw Error('请先在工作小组选择可派工型号。');triggerTargetDraft={...(triggerTargetDraft||{}),...JSON.parse(JSON.stringify(team))};renderTriggerTeam();}
 function triggerSourcePayload(){
  if(!triggerEdit||!project)throw Error('请先选择或新建来源设置。');
  const lines=id=>$(id).value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean),kind=$('triggerKind').value,mode=$('triggerMode').value;
  const rules=$('triggerRules').value===triggerRuleInitial?JSON.parse(JSON.stringify(triggerEdit.policy?.rules||[])):lines('triggerRules').map(line=>{const pos=line.indexOf('|');if(pos<1||!line.slice(pos+1).trim())throw Error('规则请按“规则ID | 说明”填写。');let id=line.slice(0,pos).trim(),enabled=!id.startsWith('!');if(!enabled)id=id.slice(1).trim();return {id,description:line.slice(pos+1).trim(),enabled};});
  if(new Set(rules.map(r=>r.id)).size!==rules.length||rules.length>32||rules.reduce((n,r)=>n+r.description.length,0)>16000)throw Error('规则编号不可重复，最多32条且说明总量不超过16000字符。');
  const evaluator=$('triggerEvaluator').value||null,paths=lines('triggerAllowedPaths'),actions=[$('triggerAllowTask').checked?'create_task':null,$('triggerAllowDraftPr').checked?'draft_pr':null].filter(Boolean);
  if(mode!=='observe'&&(!evaluator||!rules.some(rule=>rule.enabled)))throw Error('review/auto须明确评估型号及启用规则；后续评估可能计费。');
  if(mode==='auto'&&(!actions.includes('create_task')||!paths.length||!triggerTargetDraft?.coordinatorModel||!triggerTargetDraft.teamModels?.includes(triggerTargetDraft.coordinatorModel)))throw Error('自动接单须明确创建任务、允许路径和工作小组。');
  const subscribed=$('triggerSubscriptionEnabled').checked===true;
  if(subscribed&&(mode!=='auto'||triggerTargetDraft?.taskRoute?.mode!=='direct'))throw Error('订阅直通需要自动模式和完整单Worker合同。');
  let config;if(kind==='github')config={repository:$('triggerRepository').value.trim(),branch:$('triggerBranch').value.trim(),pollSeconds:Number($('triggerPollSeconds').value)};
  else if(kind==='custom')config={actor:$('triggerCustomActor').value.trim(),container:$('triggerCustomContainer').value.trim(),eventType:$('triggerCustomType').value.trim()};
  else config={dueAt:$('triggerDueAt').value.trim(),intervalSeconds:$('triggerInterval').value.trim()===''?null:Number($('triggerInterval').value)};
  return {id:triggerEdit.id,revision:triggerEdit.revision,name:$('triggerName').value.trim(),kind,enabled:$('triggerEnabled').checked===true,mode,config,...(subscribed||triggerEdit.subscription?{subscription:{version:1,enabled:subscribed}}:{}),
   policy:{actors:lines('triggerActors'),containers:lines('triggerContainers'),eventTypes:lines('triggerEventTypes'),allowBots:$('triggerAllowBots').checked===true,allowUnverifiedEditors:kind==='github'&&$('triggerAllowUnverifiedEditors').checked===true,selfActors:lines('triggerSelfActors'),excludeActors:lines('triggerExcludeActors'),rules,debounceSeconds:Number($('triggerDebounce').value),maxBatchEvents:triggerEdit.policy?.maxBatchEvents??100,cooldownSeconds:Number($('triggerCooldown').value)},
   target:{...JSON.parse(JSON.stringify(triggerTargetDraft)),projectId:project.id||pid,allowedPaths:paths,allowedActions:actions},evaluatorModel:evaluator};
 }
 async function saveTriggerSource(){try{const source=triggerSourcePayload(),existingIds=new Set((triggerData?.sources||[]).map(row=>row.id));triggerData=await api('/triggers/source/save',{source});renderTriggerPanel();const saved=(triggerData.sources||[]).find(row=>row.id===source.id)||(!source.id?(triggerData.sources||[]).find(row=>!existingIds.has(row.id)&&row.name===source.name&&row.kind===source.kind):null);if(saved)beginTriggerSource(saved.id);$('triggerSaveFeedback').textContent='来源策略已保存；权限只来自已保存的规则、动作与路径。';}catch(e){$('triggerSaveFeedback').textContent=e.message;throw e;}}
 async function toggleTriggerSource(id){const source=(triggerData?.sources||[]).find(row=>row.id===id);if(!source)throw Error('来源已变化，请刷新。');const payload={};for(const key of ['schema','id','revision','name','kind','mode','config','policy','target','evaluatorModel','subscription'])if(source[key]!==undefined)payload[key]=source[key];payload.enabled=!source.enabled;triggerData=await api('/triggers/source/save',{source:payload});renderTriggerPanel();}

 function renderTriggerConnection(){
  const source=triggerEdit?.id?(triggerData?.sources||[]).find(row=>row.id===triggerEdit.id):null,github=source?.kind==='github';
  $('triggerGithubConnection').hidden=!github;$('triggerCustomSubmit').hidden=source?.kind!=='custom';
  for(const id of ['triggerConnectToken','triggerConnectGit','triggerDisconnect'])$(id).disabled=!github;
  if(github){const connection=(triggerData?.githubConnection?.repositories||[]).find(row=>row.repository===source.config.repository);$('triggerGithubStatus').textContent=source.config.repository+' · '+(connection?.connected?'凭据已接入当前服务内存':'未连接')+(connection?.status?' · '+textValue(connection.status):'')+'。重启后需要重新连接。';}
 }
 async function connectTriggerGithub(useGitCredential=false){
  const tokenValue=$('triggerToken').value.trim();$('triggerToken').value='';
  const source=triggerEdit?.id?(triggerData?.sources||[]).find(row=>row.id===triggerEdit.id):null;
  if(!source||source.kind!=='github')throw Error('请先保存并选择GitHub来源。');
  if(source.revision!==triggerEdit.revision)throw Error('来源已更新，请重新打开设置后连接。');
  if(!useGitCredential&&!tokenValue)throw Error('请填写Token，或显式选择本机Git凭据。');
  try{triggerData=await api('/triggers/github/connect',{sourceId:source.id,...(useGitCredential?{useGitCredential:true}:{token:tokenValue})});renderTriggerPanel();const connection=(triggerData.githubConnection?.repositories||[]).find(row=>row.repository===source.config.repository);$('triggerFeedback').textContent=connection?.connected?'GitHub凭据已接入本次服务内存；连接操作只验证访问，来源随后按保存的模式处理。':'尚未连接，请查看状态；本机无可用凭据时需提供Token。';}
  catch(e){$('triggerFeedback').textContent=e.message;throw e;}
 }
 async function disconnectTriggerGithub(){if(!triggerEdit?.id)throw Error('请先选择来源。');$('triggerToken').value='';triggerData=await api('/triggers/github/disconnect',{sourceId:triggerEdit.id});renderTriggerPanel();$('triggerFeedback').textContent='仓库连接已清除，来源策略与历史证据保留。';}
 function triggerCandidateState(batch){
  const source=(triggerData?.sources||[]).find(row=>row.id===batch?.sourceId),decision=(triggerData?.decisions||[]).find(row=>row.batchId===batch?.id),ready=!!source?.enabled&&source.runtimeState==='active'&&source.revision===batch?.sourceRevision;
  return {source,decision,canEvaluate:ready&&['collecting','awaiting_resume'].includes(batch.status)&&!!source.evaluatorModel&&source.policy.rules?.some(rule=>rule.enabled),canDispatch:ready&&decision?.status==='accept'&&source.target.allowedActions?.includes('create_task')&&!!source.target.allowedPaths?.length};
 }
 function triggerCanPublish(effect){
  const source=(triggerData?.sources||[]).find(row=>row.id===effect?.sourceId);
  return source?.kind==='github'&&source.enabled&&source.runtimeState==='active'&&source.revision===effect?.sourceRevision&&source.target.allowedActions?.includes('draft_pr')&&effect.task?.status==='completed'&&['dispatched','reconciled'].includes(effect.status)&&(!effect.publication||effect.publication.status==='unknown')&&triggerData?.jobs?.['publish:'+effect.id]?.status!=='running';
 }
 async function triggerAction(action,id){
  const source=(triggerData?.sources||[]).find(row=>row.id===id),batch=(triggerData?.candidates||[]).find(row=>row.id===id),effect=(triggerData?.effects||[]).find(row=>row.id===id);
  let payload;
  if(action==='resume'){if(!source)throw Error('来源不存在');payload={sourceId:id,revision:source.revision};}
  else if(action==='poll'){if(!source?.enabled||source.runtimeState!=='active')throw Error('来源未启用或需要显式恢复。');payload={sourceId:id};}
  else if(action==='publish'){if(!effect||!triggerCanPublish(effect))throw Error('没有当前策略的草稿PR授权或已确认交付的任务。');payload={effectId:id};}
  else if(action==='evaluate'){if(!batch||!triggerCandidateState(batch).canEvaluate)throw Error('候选当前不可评估，请核对型号、启用规则和策略版本。');payload={candidateId:id};}
  else if(action==='dispatch'){if(!batch||!triggerCandidateState(batch).canDispatch)throw Error('没有有效接单决定或缺少创建任务/路径授权。');payload={candidateId:id};}
  else throw Error('不支持的Trigger操作。');
  $('triggerFeedback').textContent=action==='evaluate'?'正在提交显式计费评估，仅形成决定，不自动派工…':action==='publish'?'正在请求核验并创建授权草稿PR…':'正在提交操作…';
  try{triggerData=await api('/triggers/'+action,payload);renderTriggerPanel();$('triggerFeedback').textContent=action==='evaluate'?'评估请求已提交，以实际调用和决定记录为准；本次不自动接单。':'操作已提交，请刷新查看实际结果与错误。';}
  catch(e){$('triggerFeedback').textContent=e.message;throw e;}
 }
 async function sendTriggerCustom(){
  if(!triggerEdit?.id||triggerEdit.kind!=='custom')throw Error('请先保存并选择本机事件来源。');
  let payload;try{payload=JSON.parse($('triggerCustomPayload').value);}catch{throw Error('请填写合法事件JSON。');}
  triggerData=await api('/triggers/custom',{sourceId:triggerEdit.id,payload});renderTriggerPanel();$('triggerFeedback').textContent='本机事件已提交，是否放行取决于已保存规则，正文不提供权限。';
 }
 function renderTriggerCandidates(){
  $('triggerCandidates').innerHTML=(triggerData?.candidates||[]).length?'<h4>候选与第二层研判</h4>'+triggerData.candidates.map(batch=>{const state=triggerCandidateState(batch),body=state.decision?.decision||{};
   return '<section class="team-business"><b>'+esc(state.source?.name||batch.sourceId)+'</b> · '+esc(triggerStates[batch.status]||batch.status)+'<p>候选 '+esc(batch.id)+' · 合并 '+esc(batch.eventCount??'未知')+' 条事件 · 策略版本 '+esc(batch.sourceRevision)+'</p><p class="mono">线程 '+esc(batch.threadKey)+'</p>'+(state.decision?'<p>决定：'+esc(triggerStates[state.decision.status]||state.decision.status)+' · '+esc(body.reason||'')+'<br>引用规则 '+(body.matchedRuleIds||[]).map(esc).join('、')+' · 实际引用 '+(body.evidenceEventIds||[]).length+' 个事件</p>':'')+'<div class="settings-actions"><button type="button" data-trigger-evaluate="'+esc(batch.id)+'"'+(!state.canEvaluate?' disabled':'')+'>手动评估（计费，仅决定）</button><button type="button" data-trigger-dispatch="'+esc(batch.id)+'"'+(!state.canDispatch?' disabled':'')+'>按授权接单</button></div></section>';
  }).join(''):'';
 }
 function renderTriggerEvents(){
  $('triggerEvents').innerHTML=(triggerData?.events||[]).map(event=>'<article class="finding"><b>'+esc(event.title||event.eventType)+'</b><small>'+esc(triggerWhen(event.occurredAt||event.receivedAt))+' · '+esc(event.actor)+(event.origin?.actorBasis==='author_unverified_editor'?'（原作者；编辑者未验证）':event.origin?.actorBasis==='creator'?'（创建者）':event.origin?.actorBasis==='workflow_actor'?'（CI触发者）':'')+' · '+esc(event.container)+'</small><p>'+esc(event.gate==='rejected'?'规则过滤：'+event.gateReason+'（零模型调用）':'已进入候选：'+(triggerStates[event.status]||event.status))+'</p><details><summary>外部正文（资料，不是权限）</summary><pre>'+esc(String(event.body||'').slice(0,1200))+'</pre>'+(String(event.body||'').length>1200?'<small>正文仅显示节选，不是完整模型输入。</small>':'')+'</details></article>').join('')||(triggerData?'<p class="muted">暂无已记录事件。</p>':'');
 }
 function triggerPublicationHtml(effect){
  const publication=effect.publication;if(!publication)return '';
  const status=publication.status,url=publication.pullRequest?.url||'',source=(triggerData?.sources||[]).find(row=>row.id===effect.sourceId);
  const expected='https://github.com/'+(source?.config?.repository||'')+'/pull/';
  const link=status==='completed'&&url.startsWith(expected)&&/^\d+$/.test(url.slice(expected.length))?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">查看草稿PR ↗</a>':'';
  return '<p><b>草稿PR发布：'+esc({completed:'已确认创建',blocked:'已阻断',unknown:'结果未确认'}[status]||status)+'</b>'+ (link?'<br>'+link:'')+(publication.error?'<br>'+esc(textValue(publication.error)):'')+'</p>'+(status==='unknown'?'<p class="muted">继续发布会先核对已发步骤；无法确认时不会重发，确认后才继续尚未发送的步骤，仍可能产生外部操作。</p>':'');
 }
 function renderTriggerEffects(){
  $('triggerEffects').innerHTML=(triggerData?.effects||[]).length?'<h4>实际接单状态</h4>'+triggerData.effects.map(effect=>{const task=effect.task||{},publication=effect.publication;
   return '<section class="team-business"><b>'+esc(triggerStates[effect.status]||effect.status)+'</b><p>接单记录 '+esc(effect.id)+' · 子任务状态 '+esc(statusNames[task.status]||task.status||'尚未确认')+'</p>'+(task.projectId&&task.sessionId?'<a href="/studio?'+esc(new URLSearchParams({project:task.projectId,session:task.sessionId}).toString())+'">查看任务与全链用量 ↗</a>':'')+(effect.error?'<p class="native-warning">'+esc(textValue(effect.error))+'</p>':'')+triggerPublicationHtml(effect)+'<div class="settings-actions"><button type="button" data-trigger-publish="'+esc(effect.id)+'"'+(!triggerCanPublish(effect)?' disabled':'')+'>'+(publication?.status==='unknown'?'核对并继续发布（外部操作）':publication?.status==='completed'?'草稿PR已创建':publication?.status==='blocked'?'发布已阻断':'核验并创建草稿PR（外部操作）')+'</button></div><small>接单不等于交付；质量未通过不能发布，未知结果不自动重试。</small></section>';
  }).join(''):'';
 }
 function renderTriggerSubscriptions(){
  const rows=triggerData?.subscriptions||[];
  const states={active:'订阅中',paused:'已暂停',released:'已解除',blocked:'需处理',awaiting_resume:'待恢复'};
  $('triggerSubscriptions').innerHTML=rows.map(row=>{
   const sub=row.subscription||row,task=row.task||{};
   const link='/studio?'+new URLSearchParams({project:task.projectId||'',session:task.sessionId||''});
   return '<section class="team-business"><b>'+esc(row.threadKey)+'</b> · '+esc(states[sub.status]||sub.status||'未知')+'<p>待处理 '+esc(row.pendingCount??0)+' · 已处理 '+esc(row.consumedCount??0)+'</p>'+((sub.blockedReason||sub.reason)?'<p>'+esc(sub.blockedReason||sub.reason)+'</p>':'')+'<a href="'+esc(link)+'">查看原任务与Worker ↗</a><div class="settings-actions">'+(sub.status==='released'?'':(sub.status==='paused'?'<button type="button" data-sub-action="resume"':'<button type="button" data-sub-action="pause"')+' data-sub-source="'+esc(row.sourceId)+'" data-sub-thread="'+esc(row.threadKey)+'">'+(sub.status==='paused'?'恢复订阅（可能计费）':'暂停订阅')+'</button><button type="button" data-sub-action="release" data-sub-source="'+esc(row.sourceId)+'" data-sub-thread="'+esc(row.threadKey)+'">解除认领</button>')+'</div></section>';
  }).join('')||'<p class="muted">启用来源订阅后，首次接单会建立Worker归属。后续事件在此显示投递进度。</p>';
  const sources=new Set((triggerData?.sources||[]).filter(s=>s.subscription?.enabled&&s.mode==='auto').map(s=>s.id));
  const owners=new Map(rows.filter(row=>sources.has(row.sourceId)&&(row.subscription?.status||row.status)!=='released').map(row=>[row.origin,row]));
  $('triggerClaimOrigin').innerHTML=[...owners.values()].map(row=>'<option value="'+esc(row.origin)+'">'+esc(row.threadKey)+' · '+esc(row.task?.sessionId||row.origin)+'</option>').join('');
  const threads=new Map((triggerData?.events||[]).filter(row=>sources.has(row.sourceId)&&row.gate==='candidate').map(row=>[JSON.stringify([row.sourceId,row.threadKey,row.container]),row]));
  $('triggerClaimThread').innerHTML=[...threads.entries()].map(([key,row])=>'<option value="'+esc(key)+'">'+esc(row.threadKey)+' · '+esc(row.container)+'</option>').join('');
  $('triggerClaimForm').hidden=!owners.size||!threads.size;
 }
 async function updateTriggerSubscription(sourceId,threadKey,action){
  const row=(triggerData?.subscriptions||[]).find(row=>row.sourceId===sourceId&&row.threadKey===threadKey);
  if(!row)throw Error('订阅已变化，请刷新。');
  triggerData=await api('/triggers/subscription/update',{sourceId,threadKey,revision:row.sourceRevision,origin:row.origin,version:row.subscription?.version??row.version,action});
  renderTriggerPanel();
 }
 async function claimTriggerThread(){
  const origin=$('triggerClaimOrigin').value;
  const [sourceId,threadKey,container]=JSON.parse($('triggerClaimThread').value||'[]');
  const owner=(triggerData?.subscriptions||[]).find(row=>row.origin===origin&&row.sourceId===sourceId&&(row.subscription?.status||row.status)!=='released');
  if(!owner)throw Error('请选择同一来源中的任务和事件串。');
  triggerData=await api('/triggers/subscription/claim',{sourceId,origin,threadKey,container,revision:owner.sourceRevision});
  renderTriggerPanel();$('triggerClaimFeedback').textContent='事件串已认领，后续输入交给原Worker。';
 }
 function renderTriggerJobs(){
  const jobs=Object.entries(triggerData?.jobs||{});
  $('triggerJobs').innerHTML=jobs.length?'<details><summary>后台操作状态</summary>'+jobs.map(([key,job])=>'<p><b>'+esc(key)+' · '+esc(triggerStates[job.status]||job.status)+'</b>'+(job.error?'<br>'+esc(textValue(job.error)):'')+'<br><small>'+esc(triggerWhen(job.endedAt||job.startedAt))+'</small></p>').join('')+'</details>':'';
 }
 function renderProjectMcp(){
  const scoped=mcpPolicyDraft?.mode==='roles',selected=mcpPolicyDraft?.serverIds||[],servers=index.mcp?.servers||[];
  let html='<h3>项目 MCP 工具</h3><label>使用范围<select id="projectMcpMode"><option value="workspace"'+(!scoped?' selected':'')+'>'+(mcpPolicyDraft?'工作区共享':'兼容旧项目：工作区共享')+'</option><option value="roles"'+(scoped?' selected':'')+'>按项目与岗位分配</option></select></label>';
  html+='<p class="muted">'+(scoped?'先选择本项目可用的服务，再为下方岗位勾选具体工具。取消服务会移除所有岗位对该服务的绑定。':'当前执行岗位沿用工作区已连接的 MCP 工具。普通保存不会自动切换；选择按岗位分配后需明确配置并保存。')+'</p>';
  if(scoped){
   const ids=[...new Set([...servers.map(s=>s.id),...selected])];
   html+='<fieldset class="team-member-group"><legend>项目可用服务</legend>'+ids.map(id=>{
    const server=servers.find(s=>s.id===id),checked=selected.includes(id),ready=server?.status==='ready';
    return '<label class="team-member"><input type="checkbox" data-project-mcp="'+esc(id)+'"'+(checked?' checked':'')+(!ready&&!checked?' disabled':'')+'><span><b>'+esc(server?.name||id)+'</b><small>'+(ready?'已连接 · '+server.tools.length+' 个工具':'未连接 · 请先在“连接模型”中检测服务')+'</small></span></label>';
   }).join('')+(ids.length?'':'<p class="muted">请先在右上角“连接模型 → MCP 工具连接”中注册并检测服务。</p>')+'</fieldset>';
  }
  html+='<p class="muted">项目负责人仅规划与验收，不直接执行 MCP。更改用于后续执行，进行中的任务沿用启动时配置。</p>';
  $('projectMcpSettings').innerHTML=html;
  $('projectMcpSettings').onchange=e=>{
   const input=e.target;
   if(input.id==='projectMcpMode')mcpPolicyDraft={...(mcpPolicyDraft||{serverIds:[],roleTools:{}}),mode:input.value};
   else if(input.dataset.projectMcp){
    const id=input.dataset.projectMcp;
    mcpPolicyDraft.serverIds=input.checked?[...mcpPolicyDraft.serverIds,id]:mcpPolicyDraft.serverIds.filter(s=>s!==id);
    if(!input.checked)for(const role of Object.keys(mcpPolicyDraft.roleTools))mcpPolicyDraft.roleTools[role]=mcpPolicyDraft.roleTools[role].filter(binding=>binding.serverId!==id);
   }
   renderProjectMcp();renderRoleMcp();
  };
 }
 function roleMcpSelection(){
  const role=editedRoleId,definition=roleDefinition(role,null),bindings=mcpPolicyDraft?.roleTools||{};
  const base=definition?.baseRoleType||project?.team?.roles?.find(r=>(r.roleId||r.id)===role)?.roleType;
  const independent=Object.hasOwn(bindings,role),inheritable=!!definition||!Object.hasOwn(roleNames,role);
  return {base,independent,inheritable,bindings:bindings[role]||bindings[base]||[]};
 }
 function renderRoleMcp(){
  let html='<h4>该岗位的 MCP 工具</h4>';
  if(editedRoleId==='coordinator')html+='<p class="muted">负责人通过岗位分派使用工具；此处不授予负责人直接执行权限。</p>';
  else if(mcpPolicyDraft?.mode!=='roles')html+='<p class="muted">工作区共享模式。先在上方切换为“按项目与岗位分配”，再设置该岗位。</p>';
  else{
   const selection=roleMcpSelection(),{base,independent,inheritable,bindings}=selection,inherited=inheritable&&!independent,servers=index.mcp?.servers||[];
   if(inheritable)html+='<label>工具配置方式<select id="roleMcpMode"><option value="inherit"'+(inherited?' selected':'')+'>继承基础岗位'+(base?' · '+esc(roleTitle(base,null)):' · 由任务声明')+'</option><option value="independent"'+(!inherited?' selected':'')+'>独立工具清单</option></select></label>';
   html+='<p class="muted">'+(inherited?(base?'当前继承 '+esc(roleTitle(base,null))+' 的工具；切换独立清单后可单独修改。':'尚未声明基础岗位；任务明确基础岗位后继承其工具，否则无 MCP 工具。'):'仅可使用已勾选的工具；全部取消表示该岗位不使用 MCP，不会回退到共享工具。')+'</p>';
   const ids=mcpPolicyDraft.serverIds;
   html+=ids.map(id=>{
    const server=servers.find(s=>s.id===id),chosen=bindings.find(b=>b.serverId===id)?.tools||[],names=[...new Set([...(server?.tools||[]).map(t=>t.name),...chosen])];
    return '<fieldset class="team-member-group"><legend>'+esc(server?.name||id)+(server?.status==='ready'?'':' · 未连接')+'</legend>'+names.map(name=>{
     const tool=server?.tools.find(t=>t.name===name),checked=chosen.includes(name),available=server?.status==='ready'&&!!tool;
     return '<label class="team-member"><input type="checkbox" data-role-mcp-server="'+esc(id)+'" data-role-mcp-tool="'+esc(name)+'"'+(checked?' checked':'')+(inherited||!available&&!checked?' disabled':'')+'><span><b>'+esc(name)+'</b><small>'+esc(available?tool.description||'已发现工具':'未连接或已不在目录中，可取消旧绑定')+'</small></span></label>';
    }).join('')+(names.length?'':'<p class="muted">尚未发现可用工具。</p>')+'</fieldset>';
   }).join('')+(ids.length?'':'<p class="muted">本项目尚未选择 MCP 服务。</p>');
  }
  $('roleMcpSettings').innerHTML=html;
  $('roleMcpSettings').onchange=e=>{
   const input=e.target,role=editedRoleId;
   if(input.id==='roleMcpMode'){
    if(input.value==='inherit')delete mcpPolicyDraft.roleTools[role];
    else mcpPolicyDraft.roleTools[role]=JSON.parse(JSON.stringify(roleMcpSelection().bindings));
   }else if(input.dataset.roleMcpTool){
    const id=input.dataset.roleMcpServer,name=input.dataset.roleMcpTool;
    const rows=mcpPolicyDraft.roleTools[role]||[],binding=rows.find(b=>b.serverId===id)||{serverId:id,tools:[]};
    binding.tools=input.checked?[...binding.tools,name]:binding.tools.filter(t=>t!==name);
    mcpPolicyDraft.roleTools[role]=[...rows.filter(b=>b.serverId!==id),binding].filter(b=>b.tools.length);
   }
   renderRoleMcp();
  };
 }
 function workerRoleSelection(){
  const role=editedRoleId,definition=roleDefinition(role,null),grants=workerPolicyDraft?.roleGrants||{};
  const base=role.startsWith('custom-')?definition?.baseRoleType:(project?.roleDirectory?.roles||[]).find(row=>row.roleId===role)?.roleType||(project?.team?.roles||[]).find(row=>(row.roleId||row.id)===role)?.roleType;
  const exact=Object.hasOwn(grants,role),inheritable=!!definition||!Object.hasOwn(roleNames,role);
  return {base,exact,inheritable,sourceRole:exact?role:base,grant:grants[exact?role:base]||{enabled:false,checkIds:[]}};
 }
 function workerProcessEditor(grant,available,inherited){
  const process=grant.processRequirements||{version:1,initialFailure:null,requireExecutionChild:false},initial=process.initialFailure;
  const disabled=inherited||!grant.enabled,checks=(grant.checkIds||[]).map(id=>available.find(c=>c.id===id)||{id,name:id,criteria:[]});
  if(initial&&!checks.some(c=>c.id===initial.checkId))checks.push({id:initial.checkId,name:initial.checkId+'（已失联）',criteria:[]});
  let html='<fieldset class="team-member-group"><legend>新任务的必做步骤</legend><p class="muted">要求随契约冻结，由宿主执行和核验；旧任务不补做。模型不能用完成声明跳过这些步骤。</p>';
  html+='<label class="team-member"><input id="roleWorkerInitialFailure" type="checkbox"'+(initial?' checked':'')+(disabled||(!checks.length&&!initial)?' disabled':'')+'><span><b>修改前必须复现指定失败</b><small>宿主先检查原始冻结输入。未复现、异常或结果未知时停止，不会先修改再制造失败。</small></span></label>';
  if(initial){
   html+='<label>用于复现的固定检查<select id="roleWorkerBaselineCheck"'+(disabled?' disabled':'')+'>'+checks.map(c=>'<option value="'+esc(c.id)+'"'+(c.id===initial.checkId?' selected':'')+'>'+esc(c.name||c.id)+'</option>').join('')+'</select></label>';
   const check=checks.find(c=>c.id===initial.checkId),criteria=check?.criteria||[],ids=[...new Set([...criteria.map(c=>c.id),...(initial.criterionIds||[])])];
   html+='<p class="muted">明确选择原始输入中必须失败的标准，所选项必须全部复现：</p>'+ids.map(id=>{const c=criteria.find(x=>x.id===id),checked=(initial.criterionIds||[]).includes(id);return '<label class="team-member"><input type="checkbox" data-worker-baseline-criterion="'+esc(id)+'"'+(checked?' checked':'')+(disabled||(!c&&!checked)?' disabled':'')+'><span>'+esc(id)+' · '+esc(c?.description||'标准已失联，请撤销或重新选择')+'</span></label>';}).join('');
  }
  html+='<label class="team-member"><input id="roleWorkerRequireChild" type="checkbox"'+(process.requireExecutionChild?' checked':'')+(disabled||(!grant.childExecution&&!process.requireExecutionChild)?' disabled':'')+'><span><b>必须经执行子任务、完整检查和接纳</b><small>先授予执行子任务权限。每个接纳的变更文件须被父Worker完整查看，接回后由父重新测试。</small></span></label></fieldset>';
  return html;
 }
 function renderRoleWorker(){
  if(!$('roleWorkerSettings'))$('roleMcpSettings').insertAdjacentHTML('afterend','<section id="roleWorkerSettings" class="team-business" aria-label="岗位Worker文件与固定脚本"></section>');
  const forbidden=editedRoleId==='coordinator'||editedRoleId==='reviewer'||editedRoleId.startsWith('reviewer-');
  let html='<h4>该岗位的文件与固定脚本</h4>';
  if(forbidden)html+='<p class="muted">主负责人和复核者不获得Worker执行权限；由执行岗位在本模块范围内操作。</p>';
  else{
   const selected=workerRoleSelection(),grant=selected.grant,inherited=selected.inheritable&&!selected.exact;
   const available=index.hostAcceptance?.checks||[],projectIds=project?.hostAcceptanceIds||[],ids=[...new Set([...projectIds,...(grant.checkIds||[])])];
   html+='<p class="muted">允许在本模块范围内读、搜、修改，并运行选中的可信固定脚本；不提供任意命令或网络工具。仅在新轮启动时冻结，已有轮次保留原授权。</p>';
   if(!workerPolicyDraft)html+='<p class="muted">本项目尚未配置Worker授权；普通保存不会自动启用。</p>';
   if(selected.inheritable)html+='<label>授权来源<select id="roleWorkerMode"><option value="inherit"'+(inherited?' selected':'')+'>继承基础岗位'+(selected.base?' · '+esc(roleTitle(selected.base,null)):' · 须在任务中明确声明')+'</option><option value="exact"'+(!inherited?' selected':'')+'>单独设置本岗位</option></select></label>';
   if(inherited)html+='<p class="muted">'+(selected.base?'当前继承 '+esc(roleTitle(selected.base,null)):'当前未记录基础岗位，不能按名称推断授权')+'；单独关闭本岗位可阻止继承。</p>';
   html+='<label class="team-member"><input id="roleWorkerEnabled" type="checkbox"'+(grant.enabled?' checked':'')+(inherited?' disabled':'')+'><span><b>启用本岗位Worker工具</b><small>必须选择至少一项项目已保存的固定脚本；关闭或空授权不提供工具。</small></span></label>';
   html+='<label class="team-member"><input id="roleWorkerChildExecution" type="checkbox"'+(grant.childExecution===true?' checked':'')+(inherited||!grant.enabled?' disabled':'')+'><span><b>允许Worker派生执行子任务</b><small>子任务沿用同型号，在独立副本中修改和运行已授权固定脚本；父Worker接纳后重验。默认只允许只读分析。</small></span></label>';
   html+='<fieldset class="team-member-group"><legend>允许在本岗位执行的固定预检</legend>'+ids.map(id=>{
    const check=available.find(row=>row.id===id),checked=(grant.checkIds||[]).includes(id),usable=projectIds.includes(id)&&!!check&&index.hostAcceptance?.supported===true;
    return '<label class="team-member"><input type="checkbox" data-worker-check="'+esc(id)+'"'+(checked?' checked':'')+(inherited||(!grant.enabled&&!checked)||(!usable&&!checked)?' disabled':'')+'><span><b>'+esc(check?.name||id)+'</b><small>'+(usable?(check.files||[]).map(esc).join('、'):'不在项目选择中或本机授权已失联；只能撤销原选择')+'</small></span></label>';
   }).join('')+(ids.length?'':'<p class="muted">请先在上方选择宿主检查器并保存项目验收选择，再配置岗位授权。</p>')+'</fieldset>'+workerProcessEditor(grant,available,inherited)+'<p class="muted">本地预检不等于最终交付通过，之后仍需正式宿主验收和固定质量关卡。移除项目检查器前，请先撤销对应岗位脚本并保存。</p>';
  }
  $('roleWorkerSettings').innerHTML=html;
  $('roleWorkerSettings').onchange=e=>{
   if(forbidden)return;
   const input=e.target,selection=workerRoleSelection(),role=editedRoleId;
   if(!workerPolicyDraft)workerPolicyDraft={version:1,roleGrants:{}};
   if(input.id==='roleWorkerMode'){
    if(input.value==='inherit')delete workerPolicyDraft.roleGrants[role];
    else workerPolicyDraft.roleGrants[role]=JSON.parse(JSON.stringify(selection.grant));
   }else if(input.id==='roleWorkerEnabled'){
    if(selection.inheritable&&!selection.exact)return;
    workerPolicyDraft.roleGrants[role]=input.checked?{...selection.grant,enabled:true,checkIds:[...(selection.grant.checkIds||[])]}:{enabled:false,checkIds:[]};
   }else if(input.id==='roleWorkerChildExecution'){
    if(selection.inheritable&&!selection.exact||!selection.grant.enabled)return;
    workerPolicyDraft.roleGrants[role]={...selection.grant,childExecution:!!input.checked};
   }else if(['roleWorkerInitialFailure','roleWorkerBaselineCheck','roleWorkerRequireChild'].includes(input.id)||input.dataset?.workerBaselineCriterion){
    if(selection.inheritable&&!selection.exact||!selection.grant.enabled)return;
    const grant=JSON.parse(JSON.stringify(selection.grant)),process=grant.processRequirements||{version:1,initialFailure:null,requireExecutionChild:false};
    if(input.id==='roleWorkerInitialFailure'){
     if(input.checked){if(!grant.checkIds?.length)return;process.initialFailure={checkId:grant.checkIds[0],criterionIds:[]};}else process.initialFailure=null;
    }else if(input.id==='roleWorkerBaselineCheck'){
     if(!grant.checkIds?.includes(input.value)||!process.initialFailure)return;
     process.initialFailure={checkId:input.value,criterionIds:[]};
    }else if(input.id==='roleWorkerRequireChild'){
     if(input.checked&&grant.childExecution!==true)return;
     process.requireExecutionChild=!!input.checked;
    }else{
     if(!process.initialFailure)return;
     const id=input.dataset.workerBaselineCriterion,check=(index.hostAcceptance?.checks||[]).find(c=>c.id===process.initialFailure.checkId);
     if(input.checked&&!check?.criteria.some(c=>c.id===id))return;
     process.initialFailure.criterionIds=input.checked?[...new Set([...process.initialFailure.criterionIds,id])]:process.initialFailure.criterionIds.filter(c=>c!==id);
    }
    if(!process.initialFailure&&!process.requireExecutionChild)delete grant.processRequirements;else grant.processRequirements=process;
    workerPolicyDraft.roleGrants[role]=grant;
   }else if(input.dataset.workerCheck){
    if(selection.inheritable&&!selection.exact||!selection.grant.enabled&&input.checked)return;
    const id=input.dataset.workerCheck,allowed=(project?.hostAcceptanceIds||[]).includes(id)&&(index.hostAcceptance?.checks||[]).some(row=>row.id===id)&&index.hostAcceptance?.supported===true;
    if(input.checked&&!allowed)return;
    workerPolicyDraft.roleGrants[role]={...selection.grant,enabled:!!selection.grant.enabled,checkIds:input.checked?[...new Set([...(selection.grant.checkIds||[]),id])]:(selection.grant.checkIds||[]).filter(value=>value!==id)};
   }
   renderRoleWorker();
  };
 }
 function workerPolicyPayload(){
  if(!workerPolicyDraft)return null;
  const copy=JSON.parse(JSON.stringify(workerPolicyDraft)),projectIds=project?.hostAcceptanceIds||[],checks=index.hostAcceptance?.checks||[];
  for(const [role,grant] of Object.entries(copy.roleGrants)){
   if(role==='coordinator'||role==='reviewer'||role.startsWith('reviewer-'))throw Error('主负责人和复核者不能获得Worker执行授权。');
   if(grant.enabled&&!grant.checkIds.length)throw Error('启用Worker前，请为 '+roleTitle(role,null)+' 选择至少一项固定脚本。');
   if(grant.checkIds.some(id=>!projectIds.includes(id)||!checks.some(check=>check.id===id)))throw Error('岗位脚本不在项目已保存的验收范围，或本机授权已失联；请先撤销该选择。');
   const process=grant.processRequirements;
   if(process){
    if(!grant.enabled)throw Error('必做步骤必须绑定已启用的Worker岗位。');
    if(process.requireExecutionChild&&grant.childExecution!==true)throw Error('先授予执行子任务权限，或明确取消必需子任务步骤。');
    const baseline=process.initialFailure;
    if(baseline){
     const check=checks.find(c=>c.id===baseline.checkId);
     if(!grant.checkIds.includes(baseline.checkId)||!check)throw Error('初始失败检查须属于本岗位已选固定脚本。');
     if(!baseline.criterionIds?.length||baseline.criterionIds.some(id=>!check.criteria.some(c=>c.id===id)))throw Error('请选择必须在原始输入中失败的明确验收项。');
    }
   }

  }
  return copy;
 }
 function workerBindingHtml(binding){
  if(!binding)return '';
  return '<details class="team-business"><summary>本轮冻结Worker授权</summary><p>执行岗位：'+esc(binding.roleId)+' · 授权来源：'+esc(binding.sourceRoleId||'未记录')+'</p><p>只可修改：'+(binding.scope||[]).map(esc).join('、')+'</p><p>固定脚本：'+(binding.checks||[]).map(check=>esc(check.name||check.id)+'（'+esc(check.id)+'）').join('、')+'</p><p>派生子任务：'+(binding.childExecution===true?'允许独立副本执行；父Worker接纳后重验':'只读分析')+'</p>'+(binding.processRequirements?'<p>必做步骤：'+(binding.processRequirements.initialFailure?'原始输入须复现 '+esc(binding.processRequirements.initialFailure.checkId)+' 的 '+binding.processRequirements.initialFailure.criterionIds.map(esc).join('、')+' 失败。':'')+(binding.processRequirements.requireExecutionChild?'执行子任务 → 完整检查变更 → 显式接纳 → 父重验。':'')+'</p>':'')+'<p class="muted">只包含文件操作与登记脚本，不提供任意终端。预检记录不替代正式质量关卡；此处展示本轮授权，不使用项目最新设置。</p></details>';
 }
 function captureRoleGuide(){if(editedRoleId)roleGuideDraft[editedRoleId]=$('savedRoleGuide').value;}
 function showRoleGuide(id){editedRoleId=id;$('savedRoleGuide').value=roleGuideDraft[id]||'';const definition=roleDefinition(id,null);$('roleGuideLabel').textContent=roleTitle(id,null)+(definition?' · 额外补充约定（可选）':' · 职责与交付要求');$('savedRoleGuide').placeholder=definition?'正式岗位定义已在下方登记；这里只填写额外补充约定，可留空。':'负责的板块、需要协作的角色、工作方法及验收要求。留空时使用平台默认职责。';$('customRoleEditor').hidden=!definition;if(definition)showCustomRoleEditor(id);renderRoleMcp();renderRoleWorker();const directory=project?.roleDirectory,type=directory?.types.find(r=>r.id===id),role=directory?.roles.find(r=>r.roleId===id);$('roleFrameworkReference').innerHTML=directory?'<details class="team-business"><summary>岗位基础规范参考</summary><p>'+esc(type?.guide||role?.guide||'尚未配置岗位规范。')+(id!=='coordinator'&&!type?'<br>具体岗位在任务规划时选定基础岗位类型；实际继承链在本次上下文中记录。':'')+'</p>'+(role?.file?'<p>岗位文件：<code>'+esc(role.file)+'</code></p>':'')+'<p class="muted">这里展示当前已保存的岗位规范。编辑内容用于后续执行，实际继承版本在请求上下文中保留。</p></details>':'';}
 function showCustomRoleEditor(id=null){
  editedCustomRole=id;const definition=id?roleDefinition(id,null):{};
  $('customRoleEditor').hidden=false;$('customRoleEditor').open=true;$('customRoleEditorTitle').textContent=id?'编辑岗位 · '+definition.title:'新增业务岗位';
  for(const [field,key] of [['customRoleTitle','title'],['customRoleBusiness','businessContext'],['customRoleResponsibilities','responsibilities'],['customRoleDeliverables','deliverables'],['customRoleAcceptance','acceptance']])$(field).value=definition[key]||'';
  $('customRoleBase').innerHTML='<option value="">不继承基础岗位</option>'+(project?.roleDirectory?.types||[]).map(type=>'<option value="'+esc(type.id)+'">'+esc(type.title)+'</option>').join('');$('customRoleBase').value=definition.baseRoleType||'';
  $('customRoleFeedback').textContent='';
 }
 function customRolePayload(){return {project:pid,roleId:editedCustomRole,version:project.version,definition:{title:$('customRoleTitle').value,businessContext:$('customRoleBusiness').value,responsibilities:$('customRoleResponsibilities').value,deliverables:$('customRoleDeliverables').value,acceptance:$('customRoleAcceptance').value,baseRoleType:$('customRoleBase').value}};}
 async function saveCustomRole(){await guarded(async()=>{try{captureRoleGuide();const previous=Object.keys(project.teamConfig?.customRoles||{}),guides={...roleGuideDraft},payload=customRolePayload();project=await api('/project/role',payload);const savedId=payload.roleId||Object.keys(project.teamConfig.customRoles).find(id=>!previous.includes(id));renderRoleEditor(false);roleGuideDraft={...roleGuideDraft,...guides};$('roleGuideSelect').value=savedId;showRoleGuide(savedId);$('customRoleFeedback').textContent='岗位已保存，可在上方岗位列表继续选择和编辑；后续任务将读取此定义。';renderTeam();}catch(e){$('customRoleFeedback').textContent=e.message;throw e;}});}
 function renderRoleEditor(resetMcp=true){
  if(resetMcp){mcpPolicyDraft=project?.teamConfig?.mcpPolicy?JSON.parse(JSON.stringify(project.teamConfig.mcpPolicy)):null;workerPolicyDraft=project?.teamConfig?.workerTools?JSON.parse(JSON.stringify(project.teamConfig.workerTools)):null;}
  renderProjectMcp();renderProjectHostAcceptance(resetMcp);renderTriggerPanel();
  const configured=project?.teamConfig?.roleGuides||{};roleGuideDraft={...configured};
  const ids=[...new Set([...Object.keys(roleNames),...(project?.roleDirectory?.types||[]).map(r=>r.id),...(project?.roleDirectory?.roles||[]).map(r=>r.roleId),...Object.keys(project?.teamConfig?.customRoles||{}),...Object.keys(configured),...(project?.team?.roles||[]).map(r=>r.roleId||r.id),...(current()?.plan?.modules||[]).map(m=>m.roleId).filter(Boolean)])];
  $('roleGuideSelect').innerHTML=ids.map(id=>'<option value="'+esc(id)+'">'+esc(roleTitle(id,null))+(roleDefinition(id,null)?'':' · '+esc(id))+'</option>').join('');$('roleGuideSelect').value='coordinator';showRoleGuide('coordinator');
  const memories=project?.team?.memories||[],roles=project?.team?.roles||[];
  $('settingsTeamSummary').innerHTML='<p class="muted">已保存 '+roles.length+' 个已配置角色 · '+memories.length+' 条已接纳记忆。新规范用于后续执行，历史调用记录保留原版本。</p><button type="button" id="showTeamFromSettings" class="context-link">查看专项小组与协作记录 ↗</button>';
  $('showTeamFromSettings').onclick=()=>{$('settingsDialog').close();showPane('team');};
 }
 function render(){
  const r=current(),c=r?.classification||session?.classification;$('breadcrumb').textContent=project?project.title+' / '+(session?'持续会话':'新会话'):'项目工作台';$('taskTitle').textContent=session?.title||'在项目里，把事情接着做完。';$('taskStatus').className='status '+(r?.status||session?.status||'');$('taskStatus').textContent=(historicalRun()?'历史轮次 · ':'')+(statusNames[r?.status||session?.status]||'准备就绪');
  $('kindBadge').textContent=c?.kind==='audit'?(r&&r.workflowVersion!==2?'只读审查 · 三轮评分':'只读审查 · 按模块分工'):r?.workflowVersion===2?'业务协作 · 按模块分流':c?.kind==='development'?(r?'开发交付 · 三轮互审':'开发任务 · 局部互审'):c?'普通任务 · 直接分工':'按交付物选择流程';$('kindBadge').title=c?.reason||'';
  $('rounds').innerHTML=r?.workflowVersion===2?'<small class="local-review-progress">'+(reviewProgress(r).length?reviewStates(r).filter(v=>v.status==='decided'||v.status==='completed').length+' / '+reviewProgress(r).length+' 个审查模块已收敛':'常规模块直接执行')+'</small>':['development','audit'].includes(c?.kind)?[1,2,3].map(n=>`<span class="${(r?.completedRounds||0)>=n?'done':r?.nodes.some(x=>x.round===n&&x.status==='running')?'active':''}" title="第 ${n} 轮审核">${(r?.completedRounds||0)>=n?'✓':n}</span>`).join(''):'';
  $('phaseHint').textContent=r?.status==='queued'?'等待任务调度 · 尚未开始执行':r?.nodes.some(n=>n.status==='running')?[...new Set(r.nodes.filter(n=>n.status==='running').map(phaseName))].join(' · '):r?.status==='completed'?'已汇入项目':r?.workflowVersion===2?'分派 → 执行 → 验收':'规划 → 分工 → 汇总';
  $('conversationCount').textContent=session?`${session.messages.filter(m=>m.role==='user').length} 次用户输入 · ${Object.keys(session.agents).length} 个独立上下文`:'';
  $('modeNotice').textContent=historicalRun()?'查看历史轮次 · 泳道与执行方案按所选轮次展示，完整会话保留':r?.mode==='demo'?'流程演练 · 固定样例，未调用真实模型':r?.status==='queued'?'本次执行已排队 · 尚未开始执行':r?.taskRouting?.mode==='direct'?'用户明确单模块合同 · 宿主规则路由，保留执行与质量关卡':'项目记忆可以跨会话延续';$('footerStatus').textContent=project?`${project.files.length} 个文件 · ${session?statusNames[session.status]:'等待新任务'}`:'本地项目系统已就绪';
  renderSidebar();renderRelay();renderConversation();graph();renderPlan();renderMemory();renderTeam();renderControls();
 }
 function showPane(name){selectedPane=name;for(const p of ['tree','plan','memory','team'])$(p+'Pane').hidden=p!==name;document.querySelectorAll('[data-pane]').forEach(b=>b.classList.toggle('selected',b.dataset.pane===name));if(name==='tree')graph();if(name==='team')renderTeam();}
 function showDetail(title,html){if(playback&&!playback.paused)pausePlayback();$('detailTitle').textContent=title;$('detailContent').innerHTML=html;$('detailDialog').showModal();}

 function businessContextHtml(m){
  if(!m.roleAssignment)return '';
  const role=m.roleAssignment,pos=m.currentPosition||{},guidance=m.guidanceSnapshot||{},business=m.businessContext||{};
  let html='<h3>业务全貌与角色位置</h3><p><b>'+esc(role.title||roleTitle(role.roleId))+'</b> · '+esc(role.model)+' · '+esc(phases[pos.stage]||pos.stage||'本次任务')+'</p><p>'+esc(business.overview||'')+'</p><p class="muted">'+esc(business.notice||'')+'</p>';
  html+='<details><summary>角色规范与协作对象</summary><p>'+esc(role.guide||'')+'</p><p>负责模块：'+esc(pos.moduleTitle||'全局协调')+'</p><p>上游：'+((pos.upstream||[]).map(x=>esc(x.title||x.moduleId)).join('、')||'无')+'</p><p>下游：'+((pos.downstream||[]).map(x=>esc(x.title||x.moduleId)).join('、')||'无')+'</p><p>可讨论角色：'+((pos.discussionPeers||[]).map(x=>esc(roleTitle(x))).join('、')||'无')+'</p></details>';
  if(m.globalWorkPolicy)html+='<details><summary>本次全局工作规范</summary><pre>'+esc(m.globalWorkInstructions||'本次没有启用附加工作规范。')+'</pre><p class="mono">'+esc(JSON.stringify(m.globalWorkPolicy))+'</p></details>';
  html+=customRoleDefinitionHtml(role.roleDefinition)+workFrameworkHtml(role.workFramework)+guideInheritanceHtml(role.guideInheritance,guidance.sources||[]);
  html+='<details><summary>共同规范的实际加载版本</summary><p>资料版本 '+esc(guidance.projectRevision)+' · 规范版本 '+esc(guidance.metadataVersion)+' · 角色记忆版本 '+esc(guidance.teamMemoryVersion)+'</p><p>'+esc(guidance.policy||'')+'</p><pre>'+esc(JSON.stringify(guidance.sources||[],null,2))+'</pre></details>';
  html+='<details><summary>已继承 '+(m.projectRoleMemory||[]).length+' 条角色验收记忆</summary>'+(m.projectRoleMemory||[]).map(x=>'<p>'+esc(x.summary)+'</p><small>来源会话 '+esc(x.source?.session)+' · 版本 '+esc(x.source?.revision)+'</small>').join('')+'</details>';return html;
 }
 async function contextDetail(ref){if(playback&&!playback.paused)pausePlayback();const c=await api('/context?'+query({ref})),m=c.memory,p=m.project;showDetail(model(c.model).name+' · 本次独立上下文',`<div class="notice">${esc(c.memoryPolicy.independentHistory)} · 自己的历史 ${c.memoryPolicy.ownTurnsIncluded} 次近期历史 + ${c.memoryPolicy.compactedOwnTurns} 次摘要。过长产物使用摘要与文件索引；其他模型的原始历史不混入。</div><p>共享任务 ${esc(c.memoryPolicy.sharedTask)} · 项目版本 ${esc(p.projectRevision)}</p><h3>项目约定</h3><p>${esc(m.projectInstructions||'未设置')}</p>${businessContextHtml(m)}<h3>实际载入 ${p.files.length} 个文件</h3><p class="muted">${esc(p.retrieval)}</p><div class="file-list">${p.files.map(f=>`<div><span class="mono">${esc(f.path)}</span><small>第 ${f.startLine}–${f.endLine} 行 · ${f.includedChars}/${f.totalChars} 字符${f.truncated?' · 已截取':''}</small></div>`).join('')}</div><details><summary>${p.excluded.length} 个未载入项及原因</summary>${p.excluded.map(f=>`<p>${esc(f.path)} · ${esc(f.reason)}</p>`).join('')}</details><details><summary>查看实际请求上下文</summary><pre>${esc(JSON.stringify(c,null,2))}</pre></details>`);}
 function catalog(){const c=index.catalog;$('weightNotice').textContent=c.weightNotice;$('catalogContent').innerHTML=c.models.map(m=>`<section class="model-card"><div class="assignment">${logo(m.id)}<h3>${esc(m.name)}</h3><small>${m.kind==='executor'?'原生执行器':m.profile?'用户能力先验 · '+esc(profileNames[m.profile]||m.profile):m.verified?'官方型号已核实':'别名待核实'}</small></div><p>${esc(m.summary)}</p>${m.kind==='executor'?'<p class="native-caption">沿用本机 Codex 登录；'+esc(nativeConnectedScope())+'。模型按本机设置选择，能力分数用于任务分配。</p>':''}<div class="weights">${Object.entries(m.weights||{}).filter(([,v])=>v).map(([k,v])=>`<div><span>${esc(c.skills[k]||k)}</span><i><b style="width:${v}%;background:${colors[m.id]||'#7b899b'}"></b></i><small>${v}</small></div>`).join('')}</div><p>${(m.sources||[]).filter(s=>c.sources?.[s]).map(s=>`<a href="${esc(c.sources[s].url)}" target="_blank" rel="noopener noreferrer">${esc(c.sources[s].title)} ↗</a>`).join(' · ')}</p></section>`).join('')+`<h3>官方公布的评测线索</h3>${(c.benchmarks||[]).map(b=>`<p>${esc(model(b.model).name)} · ${esc(b.metric)} · ${esc(b.value)}<br><small>${esc(b.scope)}</small></p>`).join('')}`;$('catalogDialog').showModal();}
 async function settings(){if(!pid){$('projectDialog').showModal();return;}project=await api('/project?'+query({}));$('settingsTitle').textContent=project.title;$('savedInstructions').value=project.instructions;$('savedBusinessBrief').value=project.teamConfig?.businessBrief||'';renderRoleEditor();renderFiles();$('settingsFeedback').textContent='';$('settingsDialog').showModal();}
 function renderFiles(){$('projectFiles').innerHTML=project.files.map(f=>`<div><button data-project-file="${esc(f.path)}" class="mono">▱ ${esc(f.path)}</button><small>${(f.size/1024).toFixed(1)} KB · ${f.readable?'可检索文本':'附件'}</small></div>`).join('');}
 async function saveMessage(){const text=$('message').value.trim();if(!pid){$('projectDialog').showModal();throw Error('先创建一个项目，任务内容会保留在输入框。');}if(!text){if(session?.status==='ready'||['failed','stopped','interrupted'].includes(session?.status))return;throw Error('请填写这次任务或追加要求');}
  acceptSession(await api(session?'/session/message':'/session/create',{project:pid,session:sid,title:text.slice(0,28),text,version:session?.version,kind:$('taskKind').value}));sid=session.id;selectedRunId=null;selectedNode=null;$('message').value='';taskRouteEditKey=null;routeMessageActive=false;lastSeq=session.events.at(-1)?.seq||0;locationState();await refreshIndex();render();
 }
 async function submit(execute){await guarded(async()=>{
  const selection=nextTeamSelection(),continuing=['failed','stopped','interrupted'].includes(session?.status)&&!$('message').value.trim();
  const route=continuing?null:taskRoutePayload();selectedRunId=null;selectedNode=null;await saveMessage();
  const resume=['failed','stopped','interrupted'].includes(session.status);
  if(!resume){await persistTeamSelection(selection);await persistTaskRoute(route);}
  if(execute){acceptSession(await api('/session/start',{project:pid,session:sid,mode:resume?latestRun().mode:'live',resume,...(resume?{}:{...selection,taskRoute:session.taskRouteSelection})}));taskRouteEditKey=null;locationState();}
  else toast(resume?'本轮合同保持冻结，可继续本轮或追加新要求。':'任务、小组与路由合同已保存，尚未调用模型。');render();
 });}

 async function importFiles(input){await guarded(async()=>{
  const excluded=/^(?:\.|node_modules$|__pycache__$|dist$|build$|venv$)/,files=[],skipped=[];let bytes=0;
  for(const f of input.files){let path=f.webkitRelativePath?f.webkitRelativePath.split('/').slice(1).join('/'):f.name;if(path.split('/').some(p=>excluded.test(p))||/\.(pem|key|p12|pfx)$/i.test(path)){skipped.push(path);continue;}if(f.size>1000000)throw Error(path+' 超过 1 MB，请分拆后导入');bytes+=f.size;if(bytes>16*1024*1024||files.length>=200)throw Error('每批最多 200 个文件、16 MB');const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(f);});files.push({path,data});}
  if(!files.length)throw Error('没有可导入的项目资料');project=await api('/project/import',{project:pid,files,revision:project.revision});renderFiles();renderMemory();$('settingsFeedback').textContent=`已导入 ${files.length} 个文件${skipped.length?`，跳过 ${skipped.length} 个隐藏或依赖文件`:''}。后续会话自动读取。`;await refreshIndex();input.value='';
 });}
 async function tick(){if(loaded&&!busy){await pollModelProbes();await pollTaskQueue();}if(playback){if(!playback.paused)heartbeat();return;}if(polling||busy||!loaded)return;polling=true;try{if(sid){const expectedPid=pid,expectedSid=sid,next=await api('/session?'+query({}));if(playback||pid!==expectedPid||sid!==expectedSid)return;if(next.version!==session?.version){const events=next.events.filter(e=>e.seq>lastSeq),viewedRun=current()?.id;acceptSession(next);if(viewedRun!==current()?.id){LancerMotion.clear();locationState();}lastSeq=next.events.at(-1)?.seq||0;if(!['queued','running'].includes(session.status)){project=await api('/project?'+query({}));await refreshIndex();}render();animateEvents(events);}}}catch(e){$('footerStatus').textContent='连接中断，正在重试…';}finally{polling=false;}heartbeat();}
 function relayNode(n){return $('relay-'+(n.agent==='native_codex'?'native':['draft','decision','integration'].includes(n.phase)?'plan':n.phase==='verification'?'review':n.phase)+'-'+n.agent);}
 function animateEvents(events){const r=current();if(!r)return;events=events.filter(e=>!e.runId||e.runId===r.id);for(const e of events.filter(e=>e.type==='handoff')){const n=r.nodes.find(n=>n.id===e.node);if(!n)continue;const target=LancerStudioTree.target(e)?.querySelector('.node-dot')||LancerStudioTree.target(e);if(target&&selectedPane==='tree'){const a=target.getBoundingClientRect(),box=$('graphScroll'),b=box.getBoundingClientRect();if(a.top<b.top+15||a.bottom>b.bottom-35)box.scrollTop+=a.top-b.top-box.clientHeight*.45;}const source=relayNode(n),p=n.deps.map(id=>r.nodes.find(n=>n.id===id)).find(p=>p&&p.agent!==n.agent);if(p)LancerMotion.send(relayNode(p),source,LancerStudioTree.laneFor(n.id).color,'relay-'+n.id);setTimeout(()=>LancerMotion.send(source,target,LancerStudioTree.laneFor(n.id).color,'lane-'+n.id),p?600:200);}
  const completed=events.filter(e=>e.type==='round_completed'||e.type==='plan_finalized'||e.type==='project_merged');if(completed.length)LancerMotion.complete(completed.at(-1).detail.slice(0,32));else if(events.some(e=>e.type==='node_completed'))LancerMotion.complete(events.find(e=>e.type==='node_completed').detail.slice(0,30));
 }
 function heartbeat(){if(playback?.paused)return;if(Date.now()-lastHeartbeat<3400||document.hidden)return;lastHeartbeat=Date.now();for(const n of current()?.nodes.filter(n=>n.status==='running')||[])LancerMotion.send(relayNode(n),$('tree-stage-'+n.id)?.querySelector('.node-dot'),LancerStudioTree.laneFor(n.id).color,'lane-'+n.id);}
 function replaySource(full,runId){
  const source=JSON.parse(JSON.stringify(full)),position=source.runs.findIndex(r=>r.id===runId);if(position<0)throw Error('找不到所选执行轮次');source.runs=source.runs.slice(0,position+1);const run=source.runs.at(-1),ids=new Set(source.runs.map(r=>r.id)),lastSeq=Math.max(0,...source.events.filter(e=>e.runId===run.id||!e.runId&&full.runs.length===1).map(e=>e.seq));
  source.events=source.events.filter(e=>e.runId?ids.has(e.runId):e.seq<=lastSeq);
  const cutoff=Math.max(0,...source.messages.map((m,i)=>ids.has(m.runId)||m.role==='user'&&m.text===run.request?i:-1));
  source.messages=source.messages.filter((m,i)=>m.role==='user'?(m.time&&run.started?new Date(m.time)<=new Date(run.started):i<=cutoff):ids.has(m.runId));
  const messageIds=new Set(source.messages.map(m=>m.id)),agentIds=new Set(source.runs.flatMap(r=>r.nodes.map(n=>n.agentId)).filter(Boolean));
  source.agents=Object.fromEntries(Object.entries(source.agents||{}).map(([id,a])=>[id,{...a,turns:a.turns.filter(t=>messageIds.has(t.id))}]).filter(([id,a])=>a.turns.length||agentIds.has(id)));
  source.memory=(source.memory||[]).filter(m=>ids.has(m.run));source.classification=run.classification||source.classification;source.status=run.status;source.error=null;return source;
 }
 function replayProject(full,source,seq){const run=source.runs.at(-1),earlier=new Set(source.runs.slice(0,-1).map(r=>r.id)),merged=source.events.some(e=>e.runId===run.id&&e.type==='project_merged'&&e.seq<=seq);return {...full,instructions:run.projectSnapshot?.instructions||'',memory:(full.memory||[]).filter(m=>m.run===run.id?merged:earlier.has(m.run)||m.time&&run.started&&new Date(m.time)<=new Date(run.started))};}
 const playbackTime=()=>playback?Math.min(playback.frames.at(-1).at,playback.elapsed+(playback.paused?0:(performance.now()-playback.anchor)*playback.speed)):0;
 const formatPlayback=ms=>String(Math.floor(ms/60000)).padStart(2,'0')+':'+String(Math.floor(ms/1000)%60).padStart(2,'0');
 function updatePlaybackControls(){
  $('replayBar').hidden=!playback;if(!playback)return;const p=playback,position=playbackTime(),duration=p.frames.at(-1).at;
  $('replayLabel').textContent=p.ended?'回放结束':p.paused?'回放已暂停':'事件回放 · 等待已压缩';$('replayPosition').textContent=formatPlayback(position)+' / '+formatPlayback(duration);
  $('replayPause').textContent=p.paused?'继续播放 ▷':'暂停 Ⅱ';$('replayPause').disabled=p.ended;$('replayProgress').style.width=(duration?position/duration*100:0)+'%';
  $('taskStatus').textContent=p.ended?'回放结束':p.paused?'已暂停':'回放中';$('motionStatus').textContent=p.ended?'事件轨迹 · 回放完成':p.paused?'事件轨迹 · 已暂停':'事件轨迹 · 记录回放';$('footerStatus').textContent=p.ended?'已保存记录回放完毕':'播放已有事件，不执行任务';$('modeNotice').textContent='只播放已有'+(current()?.mode==='demo'?'演练':'执行')+'记录 · 不调用模型、不改动项目';
 }
 function schedulePlayback(){
  const p=playback;if(!p||p.paused||p.ended)return;clearTimeout(p.timer);const next=p.frames[p.cursor+1];if(!next)return;
  p.timer=setTimeout(()=>{if(playback!==p||p.paused)return;p.cursor++;const frame=p.frames[p.cursor];session=LancerReplay.project(p.source,frame.seq);
   project=replayProject(p.project,p.source,frame.seq);
   selectedNode=null;render();animateEvents(frame.events);$('conversation').scrollTop=$('conversation').scrollHeight;
   if(frame.end){p.elapsed=frame.at;p.paused=true;p.ended=true;updatePlaybackControls();return;}schedulePlayback();
  },Math.max(0,(next.at-playbackTime())/p.speed));
 }
 function startPlayback(){
  if(playback)stopPlayback();if(!session||current()?.status==='running'||!current()?.nodes.length){toast('请先打开一段已保存的执行记录');return;}
  const savedSession=session,source=replaySource(savedSession,current().id),frames=LancerReplay.timeline(source);if(frames.length<2){toast('这段会话尚没有可回放的事件');return;}
  LancerMotion.clear();playback={source,savedSession,project,frames,cursor:0,elapsed:0,anchor:performance.now(),speed:Number($('replaySpeed').value),paused:false,ended:false,timer:null};
  selectedNode=null;branchFocus=false;session=LancerReplay.project(source,0);project=replayProject(playback.project,source,0);showPane('tree');render();$('graphScroll').scrollTop=0;$('conversation').scrollTop=0;schedulePlayback();
 }
 function pausePlayback(){const p=playback;if(!p||p.ended)return;if(!p.paused){p.elapsed=playbackTime();p.paused=true;clearTimeout(p.timer);LancerMotion.setPaused(true);}else{p.paused=false;p.anchor=performance.now();LancerMotion.setPaused(false);schedulePlayback();}updatePlaybackControls();}
 function stopPlayback(){const p=playback;if(!p)return;clearTimeout(p.timer);LancerMotion.setPaused(false);LancerMotion.clear();session=p.savedSession;project=p.project;playback=null;selectedNode=null;render();}
 $('replayStart').onclick=startPlayback;$('replayPause').onclick=pausePlayback;$('replayExit').onclick=stopPlayback;
 $('runSelector').onchange=()=>chooseRun($('runSelector').value);
 $('replaySpeed').onchange=()=>{if(!playback)return;playback.elapsed=playbackTime();playback.anchor=performance.now();playback.speed=Number($('replaySpeed').value);schedulePlayback();updatePlaybackControls();};
 setInterval(()=>{if(playback&&!playback.paused)updatePlaybackControls();},250);
 document.addEventListener('click',e=>{const b=e.target.closest('button,a');if(!b)return;if(b.hasAttribute('data-close')){b.closest('dialog').close();return;}if(b.dataset.project)guarded(()=>chooseProject(b.dataset.project));if(b.dataset.session)guarded(()=>chooseSession(b.dataset.session));if(b.dataset.pane)showPane(b.dataset.pane);if(b.dataset.showPane)showPane(b.dataset.showPane);if(b.dataset.teamNode){selectedNode=b.dataset.teamNode;renderTeam();}if(b.dataset.model)catalog();if(b.dataset.context)contextDetail(b.dataset.context).catch(error);if(b.hasAttribute('data-comparison-open'))guarded(openComparisons);if(b.dataset.controlledStop)guarded(async()=>{await api('/controlled-experiment/stop',{project:pid,id:b.dataset.controlledStop});await refreshComparisons();});if(b.dataset.comparisonStop)guarded(async()=>{await api('/comparison/stop',{project:pid,id:b.dataset.comparisonStop});await refreshComparisons();$('comparisonFeedback').textContent='已请求停止该批次，后续排队轮次将取消；点击刷新查看最新状态。';});
  if(b.dataset.modelProbe)guarded(()=>probeModel(b.dataset.modelProbe));
  if(b.dataset.modelProbeStop)guarded(()=>stopModelProbe(b.dataset.modelProbeStop));
  if(b.dataset.modelPolicy){$('modelPolicySection').open=true;renderModelPolicyOptions(b.dataset.modelPolicy);}
  if(b.dataset.mcpProbe)guarded(()=>manageMcpConnection('probe',b.dataset.mcpProbe));
  if(b.dataset.mcpRemove)guarded(()=>manageMcpConnection('remove',b.dataset.mcpRemove));
  if(b.dataset.hostAcceptanceRemove)guarded(()=>removeHostAcceptance(b.dataset.hostAcceptanceRemove));
  if(b.dataset.triggerEdit)beginTriggerSource(b.dataset.triggerEdit);
  if(b.dataset.triggerToggle)guarded(()=>toggleTriggerSource(b.dataset.triggerToggle));
  for(const action of ['poll','resume','evaluate','dispatch','publish'])if(b.dataset['trigger'+action[0].toUpperCase()+action.slice(1)])guarded(()=>triggerAction(action,b.dataset['trigger'+action[0].toUpperCase()+action.slice(1)]));
  if(b.dataset.contributionVerify)openContributionVerification(b.dataset.contributionVerify);
  if(b.hasAttribute('data-evidence-review'))guarded(requestEvidenceReview);
  if(b.dataset.taskNode){selectedNode=b.dataset.taskNode;showPane('tree');renderSidebar();const m=session?.messages.find(m=>m.node===selectedNode&&m.runId===current()?.id);if(m)document.querySelector(`[data-turn="${m.id}"]`)?.scrollIntoView({block:'start',behavior:'smooth'});}
  if(b.hasAttribute('data-export'))location.href='/api/studio/export?'+query({});
  if(b.dataset.fileMessage){const m=session.messages.find(m=>m.id===b.dataset.fileMessage),f=m.result.files[Number(b.dataset.fileIndex)];showDetail(f.path,f.content!==undefined?`<pre>${esc(f.content)}</pre>`:'<p>图片文件已保留在产物包中。</p>');}
  if(b.dataset.projectFile){fetch('/api/file?'+new URLSearchParams({id:pid,path:b.dataset.projectFile})).then(r=>r.json()).then(f=>showDetail(b.dataset.projectFile,f.binary?'<p>二进制附件，可通过「文件与版本」下载。</p>':`<pre>${esc(f.content||f.error)}</pre>`)).catch(error);}
 });
 $('fullTree').onclick=()=>{branchFocus=false;graph();};$('focusTree').onclick=()=>{branchFocus=true;graph();};
 $('motionOnce').onclick=()=>{const r=current(),n=r?.nodes.find(n=>n.id===selectedNode)||r?.nodes.find(n=>n.phase==='build')||r?.nodes[0];if(!n){toast('先打开一段有执行记录的会话');return;}const p=n.deps.map(id=>r.nodes.find(x=>x.id===id)).find(x=>x&&x.agent!==n.agent);const target=$('tree-stage-'+n.id)?.querySelector('.node-dot'),box=$('graphScroll');if(target){const a=target.getBoundingClientRect(),b=box.getBoundingClientRect();box.scrollTop+=a.top-b.top-box.clientHeight*.45;}if(p)LancerMotion.send(relayNode(p),relayNode(n),LancerStudioTree.laneFor(n.id).color,'relay-preview');setTimeout(()=>LancerMotion.send(relayNode(n),target,LancerStudioTree.laneFor(n.id).color,'lane-preview'),p?700:0);toast('仅演示轨迹，不改变任务状态、不调用模型。');};
 $('projectSearch').addEventListener('input',renderSidebar);$('createProject').onclick=()=>$('projectDialog').showModal();$('newSession').onclick=()=>guarded(()=>chooseProject(pid));$('projectSettings').onclick=()=>guarded(settings);
 $('projectForm').onsubmit=e=>{e.preventDefault();guarded(async()=>{try{const keep=$('message').value,keepTeam=teamDraft,p=await api('/project/create',{title:$('projectName').value,goal:$('projectGoal').value,instructions:$('projectInstructions').value});$('projectDialog').close();$('projectForm').reset();await refreshIndex();await chooseProject(p.id);teamDraft=keepTeam;$('message').value=keep;render();}catch(e){$('projectError').textContent=e.message;throw e;}});};
 $('createCustomRole').onclick=()=>{captureRoleGuide();showCustomRoleEditor();};
 $('customRoleForm').onsubmit=e=>{e.preventDefault();saveCustomRole();};
 $('refreshTriggers').onclick=()=>guarded(refreshTriggers);
 $('newTriggerSource').onclick=()=>guarded(async()=>{if(!triggerData)await refreshTriggers();beginTriggerSource();});
 $('triggerUseDirectContract').onclick=()=>guarded(useTriggerDirectContract);
 $('triggerClaimForm').onsubmit=e=>{e.preventDefault();guarded(claimTriggerThread);};
 $('triggerSubscriptions').addEventListener('click',e=>{const button=e.target.closest('[data-sub-action]');if(button)guarded(()=>updateTriggerSubscription(button.dataset.subSource,button.dataset.subThread,button.dataset.subAction));});
 $('triggerSourceForm').onsubmit=e=>{e.preventDefault();guarded(saveTriggerSource);};
 $('triggerKind').onchange=renderTriggerKind;$('triggerMode').onchange=renderTriggerKind;
 $('triggerUseTeam').onclick=()=>{try{useTriggerTeam();}catch(e){$('triggerSaveFeedback').textContent=e.message;}};
 $('triggerConnectToken').onclick=()=>guarded(()=>connectTriggerGithub(false));$('triggerConnectGit').onclick=()=>guarded(()=>connectTriggerGithub(true));
 $('triggerDisconnect').onclick=()=>guarded(disconnectTriggerGithub);$('triggerSendCustom').onclick=()=>guarded(sendTriggerCustom);

 $('contributionForm').onsubmit=e=>{e.preventDefault();saveContributionVerification();};
 $('roleGuideSelect').onchange=()=>{captureRoleGuide();showRoleGuide($('roleGuideSelect').value);};
 $('saveInstructions').onclick=()=>guarded(async()=>{
  captureRoleGuide();const roleGuides=Object.fromEntries(Object.entries(roleGuideDraft).filter(([,value])=>value.trim()));
  try{project=await api('/project/update',{project:pid,instructions:$('savedInstructions').value,version:project.version,teamConfig:{...(project.teamConfig||{}),businessBrief:$('savedBusinessBrief').value,roleGuides,...(mcpPolicyDraft?{mcpPolicy:mcpPolicyDraft}:{}),...(workerPolicyDraft?{workerTools:workerPolicyPayload()}:{})}});$('settingsFeedback').textContent='业务说明、角色规范与工具授权已保存，将用于后续新任务。';renderMemory();renderTeam();}
  catch(e){$('settingsFeedback').textContent=e.message;throw e;}
 });
 $('fileInput').onchange=()=>importFiles($('fileInput'));$('folderInput').onchange=()=>importFiles($('folderInput'));
 $('composer').onsubmit=e=>{e.preventDefault();submit(true);};$('saveTask').onclick=()=>submit(false);$('message').oninput=taskMessageChanged;
 $('taskRouteMode').onchange=()=>{taskRouteTouched=true;renderTaskRouteEditor();$('directTaskContract').open=$('taskRouteMode').value==='direct';};
 $('directTaskClearProcess').onclick=()=>{if(busy||routeLocked())return;taskRouteProcessDraft=null;taskRouteTouched=true;renderTaskRouteEditor();};
 for(const id of ['directTaskRole','directTaskCapability','directTaskComplexity','directTaskScope','directTaskAcceptance','directTaskReview'])$(id).oninput=()=>{taskRouteTouched=true;};
 $('directTaskWorkType').onchange=()=>{taskRouteTouched=true;$('directTaskReview').checked=$('directTaskWorkType').value==='development';renderTaskRouteEditor();};
 $('stopTask').onclick=()=>guarded(stopCurrentTask);
 $('runBusinessExample').onclick=()=>guarded(async()=>{const s=await api('/business-example',{fresh:true});await refreshIndex();await chooseProject(s.projectId,s.id);if(s.status==='ready'){session=await api('/session/start',{project:pid,session:sid,mode:'demo'});render();}else toast('已打开业务协作演练，固定样例不调用模型、不积累正式经验。');});
 $('runExample').onclick=()=>guarded(async()=>{const s=await api('/example',{fresh:true});await refreshIndex();await chooseProject(s.projectId,s.id);if(s.status==='ready'){session=await api('/session/start',{project:pid,session:sid,mode:'demo'});render();}else toast('已打开独立演练会话，所有结果均为固定样例。');});
 $('exportSession').onclick=()=>{location.href='/api/studio/export?'+query({});};$('legacyFiles').onclick=()=>{if(pid)localStorage.setItem('lancer-local-project',pid);};
 $('catalogButton').onclick=catalog;$('memoryButton').onclick=()=>showPane('memory');$('connectButton').onclick=()=>{$('apiKey').value='';$('connectFeedback').textContent='';renderProviderControls();renderNativeConnection();openMcpConnections();renderHostAcceptance();$('connectDialog').showModal();};
 $('hostAcceptanceForm').onsubmit=e=>{e.preventDefault();guarded(saveHostAcceptance);};
 $('comparisonCase').onchange=()=>{if(comparisonData){$('comparisonContent').innerHTML=comparisonContentHtml(comparisonData,$('comparisonCase').value);$('controlledContent').innerHTML=controlledContentHtml(comparisonData.controlled,$('comparisonCase').value);}};
 $('controlledStart').onclick=()=>guarded(async()=>{try{const selection=nextTeamSelection();await api('/controlled-experiment/start',{project:pid,coordinatorModel:selection.coordinatorModel,teamModels:selection.teamModels,caseId:$('comparisonCase').value,repetitions:Number($('comparisonRepetitions').value),homogeneous:$('controlledModels').value==='homogeneous'});await refreshComparisons();$('controlledFeedback').textContent='交付对照已启动，不按阶段分配额度。点击刷新查看结果与实际用量。';}catch(e){$('controlledFeedback').textContent=e.message;throw e;}});
 $('comparisonRefresh').onclick=()=>guarded(refreshComparisons);
 $('comparisonStart').onclick=()=>guarded(async()=>{try{const selection=nextTeamSelection();await api('/comparison/start',{project:pid,coordinatorModel:selection.coordinatorModel,teamModels:selection.teamModels,caseId:$('comparisonCase').value,repetitions:Number($('comparisonRepetitions').value)});await refreshComparisons();$('comparisonFeedback').textContent='真实对照已启动。点击刷新查看两组执行状态。';}catch(e){$('comparisonFeedback').textContent=e.message;throw e;}});
 $('teamSetupButton').onclick=()=>{teamEdit=null;renderTeamSetup();$('teamSetupDialog').showModal();};
 $('workPolicyButton').onclick=()=>{renderWorkPolicy();$('workPolicyDialog').showModal();};
 $('policyDeepReview').onchange=()=>{$('policyMultiplier').disabled=!$('policyDeepReview').checked;};
 $('workPolicyForm').onsubmit=e=>{e.preventDefault();guarded(saveWorkPolicy);};
 $('coordinatorFamily').onchange=()=>{renderCoordinatorOptions();changeCoordinator();};$('coordinatorModel').onchange=changeCoordinator;
 $('teamSetupForm').onsubmit=e=>{e.preventDefault();guarded(async()=>{const selection=readTeamSelection();if(!selection.coordinatorModel)throw Error('请选择一个主模型。');if(session)await persistTeamSelection(selection);else teamDraft=selection;renderTeamSelection();renderRelay();$('teamSetupFeedback').textContent=session?'工作小组已保存；下一次启动时固定到执行记录。':'工作小组已暂存；创建会话时一并保存。';});};
 $('registerProvider').onchange=renderRegistrationFields;
 $('registerProtocol').onchange=()=>{if($('registerProtocol').value==='image')$('registerProfile').value='image';else if($('registerProfile').value==='image')$('registerProfile').value='balanced';};
 $('registerProfile').onchange=()=>{if($('registerProfile').value==='image')$('registerProtocol').value='image';else if($('registerProtocol').value==='image')$('registerProtocol').value='chat';};
 $('registerModelForm').onsubmit=e=>{e.preventDefault();guarded(async()=>{try{index=await api('/models/register',{providerId:$('registerProvider').value,providerName:$('registerProviderName').value,baseUrl:$('registerBaseUrl').value,modelId:$('registerModelId').value,name:$('registerModelName').value,family:$('registerFamily').value,protocol:$('registerProtocol').value,profile:$('registerProfile').value});renderProviderControls();renderRelay();$('registerFeedback').textContent='型号已添加，能力为用户指定先验。请核对目录与调用状态，达到可派工条件后加入工作小组。';}catch(e){$('registerFeedback').textContent=e.message;throw e;}});};
 $('nativeToggle').onclick=()=>guarded(async()=>{try{await api('/native',{enabled:!index.native?.enabled});await refreshIndex();renderRelay();$('nativeFeedback').textContent=index.native.enabled?'原生 Codex 已加入后续任务调度。':'原生 Codex 已停用。';}catch(e){$('nativeFeedback').textContent=e.message;throw e;}finally{renderNativeConnection();}});
 $('provider').onchange=()=>{$('providerBase').textContent=(index.providers||[]).find(p=>p.id===$('provider').value)?.baseUrl||'';$('modelReadinessFeedback').textContent='';renderModelReadiness();renderModelPolicyOptions();};
 $('discoverModels').onclick=()=>guarded(()=>discoverModelCatalog());
 $('modelPolicyModel').onchange=renderModelPolicyEditor;
 $('modelPolicyForm').onsubmit=e=>{e.preventDefault();guarded(saveModelPolicy);};
 $('connectForm').onsubmit=e=>{e.preventDefault();guarded(saveModelConnection);};
 $('forgetKeys').onclick=()=>guarded(async()=>{await api('/forget',{});$('apiKey').value='';await refreshIndex();renderProviderControls();renderRelay();$('connectFeedback').textContent='本次服务的全部密钥已清除。';});
 new ResizeObserver(()=>{if(loaded&&selectedPane==='tree')graph();}).observe($('graphScroll'));
 async function init(){try{await refreshIndex();const params=new URLSearchParams(location.search);const speed=params.get('speed');if([...$('replaySpeed').options].some(option=>option.value===speed))$('replaySpeed').value=speed;let saved={};try{saved=JSON.parse(localStorage.getItem('lancer-studio-selection')||'{}');}catch{}const wanted=params.get('project')||saved.project,projectRow=index.projects.find(p=>p.id===wanted)||index.projects[0];const wantedSession=params.get('session')||saved.session;await chooseProject(projectRow?.id||null,projectRow?.sessions.find(s=>s.id===wantedSession)?.id||null,params.get('run')||(wantedSession===saved.session?saved.run:null));loaded=true;if(params.get('replay')==='1'&&current()?.status!=='running')startPlayback();setInterval(tick,950);}catch(e){error(e);$('footerStatus').textContent='未连接本地服务，请刷新重试。';}}
 init();
})();
