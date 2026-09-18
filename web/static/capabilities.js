(() => {
  'use strict';
  const CAP_BASE = '/api/capabilities';
  const capState = { space:'workbench', module:'scripts', account:'', shopId:'', shops:[], managedShops:[], shopManagerView:'onboarded', manualShop:false, scripts:[], scriptSearch:'', scriptPage:0, messagePage:1, messagePages:1, messageFilters:{shop_id:'',search:'',roles:['user','mall_cs','assistant_simulated'],whitebox:'all',from:'',to:''}, messages:[], conversationView:null, workflows:[], workflowEditor:null, routingRules:[], routingSettings:{}, routingTargets:{}, forceHandoff:{}, paymentReminder:{}, growth:{}, growthClusters:[], growthProposals:[], growthProposalStatus:'all', corrections:[], correctionShopIds:[], correctionStatus:'pending', correctionServiceStage:'all', correctionsShopCount:0, prompts:{}, promptName:'default', importPreview:null, importPayload:null, bridgeTokens:[] };
  let capShopBrainReturn = 'manager';
  const capNames = {
    messages:['全部消息','会话归档'],
    growth:['成长中心','从运行信号发现能力缺口，形成可审核、可追踪的学习建议'],
    routing:['接待分工','哪种客户、问什么问题，用哪套方式接待（只影响模拟，不直接发给买家）'],
    payment_reminder:['催付设置','咨询后仍未下单的自动挽留话术'],
    force_handoff:['强制转人工','按关键词、业务意图和风险分数分层判断是否转客服'],
    scripts:['固定回复库','客户问法、审核回复和适用范围'], knowledge:['知识库','本地资料、文档切片与检索验证'],
    models:['大模型配置','兼容 OpenAI 接口的模型连接参数'], prompts:['提示词','分类、售前、售后、物流和默认提示词'],
    workflows:['售后规则','买家出问题后按哪几步排查、何时转人工；可导出/导入给店群选用'], import:['聊天导入','导入本地历史用于测试和复盘'],
    corrections:['纠正记录','审核通过后作为影子模型的高优先级依据'],
    users:['用户权限','店长/班长/一线/质检/只读；按店铺授权'],
    tokens:['令牌管理','全局客户端接入凭据与设备绑定，不区分店铺']
  };
  const CAP_MODULE_NEED = {
    messages:['capabilities.read','capabilities.read'],
    growth:['capabilities.read','capabilities.corrections'],
    scripts:['capabilities.read','capabilities.write'],
    knowledge:['capabilities.read','capabilities.write'],
    models:['capabilities.read','capabilities.strategy'],
    prompts:['capabilities.read','capabilities.strategy'],
    routing:['capabilities.read','capabilities.strategy'],
    payment_reminder:['capabilities.read','capabilities.strategy'],
    force_handoff:['capabilities.read','capabilities.strategy'],
    workflows:['capabilities.read','capabilities.write'],
    import:['capabilities.read','capabilities.write'],
    // 质检主战场：纠正记录
    corrections:['capabilities.corrections','capabilities.corrections'],
    users:['users.manage'],
    tokens:['users.manage']
  };
  function capHasPerm(perm){
    if(window.__AUTH__ && typeof window.__AUTH__.hasPerm==='function')return window.__AUTH__.hasPerm(perm);
    // 后端/页面尚未接入角色时：普通模块可用，用户管理先隐藏（避免 404「加载失败」）
    if(perm==='users.manage')return false;
    return true;
  }
  function capCanModule(moduleName, write=false){
    if(window.__KEFU_CONFIG__?.capabilities_enabled === false || (!capHasPerm('capabilities.view') && !['users','tokens'].includes(moduleName)))return false;
    const need=CAP_MODULE_NEED[moduleName];
    if(!need)return true;
    return write?capHasPerm(need[1]):capHasPerm(need[0])||capHasPerm(need[1]);
  }
  const e = value => escapeHtml(value == null ? '' : String(value));
  function jsonText(value){ try{return JSON.stringify(value,null,2);}catch(_){return String(value??'');} }
  function listText(value){ return Array.isArray(value)?value.join('\n'):(value||''); }
  function splitList(value){ return String(value||'').split(/[\n,，]+/).map(v=>v.trim()).filter(Boolean); }
  function parseJSON(value,fallback,label){ const text=String(value||'').trim(); if(!text)return fallback; try{return JSON.parse(text);}catch(_){throw new Error(`${label||'JSON'} 格式不正确`);} }
  function fmtBytes(n){ const s=Number(n||0); return s<1024?`${s} B`:s<1048576?`${(s/1024).toFixed(1)} KB`:`${(s/1048576).toFixed(1)} MB`; }
  function capSetLoading(text='正在加载…'){ $('capContent').innerHTML=`<div class="cap-loading">${e(text)}</div>`; }
  function capError(error){ console.error(error); $('capContent').innerHTML=`<div class="cap-empty">加载失败<br>${e(error?.message||error||'unknown')}</div>`; toast('能力中心加载失败：'+(error?.message||error||'unknown')); }
  function capButtonBusy(button,busy,busyText='处理中…'){ if(!button)return; if(busy){button.dataset.oldText=button.textContent;button.disabled=true;button.textContent=busyText;}else{button.disabled=false;if(button.dataset.oldText)button.textContent=button.dataset.oldText;} }
  function capModal(title,body,footer=''){ const modal=$('capModal');modal.classList.remove('conversation-open','workflow-open');$('capDialogTitle').textContent=title;$('capDialogBody').innerHTML=body;$('capDialogFoot').innerHTML=footer||'<button class="cap-btn" data-cap-close type="button">关闭</button>';modal.classList.add('open'); }
  function capCloseModal(){ const modal=$('capModal');modal.classList.remove('open','conversation-open','workflow-open');capState.conversationView=null;capState.workflowEditor=null; }
  function capShopName(row){
    if(!row)return capState.shopId||'未知店铺';
    return row.shop_name||row.shop_id||'未知店铺';
  }
  function capRenderShopSelector(){
    const select=$('capShopSelect');if(!select)return;
    const rows=[...capState.shops];
    select.innerHTML=rows.length?rows.map(row=>`<option value="${e(row.shop_id)}" ${row.shop_id===capState.shopId?'selected':''}>${e(capShopName(row))}（${e(row.shop_id)}）</option>`).join(''):'<option value="">当前没有运行中店铺</option>';
    select.disabled=rows.length<2;
    select.title=rows.length>1?'点击切换店铺能力空间':rows.length?'当前只有一个运行中店铺':'请先通过“管理店铺”接入或恢复店铺';
  }
  async function capLoadShopOptions(force=false){
    if(capState.shops.length&&!force)return capState.shops;
    const data=await fetchJSON(`${CAP_BASE}/shops`);
    let shops=Array.isArray(data.shops)?data.shops.filter(row=>row&&row.shop_id):[];
    // Restrict shop list for bound agents/leaders.
    const auth=window.__AUTH__?.user;
    if(auth && auth.role!=='owner' && !auth.system){
      const allow=new Set((auth.shop_ids||[]).map(String));
      if(allow.size)shops=shops.filter(row=>allow.has(String(row.shop_id)));
    }
    capState.shops=shops;
    const names={...(window.__shopNameMap||{})};
    capState.shops.forEach(row=>{if(row.shop_name)names[row.shop_id]=row.shop_name;});
    window.__shopNameMap=names;
    return capState.shops;
  }
  async function capEnsureShop(force=false){
    await capLoadShopOptions(force);
    if(capState.shopId&&!capState.shops.some(row=>row.shop_id===capState.shopId)){
      capState.shopId='';capState.manualShop=false;sessionStorage.removeItem('capabilityShopId');
    }
    const remembered=sessionStorage.getItem('capabilityShopId')||'';
    if(!capState.shopId&&remembered&&capState.shops.some(row=>row.shop_id===remembered)){
      capState.shopId=remembered;capState.manualShop=true;
    }
    if(!capState.shopId||!capState.manualShop){
      let account=currentAccount||capState.account;
      if(!account){const status=await fetchJSON('/api/status?full=1');account=status?.config?.default_account||'';}
      capState.account=account||'';
      let mappedShopId='';
      if(capState.account){const mapped=await fetchJSON(`${CAP_BASE}/shop?account=${encodeURIComponent(capState.account)}`);mappedShopId=mapped.shop_id||'';}
      capState.shopId=capState.shops.some(row=>row.shop_id===mappedShopId)?mappedShopId:(capState.shops[0]?.shop_id||'');
    }
    const selected=capState.shops.find(row=>row.shop_id===capState.shopId);
    capState.account=selected?.account||(selected?.accounts||[])[0]||capState.account||'';
    capRenderShopSelector();
    return capState.shopId;
  }
  async function capOpenSpace(space){
    if(space==='capabilities' && (window.__KEFU_CONFIG__?.capabilities_enabled === false || (!capHasPerm('capabilities.view') && !capHasPerm('users.manage')))){
      toast('当前角色不能进入能力中心');
      space='workbench';
    }
    capState.space=space==='capabilities'?'capabilities':'workbench';
    $('body').dataset.space=capState.space;
    document.querySelectorAll('.mode-tab').forEach(btn=>btn.classList.toggle('active',btn.dataset.space===capState.space));
    $('titleSub').textContent=capState.space==='capabilities'?'能力中心 · 按角色授权':'自建旁路';
    if(capState.space==='capabilities'){
      capApplyNavPermissions();
      try{
        if(['users','tokens','corrections'].includes(capState.module)){await capLoad(capState.module);return;}
        await capEnsureShop(true);await capLoad(capState.module);
      }catch(error){capError(error);}
    }
  }
  function capApplyNavPermissions(){
    document.querySelectorAll('.cap-nav-btn').forEach(btn=>{
      const mod=btn.dataset.capModule;
      if(!mod)return;
      const ok=capCanModule(mod,false);
      btn.style.display=ok?'':'none';
      btn.disabled=!ok;
    });
    // Ensure owner-only administration modules exist in older embedded templates.
    const nav=document.querySelector('.cap-nav');
    if(nav && !nav.querySelector('[data-cap-module="users"]')){
      const btn=document.createElement('button');
      btn.className='cap-nav-btn';
      btn.dataset.capModule='users';
      btn.type='button';
      btn.innerHTML='<span class="cap-nav-icon">&#128100;</span>用户权限';
      nav.appendChild(btn);
    }
    if(nav && !nav.querySelector('[data-cap-module="tokens"]')){
      const btn=document.createElement('button');
      btn.className='cap-nav-btn';
      btn.dataset.capModule='tokens';
      btn.type='button';
      btn.innerHTML='<span class="cap-nav-icon">&#128273;</span>令牌管理';
      nav.appendChild(btn);
    }
    [nav?.querySelector('[data-cap-module="users"]'),nav?.querySelector('[data-cap-module="tokens"]')].forEach(btn=>{
      if(!btn)return;
      const ok=capHasPerm('users.manage');
      btn.style.display=ok?'':'none';
      btn.disabled=!ok;
    });
  }
  function capApplyModuleScope(moduleName){
    const globalModule=['users','tokens','corrections'].includes(moduleName);
    const shopSwitch=document.querySelector('.cap-shop-switch');
    if(shopSwitch)shopSwitch.hidden=globalModule;
    const manageShops=$('capManageShops');
    if(manageShops)manageShops.hidden=globalModule;
  }
  async function capLoad(moduleName){
    if(!capNames[moduleName])moduleName='scripts';
    if(!capCanModule(moduleName,false) && !['users','tokens'].includes(moduleName)){
      moduleName=Object.keys(CAP_MODULE_NEED).find(m=>capCanModule(m,false))||'scripts';
    }
    if(['users','tokens'].includes(moduleName) && !capHasPerm('users.manage')){
      $('capContent').innerHTML='<div class="cap-empty">当前角色不能使用管理功能</div>';
      return;
    }
    capState.module=moduleName;
    capApplyModuleScope(moduleName);
    capApplyNavPermissions();
    document.querySelectorAll('.cap-nav-btn').forEach(btn=>btn.classList.toggle('active',btn.dataset.capModule===moduleName));
    const [title,sub]=capNames[moduleName];$('capTitle').textContent=title;$('capSubtitle').textContent=sub;capSetLoading();
    try{
      if(moduleName==='users'){await capLoadUsers();return;}
      if(moduleName==='tokens'){await capLoadBridgeTokens();return;}
      if(moduleName==='corrections'){await capLoadShopOptions();await capLoadCorrections();return;}
      await capEnsureShop();
      if(!capState.shopId){$('capContent').innerHTML='<div class="cap-empty">当前没有运行中店铺，请点击右上角“管理店铺”接入或恢复店铺。</div>';return;}
      const fn={messages:capLoadMessages,growth:capLoadGrowth,scripts:capLoadScripts,knowledge:capLoadKnowledge,models:capLoadModels,prompts:capLoadPrompts,routing:capLoadRouting,payment_reminder:capLoadPaymentReminder,force_handoff:capLoadForceHandoff,workflows:capLoadWorkflows,import:capLoadImport,corrections:capLoadCorrections,users:capLoadUsers,tokens:capLoadBridgeTokens}[moduleName];
      if(typeof fn!=='function'){
        $('capContent').innerHTML=`<div class="cap-empty">未知模块：${e(moduleName)}</div>`;
        return;
      }
      await fn();
    }catch(error){capError(error);}
  }
  async function capLoadUsers(){
    let data;
    try{
      data=await fetchJSON('/api/auth/users');
    }catch(error){
      const msg=String(error?.message||error||'');
      $('capContent').innerHTML=`<div class="cap-empty">用户权限接口不可用<br><small>${e(msg)}</small><br><br>请<strong>重启后端</strong>后再打开本页（Ctrl+F5）。<br>首次管理员凭据仅保存在服务器本地</div>`;
      toast('用户权限需要新版后端，请先重启服务');
      return;
    }
    const users=data.users||[];
    const roles=data.roles||[];
    const roleOpts=roles.map(r=>`<option value="${e(r.role)}">${e(r.label)} — ${e(r.description||'')}</option>`).join('');
    const rows=users.map(u=>`<article class="cap-row">
      <div class="cap-row-top">
        <div class="cap-row-title"><strong>${e(u.display_name||u.username)}</strong><small>@${e(u.username)} · ${e(u.role_label||u.role)}</small></div>
        <div class="cap-row-actions">
          <button class="cap-btn" data-user-edit="${e(u.username)}" type="button">改</button>
          <button class="cap-btn danger" data-user-delete="${e(u.username)}" type="button">删</button>
        </div>
      </div>
      <div class="cap-meta">
        <span class="cap-tag ${u.enabled?'green':'red'}">${u.enabled?'启用':'停用'}</span>
        <span class="cap-tag blue">${e(u.role_label||u.role)}</span>
        <span class="cap-tag">${(u.shop_ids||[]).length?('店铺 '+(u.shop_ids||[]).join(', ')):'全部店铺'}</span>
        ${u.team_id?`<span class="cap-tag">班组 ${e(u.team_id)}</span>`:''}
      </div>
    </article>`).join('');
    $('capContent').innerHTML=`
      <div class="cap-section-head">
        <div><h2>用户权限</h2><p>50 人规模建议：多数建「一线」；每班 1 名「班长」；管理岗建「质检」；每店/店群 1～2 名「店长」。</p></div>
        <div class="cap-actions"><button class="cap-btn primary" id="capUserAdd" type="button">新增用户</button></div>
      </div>
      <div class="cap-notice safe">
        <strong>角色怎么分：</strong><br>
        店长 = 全权限；班长 = 工作台 + 只读能力/纠正；一线 = 只会话回复；
        <b>质检 = 看全部会话（不回复）+ 纠错</b>；只读 = 能看不能回、不纠错。<br>
        质检账号「可服务店铺」留空 = 看所有店；填了则只看这些店。
      </div>
      <div class="cap-list">${rows||'<div class="cap-empty">还没有用户</div>'}</div>
      <template id="capUserRoleOpts">${roleOpts}</template>`;
  }
  function capTokenTime(value){
    if(!value)return '-';const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString('zh-CN',{hour12:false});
  }
  function capTokenAgentText(agent){
    const platform={pdd:'拼多多',taobao:'千牛'}[agent.platform]||'客户端';
    return `${platform} · ${agent.agent_name||agent.agent_id}${agent.version?` · ${agent.version}`:''}`;
  }
  async function capLoadBridgeTokens(){
    const data=await fetchJSON('/api/auth/bridge-tokens'),tokens=data.tokens||[];capState.bridgeTokens=tokens;
    const bound=tokens.filter(row=>row.bound).length,online=tokens.filter(row=>row.online).length;
    const rows=tokens.map(row=>{
      const agents=(row.agents||[]).map(agent=>`<div><span class="cap-tag ${agent.online?'green':''}">${agent.online?'在线':'离线'}</span> ${e(capTokenAgentText(agent))}</div>`).join('');
      return `<tr><td><strong>${e(row.label)}</strong><div class="cap-token-value"><code>${e(row.token||'')}</code><button class="cap-btn" data-token-copy-value="${e(row.token||'')}" type="button">复制</button></div></td><td><span class="cap-tag ${row.bound?'blue':''}">${row.bound?'已绑定':'未绑定'}</span><small>${row.bound_at?`绑定于 ${e(capTokenTime(row.bound_at))}`:'等待客户端首次连接'}</small></td><td>${agents||'<span class="cap-tag">尚无客户端</span>'}</td><td>${e(capTokenTime(row.created_at))}</td><td><div class="cap-row-actions"><button class="cap-btn" data-token-edit="${e(row.id)}" type="button">改备注</button><button class="cap-btn" data-token-unbind="${e(row.id)}" type="button" ${row.bound?'':'disabled'}>解绑</button><button class="cap-btn danger" data-token-delete="${e(row.id)}" type="button">撤销</button></div></td></tr>`;
    }).join('');
    $('capContent').innerHTML=`<div class="cap-section-head"><div><h2>全局客户端令牌</h2><p>不区分店铺 · 共 ${e(tokens.length)} 个，已绑定 ${e(bound)} 个，当前在线 ${e(online)} 个</p></div><div class="cap-actions"><button class="cap-btn primary" id="capTokenAdd" type="button">新增令牌</button></div></div>
      <div class="cap-notice warn">完整令牌会持续显示。解绑会允许下一台电脑重新绑定；撤销后客户端会立即失去访问权限。</div>
      <div class="cap-table-wrap"><table class="cap-table cap-token-table"><thead><tr><th>备注 / 令牌</th><th>设备</th><th>关联客户端</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${rows||'<tr><td colspan="5"><div class="cap-empty small">还没有客户端令牌</div></td></tr>'}</tbody></table></div>`;
  }
  function capOpenTokenCreate(){
    capModal('新增令牌','<label class="cap-field"><span>备注</span><input class="cap-input" id="capTokenLabel" maxlength="80" placeholder="例如：拼多多客服工位 01" autofocus /></label>','<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capTokenCreateSave" type="button">生成令牌</button>');
  }
  function capOpenTokenEdit(row){
    if(!row)return;capModal('修改令牌备注',`<label class="cap-field"><span>备注</span><input class="cap-input" id="capTokenEditLabel" maxlength="80" value="${e(row.label)}" /></label>`,`<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capTokenEditSave" data-token-id="${e(row.id)}" type="button">保存</button>`);
  }
  async function capCopyText(value){
    try{await navigator.clipboard.writeText(value);return true;}catch(_){const input=document.createElement('textarea');input.value=value;input.style.position='fixed';input.style.opacity='0';document.body.appendChild(input);input.select();const ok=document.execCommand('copy');input.remove();return ok;}
  }
  async function capCreateToken(button){
    const label=$('capTokenLabel')?.value.trim();if(!label)return toast('请填写令牌备注');capButtonBusy(button,true,'生成中…');
    try{const data=await fetchJSON('/api/auth/bridge-tokens',{method:'POST',body:JSON.stringify({label})});await capLoadBridgeTokens();capModal('令牌已生成',`<div class="cap-notice safe">稍后仍可在令牌管理中查看和复制完整令牌。</div><label class="cap-field"><span>完整令牌</span><input class="cap-input" id="capCreatedToken" value="${e(data.token||'')}" readonly /></label>`,'<button class="cap-btn primary" id="capTokenCopy" type="button">复制令牌</button><button class="cap-btn" data-cap-close type="button">关闭</button>');}catch(error){toast('生成失败：'+error.message);}finally{capButtonBusy(button,false);}
  }
  function capMessageRoleLabel(role,message=null){
    if(role==='mall_cs'){
      const name=String(message?.staff_display_name||message?.staff_name||message?.last_staff_name||'').trim();
      return name?`客服 · ${name}`:'客服';
    }
    return {user:'买家',assistant_simulated:'模拟 AI',system:'系统'}[role]||role||'未知';
  }
  const capMessageRoleValues=['user','mall_cs','assistant_simulated'];
  function capMessageRoles(filters=capState.messageFilters){
    const selected=Array.isArray(filters?.roles)?filters.roles.filter(role=>capMessageRoleValues.includes(role)):[];
    return selected.length?selected:[...capMessageRoleValues];
  }
  function capMessageRoleChecks(filters,name='capMsgRole'){
    const selected=new Set(capMessageRoles(filters));
    return capMessageRoleValues.map(role=>`<label class="cap-message-role-check"><input type="checkbox" name="${e(name)}" value="${e(role)}" ${selected.has(role)?'checked':''}><span>${e(capMessageRoleLabel(role))}</span></label>`).join('');
  }
  function capMessageTime(ts,created=''){
    const value=Number(ts||0);if(value){const d=new Date(value*1000);if(!Number.isNaN(d.getTime()))return d.toLocaleString('zh-CN',{hour12:false});}
    return created||'-';
  }
  function capShadowLatency(message,whitebox=null){
    const timing=(whitebox&&whitebox.timing)||(message?.whitebox&&message.whitebox.timing)||{};
    const format=raw=>{const ms=Number(raw);if(!Number.isFinite(ms)||ms<0)return '';if(ms<1000)return `${Math.round(ms)} 毫秒`;if(ms<60000)return `${Number((ms/1000).toFixed(1))} 秒`;return `${Math.floor(ms/60000)} 分 ${Math.round((ms%60000)/1000)} 秒`;};
    const endRaw=message?.shadow_latency_ms??timing.end_to_end_ms,processingRaw=timing.processing_ms;
    const processing=format(processingRaw),endToEnd=format(endRaw);if(!processing&&!endToEnd)return null;
    const queue=format(timing.queue_wait_ms),title=[endToEnd?`端到端 ${endToEnd}`:'',queue?`排队 ${queue}`:'',processing?`处理 ${processing}`:''].filter(Boolean).join('；');
    return {label:processing?'实际处理':'端到端',value:processing||endToEnd,title};
  }
  function capReadMessageFilters(){
    const roles=Array.from(document.querySelectorAll('input[name="capMsgRole"]:checked')).map(input=>input.value).filter(role=>capMessageRoleValues.includes(role));
    if(!roles.length){toast('买家、客服、模拟 AI 至少勾选一项');return null;}
    capState.messageFilters={
      shop_id:$('capMsgShop')?.value||'',search:($('capMsgSearch')?.value||'').trim(),
      roles,whitebox:$('capMsgWhitebox')?.value||'all',
      from:$('capMsgFrom')?.value||'',to:$('capMsgTo')?.value||''
    };
    return capState.messageFilters;
  }
  async function capLoadMessages(page=capState.messagePage){
    capState.messagePage=Math.max(1,Number(page)||1);
    const f=capState.messageFilters||{},query=new URLSearchParams({page:String(capState.messagePage),limit:'30',whitebox:f.whitebox||'all'});
    const selectedRoles=capMessageRoles(f);if(selectedRoles.length<capMessageRoleValues.length)query.set('roles',selectedRoles.join(','));if(f.shop_id)query.set('shop_id',f.shop_id);if(f.search)query.set('search',f.search);
    if(f.from){const value=Math.floor(new Date(`${f.from}T00:00:00`).getTime()/1000);if(Number.isFinite(value))query.set('from_ts',String(value));}
    if(f.to){const value=Math.floor(new Date(`${f.to}T23:59:59`).getTime()/1000);if(Number.isFinite(value))query.set('to_ts',String(value));}
    const data=await fetchJSON(`${CAP_BASE}/messages/conversations?${query}`);capState.messages=data.conversations||[];capState.messagePages=Number(data.pages||1);
    const shopOptions=['<option value="">全部授权店铺</option>',...capState.shops.map(row=>`<option value="${e(row.shop_id)}" ${f.shop_id===row.shop_id?'selected':''}>${e(capShopName(row))}</option>`)].join('');
    const rows=capState.messages.map(row=>{
      const role=String(row.last_role||''),roleClass=role==='user'?'blue':role==='assistant_simulated'?'green':'';
      const content=String(row.display_last_content||row.last_content||''),preview=content.length>180?content.slice(0,180)+'…':content;
      const shop=row.shop_name||(window.__shopNameMap||{})[row.shop_id]||row.shop_id||'未识别店铺';
      const counts=`买 ${Number(row.buyer_count||0)} · 客 ${Number(row.cs_count||0)} · AI ${Number(row.ai_count||0)}`;
      return `<tr><td class="cap-msg-time">${e(capMessageTime(row.last_ts))}</td><td>${e(shop)}<small>${e(row.account||'')}</small></td><td>${e(row.nickname||row.buyer_id)}<small>${e(row.buyer_id||'')}</small></td><td class="cap-msg-content"><span class="cap-tag ${roleClass}">${e(capMessageRoleLabel(role,row))}</span> ${e(preview)}</td><td><strong>${e(row.message_count||0)}</strong> 条<small>${e(counts)} · ${e(row.whitebox_count||0)} 条白盒</small></td><td><button class="cap-btn primary" data-conversation-open="1" data-account="${e(row.account)}" data-buyer="${e(row.buyer_id)}" type="button">查看上下文</button></td></tr>`;
    }).join('');
    $('capContent').innerHTML=`<div class="cap-section-head"><div><h2>全部会话</h2><p>共 ${e(data.total||0)} 个会话</p></div></div>
      <div class="cap-message-filters"><select class="cap-select" id="capMsgShop">${shopOptions}</select><input class="cap-input" id="capMsgSearch" value="${e(f.search||'')}" placeholder="买家、客服姓名、会话内容或消息 ID"/><fieldset class="cap-message-role-filter"><legend>列表显示</legend>${capMessageRoleChecks(f)}</fieldset><select class="cap-select" id="capMsgWhitebox"><option value="all" ${f.whitebox==='all'?'selected':''}>全部会话</option><option value="yes" ${f.whitebox==='yes'?'selected':''}>包含白盒</option><option value="no" ${f.whitebox==='no'?'selected':''}>不含白盒</option></select><label class="cap-date-field"><span>最后活跃从</span><input class="cap-input" id="capMsgFrom" type="date" value="${e(f.from||'')}"/></label><label class="cap-date-field"><span>最后活跃到</span><input class="cap-input" id="capMsgTo" type="date" value="${e(f.to||'')}"/></label><button class="cap-btn primary" id="capMsgApply" type="button">查询</button><button class="cap-btn" id="capMsgReset" type="button">重置</button></div>
      <div class="cap-table-wrap"><table class="cap-table cap-conversation-table"><thead><tr><th>最后活跃</th><th>店铺 / 账号</th><th>买家</th><th>最后消息</th><th>上下文</th><th>操作</th></tr></thead><tbody>${rows||'<tr><td colspan="6"><div class="cap-empty small">没有符合条件的会话</div></td></tr>'}</tbody></table></div>
      <div class="cap-message-pages"><button class="cap-btn" id="capMsgPrev" type="button" ${capState.messagePage<=1?'disabled':''}>上一页</button><span>第 ${e(capState.messagePage)} / ${e(capState.messagePages)} 页</span><button class="cap-btn" id="capMsgNext" type="button" ${capState.messagePage>=capState.messagePages?'disabled':''}>下一页</button></div>`;
  }
  function capMessageCorrectionMarkup(data){
    if(!capHasPerm('capabilities.corrections'))return '';
    const message=data.message||{},related=data.whitebox_message||null;
    const visibleRoles=capState.conversationView?capMessageRoles(capState.conversationView):capMessageRoles();
    const target=message.role==='assistant_simulated'?message:(visibleRoles.includes('assistant_simulated')&&related?.role==='assistant_simulated'?related:null);
    if(!target||target.shadow_status!=='succeeded')return '';
    const saved=target.whitebox?.correction||{};
    if(saved.id)return `<div class="cap-correction-saved">已保存为待审核纠正 · ${e(saved.error_type||'other')} · ${e(saved.id)}</div>`;
    const session=data.session||capState.conversationView||{};
    return `<section class="cap-message-correction"><div class="cap-whitebox-head"><strong>纠正这条 AI 回复</strong><span>只保存待审核，不发送</span></div><form data-cap-message-correction="1" data-account="${e(session.account||capState.conversationView?.account||'')}" data-buyer="${e(session.buyer_id||capState.conversationView?.buyerId||'')}" data-msg-id="${e(target.msg_id||'')}"><label class="cap-field"><span>正确回复</span><textarea class="cap-textarea" name="expected_reply" maxlength="3000" required placeholder="填写正确、可直接回复买家的内容"></textarea></label><div class="cap-correction-grid"><label class="cap-field"><span>错误类型</span><select class="cap-select" name="error_type"><option value="tone">语气/情绪</option><option value="script_match">话术匹配</option><option value="knowledge">知识错误</option><option value="card_status">工具/卡状态</option><option value="sop">流程/SOP</option><option value="prompt_following">未遵循提示词</option><option value="should_handoff">应转人工</option><option value="hallucination">幻觉/编造</option><option value="other">其他</option></select></label><label class="cap-field"><span>改进动作</span><select class="cap-select" name="correction_action"><option value="auto">自动建议</option><option value="script">话术</option><option value="knowledge">知识</option><option value="sop">SOP</option><option value="rule">规则</option><option value="handoff">转人工</option><option value="prompt">Prompt</option><option value="regression_only">仅回归用例</option></select></label><label class="cap-field"><span>作用范围</span><select class="cap-select" name="apply_scope"><option value="shop">当前店铺</option><option value="issue">当前问题类型</option><option value="all_mobile_wifi">全部移动 WiFi</option><option value="all_shops">全部店铺</option></select></label><label class="cap-field"><span>错误说明</span><input class="cap-input" name="error_detail" maxlength="500" placeholder="可选"/></label></div><button class="cap-btn primary" data-message-correction-submit="1" type="button">保存为待审核纠正</button></form></section>`;
  }
  function capMessageDetailMarkup(data){
    const message=data.message||{},whiteboxMessage=data.whitebox_message||null,whitebox=whiteboxMessage?.whitebox||message.whitebox||{};
    const visibleRoles=capState.conversationView?capMessageRoles(capState.conversationView):capMessageRoles();
    const related=visibleRoles.includes('assistant_simulated')&&whiteboxMessage&&Number(whiteboxMessage.sequence||0)!==Number(message.sequence||0)?`<div class="cap-message-related"><strong>关联的模拟 AI 回复</strong><p>${e(whiteboxMessage.content||'')}</p></div>`:'';
    const latency=capShadowLatency(whiteboxMessage||message,whitebox);
    return `<div class="cap-message-detail"><div class="cap-meta"><span class="cap-tag blue">${e(capMessageRoleLabel(message.role,message))}</span><span class="cap-tag">${e(capMessageTime(message.ts,message.created_at))}</span>${latency?`<span class="cap-tag green" title="${e(latency.title)}">${e(latency.label)} ${e(latency.value)}</span>`:''}</div><div class="cap-message-full">${e(message.display_content||message.content||'')}</div>${related}${capMessageCorrectionMarkup(data)}<div class="cap-whitebox-head"><strong>对应白盒</strong><span>${Object.keys(whitebox).length?'完整原始数据':'该消息没有关联白盒'}</span></div>${Object.keys(whitebox).length?`<pre class="cap-whitebox-json">${e(jsonText(whitebox))}</pre>`:'<div class="cap-empty small">没有白盒信息</div>'}</div>`;
  }
  async function capOpenMessageDetail(sequence){
    const data=await fetchJSON(`${CAP_BASE}/messages/detail?sequence=${encodeURIComponent(sequence)}`);
    capModal('消息与白盒',capMessageDetailMarkup(data),'<button class="cap-btn" data-cap-close type="button">关闭</button>');
  }
  function capConversationQuery(account,buyer,before='',roles=capMessageRoleValues){
    const query=new URLSearchParams({account:String(account||''),buyer_id:String(buyer||''),limit:'100',roles:capMessageRoles({roles}).join(',')});if(before)query.set('before',String(before));return query;
  }
  function capRenderConversationModal(){
    const view=capState.conversationView;if(!view)return;
    const relatedParents=new Set(view.messages.filter(row=>row.has_whitebox&&row.parent_msg_id).map(row=>String(row.parent_msg_id)));
    const messages=view.messages.map(row=>{
      const role=String(row.role||''),roleClass=role==='user'?'user':role==='assistant_simulated'?'assistant':'service';
      const hasWhitebox=!!row.has_whitebox||relatedParents.has(String(row.msg_id||''));
      const latency=capShadowLatency(row);
      return `<button class="cap-conversation-bubble ${roleClass}${Number(view.selectedSequence)===Number(row.sequence)?' selected':''}" data-conversation-message="${e(row.sequence)}" type="button"><span class="cap-conversation-bubble-meta">${e(capMessageRoleLabel(role,row))} · ${e(capMessageTime(row.ts,row.created_at))}${row.episode_break?' · 新一段':''}${latency?` · <span title="${e(latency.title)}">${e(latency.label)} ${e(latency.value)}</span>`:''}</span><span class="cap-conversation-bubble-text">${e(row.display_content||row.content||'')}</span>${hasWhitebox?'<span class="cap-conversation-whitebox">白盒</span>':''}</button>`;
    }).join('');
    const older=view.hasMore?'<button class="cap-btn cap-conversation-older" id="capConversationOlder" type="button">加载更早消息</button>':'';
    const roles=capMessageRoles(view);
    const body=`<div class="cap-conversation-view"><section class="cap-conversation-main"><div class="cap-conversation-summary"><strong>${e(view.nickname||view.buyerId)}</strong><span>${e(view.shopName||view.shopId||'')} · ${e(view.account)} · ${e(view.total)} 条</span></div><fieldset class="cap-conversation-role-filter"><legend>详情显示与导出</legend>${capMessageRoleChecks({roles},'capConversationRole')}</fieldset><div class="cap-conversation-log" id="capConversationLog">${older}${messages||'<div class="cap-empty small">所选角色没有消息</div>'}</div></section><aside class="cap-conversation-inspector" id="capConversationInspector"><div class="cap-empty small">点击左侧任一消息查看对应白盒</div></aside></div>`;
    capModal('会话上下文',body,`<button class="cap-btn primary" data-message-export="1" data-message-roles="${e(roles.join(','))}" data-account="${e(view.account)}" data-buyer="${e(view.buyerId)}" type="button">导出勾选角色</button><button class="cap-btn" data-cap-close type="button">关闭</button>`);
    $('capModal').classList.add('conversation-open');
  }
  async function capOpenConversation(account,buyer){
    capModal('会话上下文','<div class="cap-loading">正在读取会话…</div>','<button class="cap-btn" data-cap-close type="button">关闭</button>');$('capModal').classList.add('conversation-open');
    try{
      const roles=[...capMessageRoleValues],data=await fetchJSON(`${CAP_BASE}/messages/conversation?${capConversationQuery(account,buyer,'',roles)}`);
      capState.conversationView={account:String(data.account||account),buyerId:String(data.buyer_id||buyer),nickname:data.nickname||buyer,shopId:data.shop_id||'',shopName:data.shop_name||'',roles,total:Number(data.message_total??data.msg_count??0),messages:data.messages||[],hasMore:!!data.has_more,nextBefore:data.next_before||null,selectedSequence:0};
      capRenderConversationModal();
      const log=$('capConversationLog');if(log)log.scrollTop=log.scrollHeight;
    }catch(error){capCloseModal();toast('会话读取失败：'+error.message);}
  }
  async function capReloadConversationRoles(roles){
    const view=capState.conversationView;if(!view)return;
    const log=$('capConversationLog');if(log)log.innerHTML='<div class="cap-loading">正在按角色读取会话…</div>';
    document.querySelectorAll('input[name="capConversationRole"]').forEach(input=>{input.disabled=true;});
    try{
      const data=await fetchJSON(`${CAP_BASE}/messages/conversation?${capConversationQuery(view.account,view.buyerId,'',roles)}`);
      view.roles=[...roles];view.total=Number(data.message_total??data.msg_count??0);view.messages=data.messages||[];view.hasMore=!!data.has_more;view.nextBefore=data.next_before||null;view.selectedSequence=0;capRenderConversationModal();
      const nextLog=$('capConversationLog');if(nextLog)nextLog.scrollTop=nextLog.scrollHeight;
    }catch(error){capRenderConversationModal();toast('会话角色切换失败：'+error.message);}
  }
  async function capLoadOlderConversation(button){
    const view=capState.conversationView;if(!view||!view.hasMore||!view.nextBefore)return;
    capButtonBusy(button,true,'加载中…');
    try{
      const data=await fetchJSON(`${CAP_BASE}/messages/conversation?${capConversationQuery(view.account,view.buyerId,view.nextBefore,view.roles)}`),merged=new Map();
      [...(data.messages||[]),...view.messages].forEach(row=>merged.set(String(row.sequence||row.msg_id),row));
      view.messages=Array.from(merged.values()).sort((a,b)=>Number(a.sequence||0)-Number(b.sequence||0));view.hasMore=!!data.has_more;view.nextBefore=data.next_before||null;capRenderConversationModal();
    }catch(error){toast('更早消息加载失败：'+error.message);capButtonBusy(button,false);}
  }
  async function capInspectConversationMessage(sequence){
    const view=capState.conversationView,inspector=$('capConversationInspector');if(!view||!inspector)return;
    view.selectedSequence=Number(sequence||0);document.querySelectorAll('.cap-conversation-bubble').forEach(row=>row.classList.toggle('selected',Number(row.dataset.conversationMessage)===view.selectedSequence));inspector.innerHTML='<div class="cap-loading">正在读取白盒…</div>';
    try{const data=await fetchJSON(`${CAP_BASE}/messages/detail?sequence=${encodeURIComponent(sequence)}`);if(capState.conversationView===view&&$('capConversationInspector'))$('capConversationInspector').innerHTML=capMessageDetailMarkup(data);}catch(error){if($('capConversationInspector'))$('capConversationInspector').innerHTML=`<div class="cap-empty small">${e(error.message)}</div>`;}
  }
  async function capDownloadConversation(account,buyer,button){
    const roles=String(button?.dataset.messageRoles||'').split(',').filter(role=>capMessageRoleValues.includes(role));
    if(!roles.length)return toast('请在会话详情中至少勾选一个角色');
    capButtonBusy(button,true,'导出中…');
    try{
      const query=new URLSearchParams({account:String(account||''),buyer_id:String(buyer||''),roles:roles.join(',')}),headers={};if(typeof authToken==='function'&&authToken())headers['X-User-Token']=authToken();
      const response=await fetch(apiUrl(`${CAP_BASE}/messages/export?${query}`),{headers});if(!response.ok){const detail=await response.json().catch(()=>({}));throw new Error(detail.error||'导出失败');}
      const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a'),disposition=response.headers.get('Content-Disposition')||'',match=disposition.match(/filename="?([^";]+)"?/i);a.href=url;a.download=match?.[1]||`conversation_${buyer}.zip`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(error){toast(error.message);}finally{capButtonBusy(button,false);}
  }
  async function capSubmitMessageCorrection(button){
    if(!capHasPerm('capabilities.corrections'))return toast('当前角色不能纠正 AI 回复');
    const form=button.closest('form[data-cap-message-correction]');if(!form)return;
    const values=new FormData(form),expected=String(values.get('expected_reply')||'').trim();if(!expected)return toast('请先填写正确回复');
    capButtonBusy(button,true,'保存中…');
    try{
      const result=await fetchJSON('/api/shadow/correct',{method:'POST',body:JSON.stringify({account:form.dataset.account,buyer_id:form.dataset.buyer,msg_id:form.dataset.msgId,expected_reply:expected,error_type:String(values.get('error_type')||'other'),error_detail:String(values.get('error_detail')||'').trim(),apply_scope:String(values.get('apply_scope')||'shop'),correction_action:String(values.get('correction_action')||'auto'),direct_send:false})});
      form.closest('.cap-message-correction').outerHTML=`<div class="cap-correction-saved">已保存为待审核纠正 · ${e(result.correction?.error_type||'other')} · ${e(result.correction?.id||'')}</div>`;toast('纠正已保存，等待审核');
    }catch(error){toast('纠正保存失败：'+error.message);capButtonBusy(button,false);}
  }
  function capOpenUserEditor(row=null){
    const roleOpts=($('capUserRoleOpts')?.innerHTML)||['owner','leader','agent','viewer'].map(r=>`<option value="${r}">${r}</option>`).join('');
    capModal(row?'改用户':'新增用户',`
      <div class="cap-grid">
        <label class="cap-field"><span>用户名 *</span>
          <input class="cap-input" id="capUserName" value="${e(row?.username||'')}" ${row?'readonly':''} placeholder="例如 cs_zhangsan"/>
        </label>
        <label class="cap-field"><span>显示名</span>
          <input class="cap-input" id="capUserDisplay" value="${e(row?.display_name||'')}" placeholder="张三"/>
        </label>
        <label class="cap-field"><span>角色</span>
          <select class="cap-select" id="capUserRole">${roleOpts}</select>
        </label>
        <label class="cap-field"><span>班组（可选）</span>
          <input class="cap-input" id="capUserTeam" value="${e(row?.team_id||'')}" placeholder="售后一组"/>
        </label>
        <label class="cap-field wide"><span>可服务店铺 ID（每行一个；留空=全部）</span>
          <textarea class="cap-textarea" id="capUserShops" placeholder="mall_150792824">${e((row?.shop_ids||[]).join('\n'))}</textarea>
        </label>
        <label class="cap-field wide"><span>${row?'新密码（不改请留空）':'初始密码 *'}</span>
          <input class="cap-input" id="capUserPassword" type="password" placeholder="${row?'留空则不修改':'至少 8 位'}"/>
        </label>
        <label class="cap-check wide"><input type="checkbox" id="capUserEnabled" ${row?.enabled===false?'':'checked'}>启用此账号</label>
      </div>
    `,`<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capUserSave" type="button">保存</button>`);
    if(row?.role){
      const sel=$('capUserRole');
      if(sel)sel.value=row.role;
    }
  }
  async function capSaveUser(button){
    const username=($('capUserName')?.value||'').trim();
    const isNew=!capState.usersEditing;
    const payload={
      username,
      display_name:($('capUserDisplay')?.value||'').trim(),
      role:$('capUserRole')?.value||'agent',
      team_id:($('capUserTeam')?.value||'').trim(),
      shop_ids:String($('capUserShops')?.value||'').split(/[\n,，]+/).map(s=>s.trim()).filter(Boolean),
      enabled:!!$('capUserEnabled')?.checked
    };
    const password=($('capUserPassword')?.value||'');
    if(password)payload.password=password;
    if(!username)return toast('请填写用户名');
    if(isNew && !password)return toast('请设置初始密码');
    capButtonBusy(button,true,'保存中…');
    try{
      if(isNew)await fetchJSON('/api/auth/users',{method:'POST',body:JSON.stringify(payload)});
      else await fetchJSON(`/api/auth/users/${encodeURIComponent(username)}`,{method:'PUT',body:JSON.stringify(payload)});
      capCloseModal();toast('用户已保存');capState.usersEditing=null;await capLoadUsers();
    }catch(error){toast(error.message);}
    finally{capButtonBusy(button,false);}
  }
  function capScriptLifecycleLabel(row){const scopes=row.lifecycle||['all'];return scopes.includes('all')?'全部阶段':scopes.includes('post_order')?'已下单咨询':'未下单咨询';}
  function capRenderScriptTest(payload){
    const result=payload?.hits||payload||{},selected=result.selected||{},candidate=selected.id?selected:(result.candidates||[])[0]||{};
    const direct=!!result.would_direct_reply,score=Math.round(Number(candidate.score||0)*100);
    const source={exact:'问法完全一致',lexical:'问法高度相似',semantic:'向量语义相同',hybrid:'文字和语义相似',static_package_catalog:'可信套餐价目表',llm_semantic:'AI 判断为同一问题'}[candidate.match_source]||'未达到固定回复条件';
    if(!candidate.id)return `<div class="cap-script-decision no"><strong>不会使用固定回复</strong><p>当前没有可用的话术候选。</p></div>`;
    return `<div class="cap-script-decision ${direct?'yes':'no'}"><div class="cap-script-decision-head"><strong>${direct?'会使用固定回复':'不会直接回复'}</strong><span class="cap-tag ${direct?'green':'orange'}">匹配度 ${e(score)}%</span></div>
      <dl><div><dt>最接近的话术</dt><dd>${e(candidate.title||'-')}</dd></div><div><dt>判断依据</dt><dd>${e(candidate.reason||source)}</dd></div><div><dt>匹配方式</dt><dd>${e(source)}</dd></div>${candidate.matched_trigger?`<div><dt>匹配到的客户问法</dt><dd>${e(candidate.matched_trigger)}</dd></div>`:''}</dl>
      ${direct?`<div class="cap-script-reply-preview"><span>将使用以下回复</span><p>${e(candidate.answer||'')}</p></div>`:'<p class="cap-help">本次不会直接使用固定文案，将继续由后续接待流程处理。</p>'}</div>`;
  }
  async function capLoadScripts(search=capState.scriptSearch,page=capState.scriptPage){
    capState.scriptSearch=String(search||'').trim();capState.scriptPage=Math.max(0,Number(page)||0);
    const offset=capState.scriptPage*100,query=new URLSearchParams({search:capState.scriptSearch,limit:'100',offset:String(offset)});
    const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts?${query}`);capState.scripts=data.scripts||[];const stats=data.stats||{},filtered=Number(data.filtered_count||0);
    const rows=capState.scripts.length?capState.scripts.map(row=>`
      <article class="cap-row"><div class="cap-row-top"><div class="cap-row-title"><strong>${e(row.title)}</strong><small>${e((row.triggers||[]).slice(0,4).join(' · ')||'未设置客户问法')}${(row.triggers||[]).length>4?` · 另 ${e(row.triggers.length-4)} 种`:''}</small></div>
      <div class="cap-row-actions"><button class="cap-btn" data-script-toggle="${e(row.id)}" data-enabled="${row.enabled?'1':'0'}" type="button">${row.enabled?'停用':'启用'}</button><button class="cap-btn" data-script-edit="${e(row.id)}" type="button">编辑</button><button class="cap-btn" data-script-copy="${e(row.id)}" type="button">复制</button><button class="cap-btn danger" data-script-delete="${e(row.id)}" type="button">删除</button></div></div>
      <div class="cap-row-content">${e(row.answer)}</div><div class="cap-meta"><span class="cap-tag ${row.enabled?'green':''}">${row.enabled?'使用中':'已停用'}</span><span class="cap-tag ${row.match_mode==='direct'?'blue':''}">${row.match_mode==='direct'?'命中后固定回复':'只供 AI 参考'}</span><span class="cap-tag">${e(capScriptLifecycleLabel(row))}</span>${row.direct_in_post_order?'<span class="cap-tag green">已下单可固定回复</span>':''}<span class="cap-tag">${(row.product_ids||[]).length?'指定商品':'全店商品'}</span>${(row.workflow_steps||[]).length?`<span class="cap-tag blue">SOP ${e(row.workflow_steps.length)} 步</span>`:''}${(row.negative_triggers||[]).length?`<span class="cap-tag orange">有 ${e(row.negative_triggers.length)} 条排除问法</span>`:''}</div></article>`).join(''):'<div class="cap-empty">没有找到符合条件的话术。</div>';
    const pageCount=Math.max(1,Math.ceil(filtered/100));
    $('capContent').innerHTML=`
      <div class="cap-section-head"><div><h2>固定回复库</h2><p>按店铺维护触发问法、审核回复和可选 SOP；保存后实时生效，不需要修改代码。</p></div><div class="cap-actions"><button class="cap-btn" id="capScriptTemplate" type="button">下载导入模板</button><button class="cap-btn" id="capScriptSemanticRebuild" type="button">更新语义索引</button><button class="cap-btn" id="capScriptImport" type="button">批量导入</button><button class="cap-btn" id="capScriptExport" type="button">导出备份</button><button class="cap-btn primary" id="capScriptAdd" type="button">新增固定回复</button></div></div>
      <div class="cap-stats"><div class="cap-stat"><div class="v">${e(stats.total||0)}</div><div class="k">全部回复</div></div><div class="cap-stat"><div class="v">${e(stats.enabled||0)}</div><div class="k">正在使用</div></div><div class="cap-stat"><div class="v">${e(stats.trusted||0)}</div><div class="k">审核可用</div></div><div class="cap-stat"><div class="v">${e(stats.embedded||0)}</div><div class="k">已建立语义索引</div></div></div>
      <div class="cap-card"><div class="cap-card-head"><strong>先试一句客户问题</strong><span class="cap-tag green">只测试，不发送</span></div><div class="cap-card-body"><div class="cap-toolbar"><input class="cap-input" id="capScriptQuestion" placeholder="输入客户的原话"/><select class="cap-select" id="capScriptLifecycle"><option value="presale_no_order">客户未下单</option><option value="post_order">客户已下单</option></select><input class="cap-input" id="capScriptProduct" placeholder="商品 ID（可不填）"/><button class="cap-btn primary" id="capScriptTest" type="button">查看会怎么处理</button></div><div id="capScriptResult"></div></div></div>
      <div class="cap-script-listbar"><div class="cap-toolbar"><input class="cap-input" id="capScriptSearch" value="${e(capState.scriptSearch)}" placeholder="搜索问题、回复或分类"/><button class="cap-btn" id="capScriptSearchBtn" type="button">搜索</button>${capState.scriptSearch?'<button class="cap-btn" id="capScriptSearchClear" type="button">清除</button>':''}</div><span>共 ${e(filtered)} 条${filtered>100?`，第 ${e(capState.scriptPage+1)} / ${e(pageCount)} 页`:''}</span></div>
      <div class="cap-list">${rows}</div>${pageCount>1?`<div class="cap-script-pages"><button class="cap-btn" id="capScriptPrev" ${capState.scriptPage<=0?'disabled':''} type="button">上一页</button><button class="cap-btn" id="capScriptNext" ${capState.scriptPage>=pageCount-1?'disabled':''} type="button">下一页</button></div>`:''}`;
  }
  function capScriptRowsFromJSON(payload){
    const rows=Array.isArray(payload)?payload:(payload&&typeof payload==='object'?(payload.scripts??payload.items):null);
    if(!Array.isArray(rows))throw new Error('JSON 顶层应为话术数组，或包含 scripts/items 数组');
    if(!rows.length)throw new Error('JSON 中没有可导入的话术');
    if(rows.length>10000)throw new Error('单次最多导入 10000 条话术');
    return rows;
  }
  function capOpenScriptImportDialog(rows,fileName=''){
    capState.scriptImportRows=rows;
    const preview=rows.slice(0,8).map((row,index)=>{
      const title=row&&typeof row==='object'?(row.title||`第 ${index+1} 条`):`第 ${index+1} 条`;
      const valid=!!(row&&typeof row==='object'&&String(row.title||'').trim()&&String(row.answer||'').trim()&&splitList(row.triggers).length);
      return `<div class="cap-row"><div class="cap-row-top"><div class="cap-row-title"><strong>${e(title)}</strong><small>${valid?'字段完整':'缺少标题、标准回复或触发问法，导入时会失败'}</small></div><span class="cap-tag ${valid?'green':'orange'}">${valid?'可导入':'待检查'}</span></div></div>`;
    }).join('');
    capModal('批量导入固定回复',`
      <div class="cap-notice safe" style="margin-bottom:10px">
        文件：<strong>${e(fileName||'导入文件')}</strong> · 共 ${e(rows.length)} 条。<br>
        将新增到店铺「${e(capShopName(capState.shops.find(row=>row.shop_id===capState.shopId)))}」，不会覆盖已有话术；服务器会逐条校验。
      </div>
      <div class="cap-list">${preview}${rows.length>8?`<div class="cap-empty">另有 ${e(rows.length-8)} 条未展示</div>`:''}</div>
    `,'<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capScriptImportConfirm" type="button">确认导入</button>');
  }
  async function capImportScriptsFromFile(button){
    const input=document.createElement('input');
    input.type='file';input.accept='.csv,.json,text/csv,application/json';
    input.onchange=async()=>{
      const file=input.files?.[0];if(!file)return;
      if(file.size>30*1024*1024)return toast('文件过大，话术 JSON 上限 30MB');
      capButtonBusy(button,true,'读取中…');
      try{
        const text=(await file.text()).replace(/^\uFEFF/,'');
        const preview=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/import-preview`,{method:'POST',body:JSON.stringify({filename:file.name,content:text})});
        capOpenScriptImportDialog(preview.scripts||[],file.name);
      }catch(error){toast('读取失败：'+(error?.message||'请选择合法的 CSV 或 JSON 文件'));}
      finally{capButtonBusy(button,false);}
    };
    input.click();
  }
  async function capConfirmScriptImport(button){
    const rows=capState.scriptImportRows;
    if(!Array.isArray(rows)||!rows.length)return toast('导入内容已失效，请重新选择文件');
    capButtonBusy(button,true,'导入中…');
    try{
      let saved=0,errors=[];
      for(let start=0;start<rows.length;start+=250){
        button.textContent=`正在导入 ${Math.min(start+250,rows.length)} / ${rows.length}`;
        const result=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/import`,{method:'POST',body:JSON.stringify({scripts:rows.slice(start,start+250)})});
        saved+=Array.isArray(result.saved)?result.saved.length:0;
        errors=errors.concat((Array.isArray(result.errors)?result.errors:[]).map(item=>({...item,index:Number(item.index||0)+start})));
      }
      capState.scriptImportRows=null;
      capCloseModal();
      toast(errors.length?`导入完成：成功 ${saved} 条，失败 ${errors.length} 条`:`成功导入 ${saved} 条话术`);
      await capLoadScripts();
    }catch(error){toast('导入失败：'+error.message);}
    finally{capButtonBusy(button,false);}
  }
  function capOpenScriptEditor(row=null){
    const item=row||{title:'',triggers:[],negative_triggers:[],answer:'',category:'',tags:[],product_ids:[],lifecycle:['presale_no_order'],enabled:true,trusted:true,match_mode:'direct',priority:50,semantic_enabled:true,semantic_threshold:.50,min_margin:.08,direct_in_post_order:false,issue_type:'',workflow_steps:[],success_criteria:''};
    const lifecycle=(item.lifecycle||[]).includes('all')?'all':(item.lifecycle||[]).includes('post_order')?'post_order':'presale_no_order',specified=(item.product_ids||[]).length>0;
    capModal(row?'编辑固定回复':'新增固定回复',`<form id="capScriptForm" data-id="${e(item.id||'')}"><div class="cap-script-editor">
      <label class="cap-field"><span>这条回复解决什么问题？ *</span><input class="cap-input" name="title" value="${e(item.title)}" placeholder="例如：客户询问多久发货" required/></label>
      <label class="cap-field"><span>客户可能会怎么问？每行写一种说法 *</span><textarea class="cap-textarea" name="triggers" required placeholder="多久发货&#10;今天能发吗&#10;什么时候可以寄出">${e(listText(item.triggers))}</textarea></label>
      <label class="cap-field"><span>哪些相似问题不应该用这条回复？每行一个</span><textarea class="cap-textarea" name="negative_triggers" placeholder="例如：已经发货了吗">${e(listText(item.negative_triggers))}</textarea></label>
      <label class="cap-field"><span>命中后回复客户的内容 *</span><textarea class="cap-textarea" name="answer" required style="min-height:130px">${e(item.answer)}</textarea></label>
      <div class="cap-route-step-block">
        <div class="cap-route-step-label"><span>1</span>白盒里显示什么处理流程？（可选）</div>
        <label class="cap-field"><span>问题类型</span><input class="cap-input" name="issue_type" value="${e(item.issue_type||'')}" placeholder="例如：体验权益领取"/></label>
        <p class="cap-help" style="margin:8px 0 6px">每行一步，格式：步骤名 | 动作类型。这里用于运营说明和白盒展示，不会自动执行工具。</p>
        <textarea class="cap-textarea" name="workflow_steps" style="min-height:110px" placeholder="例如：&#10;确认买家是否符合领取条件 | verify&#10;告知店铺确认过的领取入口 | guide">${e(capWfStepsEditorText(item.workflow_steps||[]))}</textarea>
        <label class="cap-field" style="margin-top:8px"><span>怎样算处理完成</span><textarea class="cap-textarea" name="success_criteria" placeholder="例如：买家确认已经进入领取页面">${e(item.success_criteria||'')}</textarea></label>
      </div>
      <div class="cap-grid"><label class="cap-field"><span>适用于</span><select class="cap-select" name="lifecycle"><option value="presale_no_order" ${lifecycle==='presale_no_order'?'selected':''}>未下单咨询</option><option value="post_order" ${lifecycle==='post_order'?'selected':''}>已下单咨询</option><option value="all" ${lifecycle==='all'?'selected':''}>全部阶段</option></select></label>
      <label class="cap-field"><span>回复方式</span><select class="cap-select" name="match_mode"><option value="direct" ${item.match_mode==='direct'?'selected':''}>命中后直接使用固定回复</option><option value="reference" ${item.match_mode!=='direct'?'selected':''}>只提供给 AI 参考</option></select></label>
      <label class="cap-field"><span>适用商品</span><select class="cap-select" name="product_scope" id="capScriptProductScope"><option value="all" ${!specified?'selected':''}>全店商品</option><option value="specified" ${specified?'selected':''}>指定商品</option></select></label>
      <label class="cap-field" id="capScriptProductIds" ${specified?'':'hidden'}><span>商品 ID（逗号或换行分隔）</span><input class="cap-input" name="product_ids" value="${e((item.product_ids||[]).join(', '))}"/></label></div>
      <label class="cap-check"><input type="checkbox" name="enabled" ${item.enabled!==false?'checked':''}>保存后立即使用</label>
      <label class="cap-check"><input type="checkbox" name="direct_in_post_order" ${item.direct_in_post_order?'checked':''}>已下单时也允许直接使用这条固定回复（只勾选不依赖实时卡状态的标准答案）</label>
      <details class="cap-route-advanced-box"><summary>高级设置</summary><div class="cap-grid compact">
        <label class="cap-field"><span>业务分类</span><input class="cap-input" name="category" value="${e(item.category)}" placeholder="售前 / 物流 / 售后"/></label><label class="cap-field"><span>内部标签</span><input class="cap-input" name="tags" value="${e((item.tags||[]).join(', '))}"/></label>
        <label class="cap-field"><span>最低匹配度（0.30-1.00）</span><input class="cap-input" name="semantic_threshold" type="number" min="0.3" max="1" step="0.01" value="${e(item.semantic_threshold??.50)}"/></label><label class="cap-field"><span>与第二候选最小差值</span><input class="cap-input" name="min_margin" type="number" min="0" max="0.5" step="0.01" value="${e(item.min_margin??.08)}"/></label>
        <label class="cap-field"><span>优先级（0-100）</span><input class="cap-input" name="priority" type="number" min="0" max="100" value="${e(item.priority??50)}"/></label><div class="cap-toolbar"><label class="cap-check"><input type="checkbox" name="semantic_enabled" ${item.semantic_enabled!==false?'checked':''}>参与语义识别</label><label class="cap-check"><input type="checkbox" name="trusted" ${item.trusted!==false?'checked':''}>已审核</label></div>
      </div></details></div></form>`,
      '<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capScriptSave" type="button">保存固定回复</button>');
  }
  async function capSaveScript(button){
    const form=$('capScriptForm');if(!form.reportValidity())return;const fd=new FormData(form),id=form.dataset.id;
    let workflowSteps=[];try{workflowSteps=capWfParseSteps(fd.get('workflow_steps'));}catch(error){return toast(error.message);}
    const lifecycle=fd.get('lifecycle'),payload={title:fd.get('title'),category:fd.get('category'),answer:fd.get('answer'),triggers:splitList(fd.get('triggers')),negative_triggers:splitList(fd.get('negative_triggers')),tags:splitList(fd.get('tags')),product_ids:fd.get('product_scope')==='specified'?splitList(fd.get('product_ids')):[],lifecycle:[lifecycle],match_mode:fd.get('match_mode'),priority:Number(fd.get('priority')||50),semantic_threshold:Number(fd.get('semantic_threshold')||.50),min_margin:Number(fd.get('min_margin')||.08),semantic_enabled:fd.get('semantic_enabled')==='on',enabled:fd.get('enabled')==='on',trusted:fd.get('trusted')==='on',direct_in_post_order:fd.get('direct_in_post_order')==='on',issue_type:String(fd.get('issue_type')||'').trim(),workflow_steps:workflowSteps,success_criteria:String(fd.get('success_criteria')||'').trim()};
    if(fd.get('product_scope')==='specified'&&!payload.product_ids.length)return toast('请填写适用的商品 ID');
    capButtonBusy(button,true,'保存中…');try{await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts${id?`/${encodeURIComponent(id)}`:''}`,{method:id?'PUT':'POST',body:JSON.stringify(payload)});capCloseModal();toast('话术已保存');await capLoadScripts();}catch(error){toast('保存失败：'+error.message);}finally{capButtonBusy(button,false);}
  }
  async function capLoadKnowledge(){
    const shop=encodeURIComponent(capState.shopId);const [stats,files,docs]=await Promise.all([fetchJSON(`${CAP_BASE}/shops/${shop}/knowledge/stats`),fetchJSON(`${CAP_BASE}/shops/${shop}/knowledge/files`),fetchJSON(`${CAP_BASE}/shops/${shop}/knowledge/docs?limit=100`)]);
    const fileRows=(files.files||[]).map(f=>`<tr><td>${e(f.name)}</td><td>${fmtBytes(f.size)}</td><td>${e(fmtTime(f.modified_at))}</td></tr>`).join('');
    const docRows=(docs.documents||[]).map(d=>`<article class="cap-row"><div class="cap-row-title"><strong>${e(d.source_name||'知识文档')}</strong><small>${e(jsonText(d.metadata||{}))}</small></div><div class="cap-row-content">${e(d.text)}</div></article>`).join('');
    $('capContent').innerHTML=`
      <div class="cap-section-head"><div><h2>知识库</h2><p>本地词法检索。可从商品库一键灌入商品档案，也可上传政策/说明书等资料。</p></div><div class="cap-actions"><button class="cap-btn" id="capKbSyncProducts" type="button">从商品库同步</button><button class="cap-btn" id="capKbRebuild" type="button">重建索引</button><button class="cap-btn primary" id="capKbUploadBtn" type="button">上传资料</button><input id="capKbFile" type="file" accept=".txt,.csv,.json,.jsonl,.md" hidden/></div></div>
      <div class="cap-stats"><div class="cap-stat"><div class="v">${e(stats.document_count||0)}</div><div class="k">文档切片</div></div><div class="cap-stat"><div class="v">${e(stats.source_file_count||0)}</div><div class="k">源文件</div></div><div class="cap-stat"><div class="v">本地</div><div class="k">存储位置</div></div><div class="cap-stat"><div class="v">词法</div><div class="k">检索后端</div></div></div>
      <div class="cap-card"><div class="cap-card-head"><strong>检索测试</strong><span class="cap-tag green">只检索，不回复</span></div><div class="cap-card-body"><div class="cap-toolbar"><input class="cap-input" id="capKbQuestion" placeholder="输入问题测试知识命中"/><input class="cap-input" id="capKbProduct" placeholder="商品 ID（可选）"/><button class="cap-btn primary" id="capKbQuery" type="button">开始检索</button></div><div id="capKbResult"></div></div></div>
      <div class="cap-card"><div class="cap-card-head"><strong>源文件</strong><span class="cap-tag">${e((files.files||[]).length)} 个</span></div><div class="cap-card-body">${fileRows?`<div class="cap-table-wrap"><table class="cap-table"><thead><tr><th>文件名</th><th>大小</th><th>更新时间</th></tr></thead><tbody>${fileRows}</tbody></table></div>`:'<div class="cap-empty">暂无源文件</div>'}</div></div>
      <div class="cap-card"><div class="cap-card-head"><strong>文档内容（最多显示 100 条）</strong><span class="cap-tag">${e((docs.documents||[]).length)} 条</span></div><div class="cap-card-body"><div class="cap-list">${docRows||'<div class="cap-empty">当前店铺暂无知识文档</div>'}</div></div></div>`;
  }
  async function capFileBase64(file){const buffer=await file.arrayBuffer(),bytes=new Uint8Array(buffer);let binary='';for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));return btoa(binary);}
  async function capLoadModels(){
    const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/models`),llm=data.settings?.llm||{},embedding=data.settings?.embedding||{};
    $('capContent').innerHTML=`
      <div class="cap-section-head"><div><h2>大模型配置</h2><p>当前店铺独立配置；API Key 仅显示掩码，保存掩码时保留本店原密钥。</p></div><div class="cap-actions"><button class="cap-btn" id="capModelTest" type="button">连接测试</button><button class="cap-btn primary" id="capModelSave" type="button">保存配置</button></div></div>
      <div class="cap-notice">配置用于本地能力中心。当前真实发送被关闭，模型测试只发送一条连接验证请求，不会触发买家回复。</div>
      <form class="cap-card" id="capModelForm"><div class="cap-card-body"><div class="cap-grid">
      <label class="cap-field"><span>Provider</span><select class="cap-select" name="provider"><option value="custom" ${llm.provider==='custom'?'selected':''}>Custom / OpenAI 兼容</option><option value="openai" ${llm.provider==='openai'?'selected':''}>OpenAI</option></select></label><label class="cap-field"><span>模型名称</span><input class="cap-input" name="model_name" value="${e(llm.model_name)}" placeholder="模型 ID"/></label>
      <label class="cap-field wide"><span>Base URL（不含 /chat/completions）</span><input class="cap-input" name="base_url" value="${e(llm.base_url)}" placeholder="https://example.com/v1"/></label><label class="cap-field wide"><span>API Key</span><input class="cap-input" name="api_key" type="password" value="${e(llm.api_key||'')}" autocomplete="new-password"/></label>
      <label class="cap-field"><span>请求超时（秒）</span><input class="cap-input" name="request_timeout_seconds" type="number" min="3" max="120" value="${e(llm.request_timeout_seconds||30)}"/></label><label class="cap-field"><span>额外请求头（JSON）</span><textarea class="cap-textarea" name="extra_headers" style="min-height:80px">${e(jsonText(llm.extra_headers||{}))}</textarea></label>
      <div class="wide cap-toolbar"><label class="cap-check"><input type="checkbox" name="allow_empty_api_key" ${llm.allow_empty_api_key?'checked':''}>允许空 API Key</label><label class="cap-check"><input type="checkbox" name="trace_enabled" ${llm.trace_enabled!==false?'checked':''}>记录本地调用跟踪</label></div></div>
      <details class="cap-route-advanced-box"><summary>固定回复语义识别（向量模型）</summary><div class="cap-grid compact"><div class="wide cap-toolbar"><label class="cap-check"><input type="checkbox" name="embedding_enabled" ${embedding.enabled?'checked':''}>启用向量语义匹配</label><label class="cap-check"><input type="checkbox" name="embedding_inherit" ${embedding.inherit_llm_credentials!==false?'checked':''}>沿用上方地址和密钥</label></div><label class="cap-field"><span>向量模型名称</span><input class="cap-input" name="embedding_model_name" value="${e(embedding.model_name||'')}" placeholder="例如 text-embedding-3-small"/></label><label class="cap-field"><span>请求超时（秒）</span><input class="cap-input" name="embedding_timeout" type="number" min="3" max="120" value="${e(embedding.request_timeout_seconds||30)}"/></label><label class="cap-field wide"><span>独立 Base URL（沿用时可不填）</span><input class="cap-input" name="embedding_base_url" value="${e(embedding.base_url||'')}"/></label><label class="cap-field wide"><span>独立 API Key（沿用时可不填）</span><input class="cap-input" name="embedding_api_key" type="password" value="${e(embedding.api_key||'')}" autocomplete="new-password"/></label></div></details>
      </div></form><div id="capModelResult"></div>`;
  }
  async function capLoadPaymentReminder(){
    const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/payment-reminder`),settings=data.settings||{},stats=data.stats||{},history=data.history||[],reminders=Array.isArray(settings.reminders)?settings.reminders:[],first=reminders[0]||{},second=reminders[1]||{},sendCount=Number(settings.send_count||1);
    capState.paymentReminder=settings;
    const rows=history.map(row=>`<tr><td>${e(fmtTime(row.attempted_at))}</td><td>第 ${e(row.attempt_number||1)} 次</td><td>${e(row.buyer_id||'-')}</td><td><span class="cap-tag ${row.status==='sent'?'green':'red'}">${row.status==='sent'?'已发送':'失败'}</span></td><td>${e(row.error||row.delivery_status||'-')}</td></tr>`).join('');
    $('capContent').innerHTML=`
      <div class="cap-section-head"><div><h2>催付设置</h2><p>客户进线咨询后，仍未下单时最多发送两次挽留消息。</p></div><div class="cap-actions"><button class="cap-btn primary" id="capPaymentReminderSave" type="button">保存设置</button></div></div>
      <div class="cap-notice warn">只处理启用后产生、订单查询明确为 0 笔且会话已静默的咨询。实际发送仍受店铺大脑与发送权限控制。</div>
      <form class="cap-card" id="capPaymentReminderForm" onsubmit="return false"><div class="cap-card-body"><div class="cap-grid">
        <div class="wide cap-toolbar"><label class="cap-check"><input type="checkbox" name="enabled" ${settings.enabled?'checked':''}>启用未下单催付</label></div>
        <label class="cap-field"><span>催付次数</span><select class="cap-select" id="capPaymentReminderCount" name="send_count"><option value="1" ${sendCount===1?'selected':''}>1 次</option><option value="2" ${sendCount===2?'selected':''}>2 次</option></select></label>
        <fieldset class="cap-reminder-step wide"><legend>第 1 次催付</legend><div class="cap-grid"><label class="cap-field"><span>会话静默时间（秒）</span><input class="cap-input" name="delay_seconds_1" type="number" min="1" max="86400" step="1" value="${e(first.delay_seconds||1800)}"></label><label class="cap-field wide"><span>发送内容</span><textarea class="cap-textarea" name="message_1" maxlength="1000" rows="5">${e(first.message||'')}</textarea></label></div></fieldset>
        <fieldset class="cap-reminder-step wide" id="capPaymentReminderStep2" ${sendCount===2?'':'hidden'}><legend>第 2 次催付</legend><div class="cap-grid"><label class="cap-field"><span>第 1 次发送后继续静默（秒）</span><input class="cap-input" name="delay_seconds_2" type="number" min="1" max="86400" step="1" value="${e(second.delay_seconds||7200)}"></label><label class="cap-field wide"><span>发送内容</span><textarea class="cap-textarea" name="message_2" maxlength="1000" rows="5">${e(second.message||'')}</textarea></label></div></fieldset>
        <div class="cap-notice wide">当前发送通道为纯文本：支持换行和 emoji，不渲染 HTML 富文本，暂不能发送图片。</div>
      </div></div></form>
      <div class="cap-stats" style="margin-top:10px"><div class="cap-stat"><div class="v">${settings.enabled?'已开启':'未开启'}</div><div class="k">当前状态</div></div><div class="cap-stat"><div class="v">${e(stats.sent||0)}</div><div class="k">发送成功</div></div><div class="cap-stat"><div class="v">${e(stats.failed||0)}</div><div class="k">发送失败</div></div><div class="cap-stat"><div class="v">${e(stats.attempted||0)}</div><div class="k">累计尝试</div></div></div>
      <div class="cap-card"><div class="cap-card-head"><strong>最近执行</strong><span class="cap-tag">最多显示 20 条</span></div><div class="cap-card-body">${rows?`<div class="cap-table-wrap"><table class="cap-table"><thead><tr><th>时间</th><th>批次</th><th>买家</th><th>结果</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<div class="cap-empty">暂无执行记录</div>'}</div></div>`;
  }
  function capModelPayload(){const fd=new FormData($('capModelForm'));return{llm:{provider:fd.get('provider'),model_name:fd.get('model_name'),base_url:fd.get('base_url'),api_key:fd.get('api_key'),request_timeout_seconds:Number(fd.get('request_timeout_seconds')||30),extra_headers:parseJSON(fd.get('extra_headers'),{},'额外请求头'),allow_empty_api_key:fd.get('allow_empty_api_key')==='on',trace_enabled:fd.get('trace_enabled')==='on'},embedding:{enabled:fd.get('embedding_enabled')==='on',inherit_llm_credentials:fd.get('embedding_inherit')==='on',model_name:fd.get('embedding_model_name'),base_url:fd.get('embedding_base_url'),api_key:fd.get('embedding_api_key'),request_timeout_seconds:Number(fd.get('embedding_timeout')||30)}};}
  async function capLoadPrompts(){
    const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/prompts`);capState.prompts=data.prompts||{};if(!(capState.promptName in capState.prompts))capState.promptName=Object.keys(capState.prompts)[0]||'default';
    const labels={classify:'意图分类',presale:'售前',aftersale:'售后',shipping:'物流',default:'默认'};const options=Object.keys(capState.prompts).map(name=>`<option value="${e(name)}" ${name===capState.promptName?'selected':''}>${e(labels[name]||name)}</option>`).join('');
    $('capContent').innerHTML=`<div class="cap-section-head"><div><h2>提示词</h2><p>只影响当前店铺的后续本地模拟回复；不会影响其他店铺。</p></div><div class="cap-actions"><button class="cap-btn primary" id="capPromptSave" type="button">保存提示词</button></div></div><div class="cap-card"><div class="cap-card-body"><label class="cap-field"><span>提示词类型</span><select class="cap-select" id="capPromptSelect">${options}</select></label><label class="cap-field" style="margin-top:10px"><span>内容</span><textarea class="cap-textarea" id="capPromptContent" style="min-height:430px">${e(capState.prompts[capState.promptName]||'')}</textarea></label></div></div>`;
  }
  function capTriOptions(){return '<option value="">不知道</option><option value="true">是</option><option value="false">否</option>';}
  function capRouteStageText(stage){return({
    presale_no_order:'还没买设备',
    ordered_not_received:'已下单，货还在路上',
    received_no_package:'货收到了，还没买套餐',
    package_customer:'已经买了设备+套餐',
    device_customer_unknown_stage:'买过设备，但具体情况不清楚',
    facts_unknown:'订单情况还不清楚',
    '*':'任意阶段（都适用）'
  })[stage]||stage;}
  function capIntentCatalog(){return Array.isArray(window.__INTENT_CATALOG__)?window.__INTENT_CATALOG__:[];}
  function capOperatorIntentCodes(){return capIntentCatalog().filter(row=>row?.operator_selectable).map(row=>String(row.code||'')).filter(Boolean);}
  function capRouteIntentText(intent){
    if(intent==='*')return '任意问题（都适用）';
    return capIntentCatalog().find(row=>row?.code===intent)?.label||intent;
  }
  function capRouteTargetText(target){
    const plain={
      presale_script:'用售前话术接待（推荐、解答购买前问题）',
      logistics_support:'按物流/收货来接待（催件、运费、到哪了）',
      activation_support:'教他激活和使用',
      package_sales:'推荐/讲解套餐和充值',
      activation_then_package:'先帮他用起来，再适度提套餐',
      aftersales_diagnosis:'做售后排查（设备/卡/网络状态）',
      package_service:'处理套餐状态、续费、到期',
      product_clarify:'先问清是哪款商品/设备，再回答参数',
      human_handoff:'交给人工客服处理',
      hybrid_fallback:'信息不够，先问清楚再继续'
    };
    if(plain[target])return plain[target];
    return (capState.routingTargets&&capState.routingTargets[target])||target||'默认接待方式';
  }
  function capRouteJoinList(items,mapper){
    const list=(items||[]).map(mapper).filter(Boolean);
    if(!list.length)return '不限';
    if(list.length===1)return list[0];
    if(list.length===2)return list.join(' 或 ');
    return list.slice(0,-1).join('、')+' 或 '+list[list.length-1];
  }
  function capRouteStageOptions(selected){
    const all=['*','presale_no_order','ordered_not_received','received_no_package','package_customer','device_customer_unknown_stage','facts_unknown'];
    const sel=new Set(selected||[]);
    return all.map(v=>`<label class="cap-check"><input type="checkbox" name="capRouteStage" value="${e(v)}" ${sel.has(v)?'checked':''}> ${e(capRouteStageText(v))}</label>`).join('');
  }
  function capRouteIntentOptions(selected){
    const sel=new Set((selected||[]).map(String));
    const all=['*',...capOperatorIntentCodes(),...[...sel].filter(value=>value!=='*'&&!capOperatorIntentCodes().includes(value))];
    return all.map(v=>`<label class="cap-check"><input type="checkbox" name="capRouteIntent" value="${e(v)}" ${sel.has(v)?'checked':''}> ${e(capRouteIntentText(v))}</label>`).join('');
  }
  function capFhIntentOptions(selected){
    const supported=capOperatorIntentCodes();
    const configured=(selected||[]).map(v=>String(v||'').trim()).filter(Boolean);
    const all=[...supported,...configured.filter(v=>!supported.includes(v))];
    const sel=new Set(configured);
    return all.map(v=>`<label class="cap-check"><input type="checkbox" name="capFhL2Intent" value="${e(v)}" ${sel.has(v)?'checked':''}> ${e(capRouteIntentText(v))}</label>`).join('');
  }
  function capFhTestIntentOptions(){
    const all=capOperatorIntentCodes();
    return `<option value="">自动识别买家诉求</option>${all.map(v=>`<option value="${e(v)}">${e(capRouteIntentText(v))}</option>`).join('')}`;
  }
  async function capLoadRouting(){
    const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/routing`);
    capState.routingRules=data.rules||[];
    capState.routingSettings=data.settings||{};
    capState.routingTargets=data.targets||{};
    const settings=capState.routingSettings;
    const rows=capState.routingRules.map((row,idx)=>{
      const stages=capRouteJoinList(row.stages,capRouteStageText);
      const intents=capRouteJoinList(row.intents,capRouteIntentText);
      const target=capRouteTargetText(row.target);
      return `<article class="cap-route-card ${row.enabled?'':'is-off'}">
        <div class="cap-route-card-top">
          <div class="cap-route-ord" title="越靠前越先匹配">第 ${e(idx+1)} 条</div>
          <div class="cap-route-card-title">
            <strong>${e(row.name||('规则 '+(idx+1)))}</strong>
            <span class="cap-tag ${row.enabled?'green':'red'}">${row.enabled?'使用中':'已关掉'}</span>
          </div>
          <div class="cap-row-actions">
            <button class="cap-btn" data-routing-edit="${e(row.id)}" type="button">改</button>
            <button class="cap-btn" data-routing-toggle="${e(row.id)}" data-enabled="${row.enabled?'1':'0'}" type="button">${row.enabled?'关掉':'打开'}</button>
            <button class="cap-btn danger" data-routing-delete="${e(row.id)}" type="button">删</button>
          </div>
        </div>
        <div class="cap-route-ifthen">
          <div class="cap-route-line"><em>如果客户是</em><strong>${e(stages)}</strong></div>
          <div class="cap-route-line"><em>并且在问</em><strong>${e(intents)}</strong></div>
          <div class="cap-route-line result"><em>那就</em><strong>${e(target)}</strong></div>
        </div>
        ${row.description?`<p class="cap-route-why">为什么这样：${e(row.description)}</p>`:''}
      </article>`;
    }).join('');
    $('capContent').innerHTML=`
      <div class="cap-section-head">
        <div>
          <h2>接待分工</h2>
          <p>不用背术语。这里只做一件事：告诉 AI「什么情况用什么方式接待」。</p>
        </div>
        <div class="cap-actions">
          <button class="cap-btn" id="capRoutingBootstrap" type="button">恢复系统推荐规则</button>
          <button class="cap-btn primary" id="capRoutingAdd" type="button">加一条规则</button>
        </div>
      </div>

      <div class="cap-route-story">
        <div class="cap-route-story-title">系统怎么想（3 步）</div>
        <ol class="cap-route-steps">
          <li><span>1</span><div><strong>客户到哪一步了？</strong><small>还没买 / 货在路上 / 已收货 / 已买套餐…</small></div></li>
          <li><span>2</span><div><strong>这句话在问什么？</strong><small>物流、套餐、激活、故障、投诉…</small></div></li>
          <li><span>3</span><div><strong>用哪套方式接待？</strong><small>售前话术、物流处理、教激活、转人工…</small></div></li>
        </ol>
        <p class="cap-route-story-tip">下面每一条规则，都是一句「如果…并且…那就…」。从上到下比，<b>先对上的那条生效</b>。这里只做模拟判断，<b>不会真发给买家</b>。</p>
      </div>

      <section class="cap-card cap-route-try">
        <div class="cap-card-head">
          <div>
            <strong>先试一试（推荐）</strong>
            <small>写一句买家原话，看看系统会怎么分</small>
          </div>
          <button class="cap-btn primary" id="capRoutingTest" type="button">看看会怎么接待</button>
        </div>
        <div class="cap-card-body">
          <label class="cap-field"><span>买家说了什么</span>
            <textarea class="cap-textarea" id="capRoutingMessage" style="min-height:70px" placeholder="例如：货收到了，套餐怎么充？"></textarea>
          </label>
          <details class="cap-route-facts">
            <summary>可选：你还知道订单的哪些情况？（不知道就不用选）</summary>
            <div class="cap-grid compact" style="margin-top:10px">
              <label class="cap-field"><span>买过设备吗</span><select class="cap-select" id="capFactDeviceOrder">${capTriOptions()}</select></label>
              <label class="cap-field"><span>货收到了吗</span><select class="cap-select" id="capFactReceived">${capTriOptions()}</select></label>
              <label class="cap-field"><span>买过套餐吗</span><select class="cap-select" id="capFactPackageOrder">${capTriOptions()}</select></label>
              <label class="cap-field"><span>设备在线吗</span><select class="cap-select" id="capFactDeviceOnline">${capTriOptions()}</select></label>
              <label class="cap-field"><span>网络注册了吗</span><select class="cap-select" id="capFactNetworkRegistered">${capTriOptions()}</select></label>
              <label class="cap-field"><span>欠费吗</span><select class="cap-select" id="capFactArrears">${capTriOptions()}</select></label>
              <label class="cap-field"><span>套餐状态</span><input class="cap-input" id="capFactPackageStatus" placeholder="生效 / 过期，可不填"/></label>
              <label class="cap-field"><span>卡号</span><input class="cap-input" id="capFactCardId" placeholder="有卡号再填"/></label>
            </div>
          </details>
          <div id="capRoutingResult" class="cap-routing-result"><div class="cap-empty small">写一句买家的话，点右上角按钮。</div></div>
        </div>
      </section>

      <div class="cap-section-head compact">
        <div>
          <h2>现在的分工规则</h2>
          <p>共 ${e(capState.routingRules.length)} 条。一般用系统推荐即可；只有发现分错了，再改对应那一条。</p>
        </div>
      </div>
      <div class="cap-route-list">${rows||'<div class="cap-empty">还没有规则。点右上角「恢复系统推荐规则」即可。</div>'}</div>

      <details class="cap-route-advanced-box">
        <summary>高级开关（一般不用动，保持默认即可）</summary>
        <div class="cap-card" style="margin-top:10px">
          <div class="cap-card-body cap-routing-settings">
            <label class="cap-check"><input id="capRoutingEnabled" type="checkbox" ${settings.enabled!==false?'checked':''}>启用接待分工（推荐开）</label>
            <label class="cap-check"><input id="capRoutingEnforce" type="checkbox" ${settings.shadow_enforce!==false?'checked':''}>命中规则后，优先用对应分类的话术（推荐开）</label>
            <p class="cap-help">举例：分到「物流」时，AI 优先找物流话术，而不是拿售前话术乱回。</p>
            <div style="margin-top:8px"><button class="cap-btn primary" id="capRoutingSettingsSave" type="button">保存开关</button></div>
          </div>
        </div>
      </details>`;
  }
  function capOpenRoutingEditor(row=null){
    const targets=Object.keys(capState.routingTargets||{}).length
      ? Object.keys(capState.routingTargets)
      : ['presale_script','logistics_support','activation_support','package_sales','activation_then_package','aftersales_diagnosis','package_service','product_clarify','human_handoff','hybrid_fallback'];
    const targetOpts=targets.map(value=>`<option value="${e(value)}" ${(row?.target||'hybrid_fallback')===value?'selected':''}>${e(capRouteTargetText(value))}</option>`).join('');
    const stages=row?.stages||['*'];
    const intents=row?.intents||['*'];
    capModal(row?'改这条分工规则':'加一条分工规则',`
      <input type="hidden" id="capRoutingRuleId" value="${e(row?.id||'')}">
      <div class="cap-notice safe" style="margin-bottom:12px">
        按下面 3 步填就行：<strong>谁</strong> + <strong>问什么</strong> + <strong>怎么接待</strong>。<br>
        名字只是给你自己看的；高级项可以不管。
      </div>
      <div class="cap-route-editor">
        <label class="cap-field"><span>这条规则叫什么？（自己看得懂就行）</span>
          <input class="cap-input" id="capRoutingRuleName" value="${e(row?.name||'')}" placeholder="例如：货收到了问套餐怎么买">
        </label>

        <div class="cap-route-step-block">
          <div class="cap-route-step-label"><span>1</span>如果客户是（可多选）</div>
          <div class="cap-check-grid" id="capRoutingStageBox">${capRouteStageOptions(stages)}</div>
        </div>

        <div class="cap-route-step-block">
          <div class="cap-route-step-label"><span>2</span>并且在问（可多选）</div>
          <div class="cap-check-grid" id="capRoutingIntentBox">${capRouteIntentOptions(intents)}</div>
        </div>

        <div class="cap-route-step-block">
          <div class="cap-route-step-label"><span>3</span>那就这样接待</div>
          <label class="cap-field" style="margin:0">
            <select class="cap-select" id="capRoutingRuleTarget">${targetOpts}</select>
          </label>
        </div>

        <label class="cap-field"><span>备注（可选，写给自己看）</span>
          <textarea class="cap-textarea" id="capRoutingRuleDescription" placeholder="例如：未签收时也能先讲清套餐怎么充">${e(row?.description||'')}</textarea>
        </label>
        <label class="cap-check"><input id="capRoutingRuleEnabled" type="checkbox" ${row?.enabled===false?'':'checked'}>保存后立刻使用这条规则</label>

        <details class="cap-advanced">
          <summary>高级选项（多数情况不用改）</summary>
          <div class="cap-grid" style="margin-top:10px">
            <label class="cap-field"><span>匹配先后（数字越大越先比）</span>
              <input class="cap-input" id="capRoutingRulePriority" type="number" value="${e(row?.priority??50)}">
            </label>
            <label class="cap-field wide"><span>优先使用的话术分类（可选，每行一个内部名）</span>
              <textarea class="cap-textarea" id="capRoutingRuleCategories" placeholder="例如：&#10;presale&#10;logistics">${e(listText(row?.script_categories||[]))}</textarea>
            </label>
            <label class="cap-field wide"><span>额外条件 JSON（可不填）</span>
              <textarea class="cap-textarea" id="capRoutingRuleConditions" style="min-height:90px">${e(jsonText(row?.conditions||[]))}</textarea>
            </label>
          </div>
        </details>
      </div>
    `,`<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capRoutingRuleSave" type="button">保存</button>`);
  }
  async function capSaveRoutingRule(button){
    const id=$('capRoutingRuleId').value.trim();
    const stages=[...document.querySelectorAll('input[name="capRouteStage"]:checked')].map(el=>el.value);
    const intents=[...document.querySelectorAll('input[name="capRouteIntent"]:checked')].map(el=>el.value);
    const payload={
      name:$('capRoutingRuleName').value.trim(),
      priority:Number($('capRoutingRulePriority')?.value||50),
      target:$('capRoutingRuleTarget').value,
      stages: stages.length?stages:['*'],
      intents: intents.length?intents:['*'],
      script_categories:splitList($('capRoutingRuleCategories')?.value||''),
      conditions:parseJSON($('capRoutingRuleConditions')?.value||'[]',[],'额外条件'),
      description:$('capRoutingRuleDescription').value.trim(),
      enabled:$('capRoutingRuleEnabled').checked
    };
    if(!payload.name)return toast('请给规则起个名字，方便以后找到');
    capButtonBusy(button,true,'保存中…');
    try{
      await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/routing/rules${id?`/${encodeURIComponent(id)}`:''}`,{method:id?'PUT':'POST',body:JSON.stringify(payload)});
      capCloseModal();toast('规则已保存');await capLoadRouting();
    }catch(error){toast('保存失败：'+error.message);}
    finally{capButtonBusy(button,false);}
  }
  function capRoutingFacts(){const facts={};[['has_device_order','capFactDeviceOrder'],['received','capFactReceived'],['has_package_order','capFactPackageOrder'],['device_online','capFactDeviceOnline'],['network_registered','capFactNetworkRegistered'],['arrears','capFactArrears']].forEach(([key,id])=>{const el=$(id);if(!el)return;const value=el.value;if(value!=='')facts[key]=value==='true';});const ps=$('capFactPackageStatus');const cid=$('capFactCardId');if(ps&&ps.value.trim())facts.package_status=ps.value.trim();if(cid&&cid.value.trim())facts.card_id=cid.value.trim();return facts;}
  function capRenderRoutingDecision(decision){
    const lifecycle=decision.lifecycle||{},intent=decision.intent||{},route=decision.route||{};
    const missing=decision.data_quality?.missing_core_fields||[],tools=decision.required_tool_calls||[],fh=decision.force_handoff||{};
    const stageText=lifecycle.label||capRouteStageText(lifecycle.stage)||lifecycle.stage||'还不清楚';
    const intentText=intent.label||capRouteIntentText(intent.intent)||intent.intent||'还不清楚';
    const targetText=route.target_label||capRouteTargetText(route.target)||'默认接待';
    const plain=route.matched
      ? `系统认为：客户现在是「${stageText}」，这句话像在问「${intentText}」。对上了规则「${route.rule_name}」，所以会：${targetText}。`
      : `系统认为：客户现在是「${stageText}」，这句话像在问「${intentText}」。没有对上专用规则，走默认兜底：${targetText}。`;
    const fhHtml=fh.matched?`<div class="cap-meta" style="margin-top:8px"><span class="cap-tag red">触发强制转人工</span><span class="cap-tag">${e(fh.reason||fh.layer||'')}</span><span class="cap-tag ${fh.apply_handoff?'red':'orange'}">${fh.apply_handoff?'会真的转人工':'目前只观察，不真转'}</span></div>`:'';
    return `<div class="cap-route-decision">
      <div class="cap-route-summary">${e(plain)}</div>
      <div class="cap-route-decision-flow">
        <div class="cap-route-flow-item"><span>客户阶段</span><strong>${e(stageText)}</strong></div>
        <div class="cap-route-flow-arrow">→</div>
        <div class="cap-route-flow-item"><span>在问什么</span><strong>${e(intentText)}</strong></div>
        <div class="cap-route-flow-arrow">→</div>
        <div class="cap-route-flow-item result"><span>怎么接待</span><strong>${e(targetText)}</strong></div>
      </div>
      <p class="cap-route-safe-note">这是测试结果，<strong>不会发给买家</strong>。命中规则：${e(route.rule_name||'默认兜底')}</p>
      ${fhHtml}
      ${(route.script_categories||[]).length||tools.length||missing.length?`<div class="cap-meta" style="margin-top:8px">${(route.script_categories||[]).map(x=>`<span class="cap-tag blue">可用话术：${e(x)}</span>`).join('')}${tools.map(x=>`<span class="cap-tag orange">可能查工具：${e(x)}</span>`).join('')}${missing.map(x=>`<span class="cap-tag red">还缺：${e(x)}</span>`).join('')}</div>`:''}
      <details><summary>技术人员可看：完整 JSON</summary><pre>${e(jsonText(decision))}</pre></details>
    </div>`;
  }

  async function capLoadForceHandoff(){
    const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/force-handoff`);
    const p=data.policy||{};
    capState.forceHandoff=p;
    const l1=p.l1_keywords||{}, l2=p.l2_intents||{}, l3=p.l3_score||{}, w=l3.weights||{};
    const modeTag=(!p.enabled)?'已关闭':(p.observe_only?'仅记录命中':(p.enforce?'执行转接':'尚未开启转接'));
    const modeClass=(!p.enabled)?'red':(p.observe_only?'orange':(p.enforce?'red':'orange'));
    $('capContent').innerHTML=`
      <div class="cap-section-head">
        <div><h2>强制转人工</h2><p>按关键词、业务意图和风险分数分层判断。默认只记录命中，开启执行后才转客服。</p></div>
        <div class="cap-actions">
          <button class="cap-btn" id="capFhReset" type="button">恢复默认</button>
          <button class="cap-btn primary" id="capFhSave" type="button">保存策略</button>
        </div>
      </div>
      <div class="cap-notice ${p.enforce&&!p.observe_only?'warn':'safe'}">
        <strong>当前模式：</strong><span class="cap-tag ${modeClass}">${e(modeTag)}</span>
        ｜ 记录模式：${p.observe_only?'开启':'关闭'} ｜ 执行转接：${p.enforce?'开启':'关闭'} ｜ 版本 ${e(p.version||1)}
        <br>建议先使用「仅记录命中」检查规则，再开启「执行转接」。
      </div>
      <div class="cap-routing-grid">
        <section class="cap-card">
          <div class="cap-card-head"><div><strong>总开关</strong><small>店铺级</small></div></div>
          <div class="cap-card-body cap-routing-settings">
            <label class="cap-check"><input id="capFhEnabled" type="checkbox" ${p.enabled!==false?'checked':''}>启用强制转人工策略</label>
            <label class="cap-check"><input id="capFhObserve" type="checkbox" ${p.observe_only!==false?'checked':''}>仅记录命中（不转客服）</label>
            <label class="cap-check"><input id="capFhEnforce" type="checkbox" ${p.enforce?'checked':''}>执行转接（进入客服队列，并暂停自动回复）</label>
            <label class="cap-field" style="margin-top:8px"><span>转客服时回复买家的话术</span>
              <textarea class="cap-textarea" id="capFhReply" style="min-height:72px">${e(p.handoff_reply||'')}</textarea>
            </label>
          </div>
        </section>
        <section class="cap-card">
          <div class="cap-card-head"><div><strong>策略测试</strong><small>不改真实会话</small></div>
            <button class="cap-btn primary" id="capFhTest" type="button">测试这句话</button>
          </div>
          <div class="cap-card-body">
            <label class="cap-field"><span>买家消息</span>
              <textarea class="cap-textarea" id="capFhMessage" style="min-height:72px" placeholder="例如：我要转人工 / 我要投诉退款"></textarea>
            </label>
            <div class="cap-grid compact" style="margin-top:8px">
              <label class="cap-field"><span>模拟买家诉求（可选）</span>
                <select class="cap-select" id="capFhIntent">${capFhTestIntentOptions()}</select>
              </label>
              <label class="cap-field"><span>意图置信度</span>
                <input class="cap-input" id="capFhIntentConf" type="number" min="0" max="1" step="0.05" value="0.9"/>
              </label>
              <label class="cap-field"><span>AI 连续未知次数</span>
                <input class="cap-input" id="capFhUnknownStreak" type="number" min="0" value="0"/>
              </label>
            </div>
            <div id="capFhResult" class="cap-routing-result"><div class="cap-empty small">输入消息后点「测试这句话」。</div></div>
          </div>
        </section>
      </div>
      <div class="cap-section-head compact"><div><h2>L1 · 关键词规则</h2><p>买家消息命中关键词时触发。一行一个词或表达式。</p></div></div>
      <div class="cap-card"><div class="cap-card-body">
        <label class="cap-check"><input id="capFhL1Enabled" type="checkbox" ${l1.enabled!==false?'checked':''}>启用 L1</label>
        <div class="cap-grid" style="margin-top:8px">
          <label class="cap-field"><span>关键词（每行一个）</span>
            <textarea class="cap-textarea" id="capFhL1Keywords" style="min-height:160px">${e(listText(l1.keywords||[]))}</textarea>
          </label>
          <label class="cap-field"><span>正则（每行一个）</span>
            <textarea class="cap-textarea" id="capFhL1Patterns" style="min-height:160px">${e(listText(l1.patterns||[]))}</textarea>
          </label>
        </div>
      </div></div>
      <div class="cap-section-head compact"><div><h2>L2 · 业务意图规则</h2><p>勾选哪些买家诉求需要转客服，例如“投诉 / 高风险”或“退款 / 退货”。</p></div></div>
      <div class="cap-card"><div class="cap-card-body">
        <label class="cap-check"><input id="capFhL2Enabled" type="checkbox" ${l2.enabled!==false?'checked':''}>启用业务意图规则</label>
        <div class="cap-grid" style="margin-top:8px">
          <div class="cap-field"><span>命中后转客服的业务意图</span>
            <div class="cap-check-grid">${capFhIntentOptions(l2.intents||[])}</div>
          </div>
          <label class="cap-field"><span>最低置信度</span>
            <input class="cap-input" id="capFhL2MinConf" type="number" min="0" max="1" step="0.05" value="${e(l2.min_confidence??0)}"/>
          </label>
        </div>
      </div></div>
      <div class="cap-section-head compact"><div><h2>L3 · 风险分数规则</h2><p>综合多个风险信号，达到阈值才转客服，适合作为兜底。</p></div></div>
      <div class="cap-card"><div class="cap-card-body">
        <label class="cap-check"><input id="capFhL3Enabled" type="checkbox" ${l3.enabled!==false?'checked':''}>启用 L3</label>
        <div class="cap-grid compact" style="margin-top:8px">
          <label class="cap-field"><span>分数阈值</span><input class="cap-input" id="capFhL3Threshold" type="number" min="0" step="0.05" value="${e(l3.score_threshold??1)}"/></label>
          <label class="cap-field"><span>低置信阈值</span><input class="cap-input" id="capFhL3LowConf" type="number" min="0" max="1" step="0.05" value="${e(l3.low_confidence_threshold??0.4)}"/></label>
          <label class="cap-field"><span>连续未知次数</span><input class="cap-input" id="capFhL3UnknownNeed" type="number" min="1" value="${e(l3.ai_unknown_streak??2)}"/></label>
          <label class="cap-field"><span>重复要人工次数</span><input class="cap-input" id="capFhL3HumanNeed" type="number" min="1" value="${e(l3.buyer_repeat_human_ask??1)}"/></label>
          <label class="cap-field wide"><span>软关键词（每行一个，只加分不直接 L1）</span>
            <textarea class="cap-textarea" id="capFhL3Soft" style="min-height:80px">${e(listText(l3.soft_keywords||[]))}</textarea>
          </label>
          <label class="cap-field"><span>软关键词权重</span><input class="cap-input" id="capFhWSoft" type="number" step="0.05" value="${e(w.soft_keyword??0.45)}"/></label>
          <label class="cap-field"><span>低置信度权重</span><input class="cap-input" id="capFhWLow" type="number" step="0.05" value="${e(w.low_confidence??0.55)}"/></label>
          <label class="cap-field"><span>连续未识别权重</span><input class="cap-input" id="capFhWUnk" type="number" step="0.05" value="${e(w.unknown_streak??0.5)}"/></label>
          <label class="cap-field"><span>重复要求转客服权重</span><input class="cap-input" id="capFhWRep" type="number" step="0.05" value="${e(w.repeat_human_ask??0.6)}"/></label>
        </div>
      </div></div>`;
  }

  function capFhCollectPayload(){
    return {
      enabled: $('capFhEnabled').checked,
      observe_only: $('capFhObserve').checked,
      enforce: $('capFhEnforce').checked,
      handoff_reply: $('capFhReply').value.trim(),
      l1_keywords: {
        enabled: $('capFhL1Enabled').checked,
        keywords: splitList($('capFhL1Keywords').value),
        patterns: splitList($('capFhL1Patterns').value),
      },
      l2_intents: {
        enabled: $('capFhL2Enabled').checked,
        intents: [...document.querySelectorAll('input[name="capFhL2Intent"]:checked')].map(el=>el.value),
        min_confidence: Number($('capFhL2MinConf').value||0),
      },
      l3_score: {
        enabled: $('capFhL3Enabled').checked,
        score_threshold: Number($('capFhL3Threshold').value||1),
        low_confidence_threshold: Number($('capFhL3LowConf').value||0.4),
        ai_unknown_streak: Number($('capFhL3UnknownNeed').value||2),
        buyer_repeat_human_ask: Number($('capFhL3HumanNeed').value||1),
        soft_keywords: splitList($('capFhL3Soft').value),
        weights: {
          soft_keyword: Number($('capFhWSoft').value||0.45),
          low_confidence: Number($('capFhWLow').value||0.55),
          unknown_streak: Number($('capFhWUnk').value||0.5),
          repeat_human_ask: Number($('capFhWRep').value||0.6),
        },
      },
    };
  }

  function capRenderForceHandoffResult(r){
    if(!r)return '<div class="cap-empty small">无结果</div>';
    const cls=r.matched?(r.apply_handoff?'red':'orange'):'green';
    const layerText=({L1:'关键词规则',L2:'业务意图规则',L3:'风险分数规则'})[r.layer]||'转接规则';
    const title=r.matched?`${layerText} · ${r.reason||'命中'}`:'未命中强制转人工';
    return `<div class="cap-route-decision"><div class="cap-route-decision-grid">
      <div><span>结果</span><strong class="${r.matched?'':'safe-text'}">${e(title)}</strong><small>${e(layerText)}</small></div>
      <div><span>会转客服吗？</span><strong>${r.apply_handoff?'是':'否'}</strong><small>${r.observe_only?'仅记录命中':'执行转接模式'}</small></div>
      <div><span>风险分数</span><strong>${e(r.score??0)}</strong><small>风险分数规则使用</small></div>
      <div><span>处理方式</span><strong class="cap-tag ${cls}">${r.apply_handoff?'转入客服队列':(r.matched?'只记录，不转接':'正常接待')}</strong></div>
    </div><details><summary>技术人员可看：完整结果</summary><pre>${e(jsonText(r))}</pre></details></div>`;
  }

  function capWfStatusText(status){
    return({published:'使用中',draft:'草稿（未启用）',disabled:'已关掉'})[status]||status||'未知';
  }
  function capWfSourceTag(name){
    const n=String(name||'');
    if(n.startsWith('探域学习-'))return {label:'探域学习',cls:'blue'};
    if(n.startsWith('移动WiFi-'))return {label:'系统推荐',cls:'orange'};
    return {label:'本店自建',cls:''};
  }
  function capWfActionText(action){
    return({
      tool:'查系统状态',branch:'按状态分支处理',guide:'指导用户操作',verify:'确认是否恢复',
      knowledge:'查话术/资料',reply:'先回复安抚',risk_check:'安全风险检查',collect:'收集证据资料',
      handoff:'转给人工',ask:'追问澄清',clarify:'继续问清楚'
    })[action]||'其他处理';
  }
  function capWfLines(value){
    if(Array.isArray(value))return value.map(v=>typeof v==='string'?v:(v?.name||v?.text||JSON.stringify(v))).filter(Boolean).join('\n');
    return String(value||'');
  }
  function capWfStepsText(steps){
    return(steps||[]).map((step,i)=>{
      if(typeof step==='string')return `${i+1}. ${step}`;
      const name=step.name||step.id||`步骤${i+1}`;
      const act=capWfActionText(step.action);
      const tool=step.tool?`（${step.tool}）`:'';
      return `${i+1}. ${name} · ${act}${tool}`;
    }).join('\n');
  }
  function capWfStepsEditorText(steps){
    return(steps||[]).map(step=>{
      if(typeof step==='string')return step;
      const parts=[step.name||step.id||''];
      if(step.action)parts.push(step.action);
      if(step.tool)parts.push(step.tool);
      return parts.join(' | ');
    }).join('\n');
  }
  function capWfParseSteps(text){
    return String(text||'').split(/\n+/).map(line=>line.trim()).filter(Boolean).map((line,idx)=>{
      const cleaned=line.replace(/^\d+[\.、\)]\s*/,'');
      const parts=cleaned.split(/\s*\|\s*/).map(x=>x.trim()).filter(Boolean);
      const name=parts[0]||`步骤${idx+1}`;
      const action=parts[1]||'guide';
      const tool=parts[2]||'';
      const out={id:`step_${idx+1}`,name,action};
      if(tool)out.tool=tool;
      return out;
    });
  }
  const CAP_WF_ISSUE_TYPES = [
    '无法联网或信号异常','充值后仍无法使用','网速慢或网络体验异常','实名认证或激活',
    '套餐价格与续费','设备供电或电池异常','退款退货与投诉','物流配送问题',
    '少件、漏发或发错','商品质量问题','安装或使用问题','其他售后咨询'
  ];
  const CAP_WF_ACTIONS = [
    {value:'tool',label:'查询店铺系统状态'},
    {value:'branch',label:'根据状态分别处理'},
    {value:'ask',label:'向买家追问情况'},
    {value:'guide',label:'指导买家操作'},
    {value:'knowledge',label:'查询已审核资料或话术'},
    {value:'reply',label:'先回复并安抚'},
    {value:'risk_check',label:'检查安全风险'},
    {value:'collect',label:'收集图片或资料'},
    {value:'verify',label:'确认问题是否解决'},
    {value:'handoff',label:'转给人工客服'},
    {value:'clarify',label:'继续问清楚'}
  ];
  function capWfIssueOptions(current){
    const value=String(current||'').trim(),values=[...CAP_WF_ISSUE_TYPES];
    if(value&&!values.includes(value))values.unshift(value);
    return `<option value="">请选择问题类型</option>${values.map(item=>`<option value="${e(item)}" ${item===value?'selected':''}>${e(item)}</option>`).join('')}<option value="__custom__">其他问题（自行填写）</option>`;
  }
  function capWfPriorityOptions(current){
    const value=Math.max(0,Math.min(100,Number(current??50))),standard=[90,70,50,30];
    const label=n=>n>=90?'最优先':n>=70?'优先':n>=50?'普通':'靠后';
    const values=standard.includes(value)?standard:[value,...standard];
    return values.map(n=>`<option value="${e(n)}" ${n===value?'selected':''}>${e(label(n))}${standard.includes(n)?'':`（保持当前 ${e(n)}）`}</option>`).join('');
  }
  function capWfActionOptions(current){
    const value=String(current||'guide'),known=CAP_WF_ACTIONS.some(item=>item.value===value);
    const options=known?CAP_WF_ACTIONS:[{value,label:'原有处理方式'},...CAP_WF_ACTIONS];
    return options.map(item=>`<option value="${e(item.value)}" ${item.value===value?'selected':''}>${e(item.label)}</option>`).join('');
  }
  function capWfListValues(value){
    if(!Array.isArray(value))return [];
    return value.map(item=>typeof item==='string'?item:(item?.text||item?.name||'')).map(item=>String(item||'').trim()).filter(Boolean);
  }
  function capWfListRow(kind,value=''){
    const placeholder=kind==='entry'?'例如：连不上网':'例如：处理两次仍未解决';
    const label=kind==='entry'?'删除这句买家说法':'删除这条转人工条件';
    return `<div class="cap-wf-value-row" data-wf-list-row="${e(kind)}"><input class="cap-input" data-wf-list-value value="${e(value)}" placeholder="${e(placeholder)}"><button class="cap-wf-icon-btn danger" data-wf-list-remove type="button" title="${e(label)}" aria-label="${e(label)}">&times;</button></div>`;
  }
  function capWfListRows(kind,values){
    const rows=capWfListValues(values);return (rows.length?rows:['']).map(value=>capWfListRow(kind,value)).join('');
  }
  function capWfStepRow(step={},originalIndex=''){
    const item=step&&typeof step==='object'?step:{name:String(step||''),action:'guide'};
    return `<div class="cap-wf-step-row" data-wf-step-row data-original-index="${e(originalIndex)}">
      <div class="cap-wf-step-order" data-wf-step-order>1</div>
      <label class="cap-field"><span>这一步要做什么 *</span><input class="cap-input" data-wf-step-name value="${e(item.name||item.id||'')}" required placeholder="例如：确认订单和商品状态"></label>
      <label class="cap-field"><span>处理方式</span><select class="cap-select" data-wf-step-action>${capWfActionOptions(item.action)}</select></label>
      <div class="cap-wf-step-actions"><button class="cap-wf-icon-btn" data-wf-step-move="-1" type="button" title="上移" aria-label="上移">&uarr;</button><button class="cap-wf-icon-btn" data-wf-step-move="1" type="button" title="下移" aria-label="下移">&darr;</button><button class="cap-wf-icon-btn danger" data-wf-step-remove type="button" title="删除步骤" aria-label="删除步骤">&times;</button></div>
    </div>`;
  }
  function capWfRefreshStepOrder(){
    const rows=[...document.querySelectorAll('#capWfStepRows [data-wf-step-row]')];
    rows.forEach((row,index)=>{const order=row.querySelector('[data-wf-step-order]');if(order)order.textContent=String(index+1);row.querySelector('[data-wf-step-move="-1"]')?.toggleAttribute('disabled',index===0);row.querySelector('[data-wf-step-move="1"]')?.toggleAttribute('disabled',index===rows.length-1);});
  }
  function capWfCollectSteps(){
    const original=capState.workflowEditor?.steps||[],usedIds=new Set();
    return [...document.querySelectorAll('#capWfStepRows [data-wf-step-row]')].map((row,index)=>{
      const originalKey=String(row.dataset.originalIndex??''),originalIndex=originalKey===''?-1:Number(originalKey),base=Number.isInteger(originalIndex)&&originalIndex>=0&&original[originalIndex]&&typeof original[originalIndex]==='object'?{...original[originalIndex]}:{};
      const action=String(row.querySelector('[data-wf-step-action]')?.value||'guide'),name=String(row.querySelector('[data-wf-step-name]')?.value||'').trim();
      let id=String(base.id||`step_${index+1}`);while(usedIds.has(id))id=`${id}_${index+1}`;usedIds.add(id);
      const previousAction=String(base.action||'');base.id=id;base.name=name;base.action=action;delete base.status;
      if(action==='tool')base.tool=String(base.tool||'get_card_status');else if(previousAction!==action)delete base.tool;
      return base;
    });
  }
  function capWfReadList(kind){
    return [...document.querySelectorAll(`[data-wf-list-row="${kind}"] [data-wf-list-value]`)].map(input=>input.value.trim()).filter(Boolean);
  }
  function capSyncWorkflowIssueField(focus=false){
    const select=$('capWfIssueType'),field=$('capWfCustomIssueField'),input=$('capWfCustomIssue');if(!select||!field||!input)return;
    const custom=select.value==='__custom__';field.hidden=!custom;input.required=custom;if(custom&&focus)input.focus();
  }
  function capRenderDiagnoseResult(d){
    if(!d||typeof d!=='object')return`<div class="cap-empty small">没有诊断结果</div>`;
    const wf=d.workflow||{},decision=d.decision||{},gate=d.service_gate||{},emotion=d.emotion||{};
    const issue=d.decision_issue_type||d.issue_type||d.reported_issue_type||'未知问题';
    const steps=Array.isArray(wf.steps)?wf.steps:[];
    const findings=Array.isArray(d.findings)?d.findings:[];
    const reply=decision.reply||'';
    const handoff=!!decision.handoff;
    const plain=handoff
      ?`系统判断：这是「${issue}」。建议走规则「${wf.name||'通用售后'}」，并准备转人工：${decision.handoff_reason||'需要人工权限'}。`
      :`系统判断：这是「${issue}」。建议按规则「${wf.name||'通用售后'}」处理。`;
    return`<div class="cap-route-decision">
      <div class="cap-route-summary">${e(plain)}</div>
      <div class="cap-route-decision-flow">
        <div class="cap-route-flow-item"><span>识别到的问题</span><strong>${e(issue)}</strong></div>
        <div class="cap-route-flow-arrow">→</div>
        <div class="cap-route-flow-item"><span>命中规则</span><strong>${e(wf.name||'通用售后')}</strong></div>
        <div class="cap-route-flow-arrow">→</div>
        <div class="cap-route-flow-item result"><span>下一步</span><strong>${e(handoff?'转人工':(decision.next_action||'继续指导'))}</strong></div>
      </div>
      ${reply?`<div class="cap-wf-reply"><span>AI 可能这样回（仅预览，不发送）</span><p>${e(reply)}</p></div>`:''}
      ${steps.length?`<div class="cap-wf-steps-preview"><span>规则步骤</span><ol>${steps.map(s=>`<li><strong>${e(s.name||s.id||'')}</strong><small>${e(capWfActionText(s.action))}</small></li>`).join('')}</ol></div>`:''}
      ${findings.length?`<div class="cap-meta" style="margin-top:8px">${findings.map(f=>`<span class="cap-tag ${f.level==='good'?'green':f.level==='warn'?'orange':'red'}">${e(f.title||f.code||'')}</span>`).join('')}</div>`:''}
      <p class="cap-route-safe-note">情绪：${e(emotion.label||'正常')} · 门控：${e(gate.code||'normal')} · <strong>不会发给买家</strong></p>
      <details><summary>技术人员可看：完整 JSON</summary><pre>${e(jsonText(d))}</pre></details>
    </div>`;
  }
  function capDownloadJson(filename, obj){
    const blob=new Blob([JSON.stringify(obj,null,2)],{type:'application/json;charset=utf-8'});
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url;a.download=filename;a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1500);
  }
  async function capExportWorkflows(button, selectedOnly=false){
    let ids=null;
    if(selectedOnly){
      ids=[...document.querySelectorAll('input[name="capWfPick"]:checked')].map(el=>el.value);
      if(!ids.length)return toast('请先勾选要导出的规则');
    }
    capButtonBusy(button,true,'导出中…');
    try{
      const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/workflows/export`,{
        method:'POST',body:JSON.stringify({rule_ids:ids})
      });
      const pack=data.pack||data;
      const shop=(pack.source_shop_name||capState.shopId||'shop').replace(/[\\/:*?"<>|]+/g,'_');
      capDownloadJson(`${shop}-售后规则.json`,pack);
      toast(`已导出 ${pack.rule_count??(pack.rules||[]).length} 条规则`);
    }catch(error){toast('导出失败：'+error.message);}
    finally{capButtonBusy(button,false);}
  }
  function capOpenWorkflowImportDialog(pack, {fromSystem=false}={}){
    const rules=Array.isArray(pack?.rules)?pack.rules:(Array.isArray(pack)?pack:[]);
    if(!rules.length)return toast('导入包里没有规则');
    capState.workflowImportPack=pack;
    const existing=new Set((capState.workflows||[]).map(x=>String(x.name||'')));
    const rows=rules.map((r,i)=>{
      const name=r.name||`规则${i+1}`;
      const clash=existing.has(name);
      return`<label class="cap-check cap-wf-import-row">
        <input type="checkbox" name="capWfImportPick" value="${e(name)}" checked>
        <span><strong>${e(name)}</strong>
          <small>${e(r.issue_type||'未分类')}${clash?' · 本店已有同名':''}</small>
        </span>
      </label>`;
    }).join('');
    capModal(fromSystem?'选用系统推荐规则':'导入售后规则',`
      <div class="cap-notice safe" style="margin-bottom:10px">
        来源：<strong>${e(pack.source_shop_name||pack.source_shop_id||(fromSystem?'系统推荐':'规则包'))}</strong>
        · 共 ${e(rules.length)} 条。勾选本店要用的，再点导入。
      </div>
      <div class="cap-grid" style="margin-bottom:10px">
        <label class="cap-field"><span>同名规则怎么处理</span>
          <select class="cap-select" id="capWfImportMode">
            <option value="skip" selected>跳过（保留本店已有）</option>
            <option value="replace">覆盖本店同名规则</option>
            <option value="always_new">始终新建一份</option>
          </select>
        </label>
        <label class="cap-check" style="align-self:end"><input type="checkbox" id="capWfImportDraft">导入为草稿（先不启用）</label>
      </div>
      <div class="cap-actions" style="margin-bottom:8px">
        <button class="cap-btn" type="button" id="capWfImportAll">全选</button>
        <button class="cap-btn" type="button" id="capWfImportNone">全不选</button>
      </div>
      <div class="cap-wf-import-list">${rows}</div>
    `,`<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capWorkflowImportConfirm" type="button">导入到本店</button>`);
  }
  async function capImportWorkflowsFromFile(button){
    const input=document.createElement('input');
    input.type='file';input.accept='.json,application/json';
    input.onchange=async()=>{
      const file=input.files?.[0];if(!file)return;
      capButtonBusy(button,true,'读取中…');
      try{
        const text=await file.text();
        const pack=JSON.parse(text);
        capOpenWorkflowImportDialog(pack.pack||pack,{fromSystem:false});
      }catch(error){toast('读取失败：请选择合法的规则 JSON 文件');}
      finally{capButtonBusy(button,false);}
    };
    input.click();
  }
  async function capConfirmWorkflowImport(button){
    const pack=capState.workflowImportPack;
    if(!pack)return toast('导入包无效，请重新选择文件');
    const selected=[...document.querySelectorAll('input[name="capWfImportPick"]:checked')].map(el=>el.value);
    if(!selected.length)return toast('请至少勾选一条规则');
    const mode=$('capWfImportMode')?.value||'skip';
    const as_draft=!!$('capWfImportDraft')?.checked;
    capButtonBusy(button,true,'导入中…');
    try{
      const result=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/workflows/import`,{
        method:'POST',
        body:JSON.stringify({pack,selected_names:selected,mode,as_draft})
      });
      capState.workflowImportPack=null;
      capCloseModal();
      toast(`导入完成：新增 ${result.created_count||0}，覆盖 ${result.replaced_count||0}，跳过 ${result.skipped_count||0}`);
      await capLoadWorkflows();
    }catch(error){toast('导入失败：'+error.message);}
    finally{capButtonBusy(button,false);}
  }
  async function capLoadWorkflows(){
    const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/workflows`);
    capState.workflows=data.workflows||[];
    const scope=data.scope||{};
    const shopName=scope.shop_name||capState.shopId;
    const published=capState.workflows.filter(x=>x.status==='published').length;
    const sorted=[...capState.workflows].sort((a,b)=>{
      const ap=Number(a.priority||0),bp=Number(b.priority||0);
      if(bp!==ap)return bp-ap;
      const as=String(a.name||'').startsWith('探域学习-')?0:1;
      const bs=String(b.name||'').startsWith('探域学习-')?0:1;
      return as-bs;
    });
    const rows=sorted.map((row,idx)=>{
      const src=capWfSourceTag(row.name);
      const triggers=(row.entry_conditions||[]).slice(0,6).map(x=>`<span class="cap-tag">${e(x)}</span>`).join('')
        +((row.entry_conditions||[]).length>6?`<span class="cap-tag">+${(row.entry_conditions||[]).length-6}</span>`:'');
      const steps=(row.steps||[]).map((s,i)=>`<li><em>${e(i+1)}</em><div><strong>${e(s.name||s.id||('步骤'+(i+1)))}</strong><small>${e(capWfActionText(s.action))}</small></div></li>`).join('');
      const esc=(row.escalation_rules||[]).slice(0,3);
      return`<article class="cap-wf-card ${row.status==='published'?'':'is-off'}">
        <div class="cap-wf-card-top">
          <label class="cap-wf-pick" title="勾选后可选择性导出"><input type="checkbox" name="capWfPick" value="${e(row.id)}"></label>
          <div class="cap-route-ord">规则 ${e(idx+1)}</div>
          <div class="cap-route-card-title">
            <strong>${e(row.name||'未命名')}</strong>
            <span class="cap-tag ${src.cls}">${e(src.label)}</span>
            <span class="cap-tag ${row.status==='published'?'green':row.status==='disabled'?'red':'orange'}">${e(capWfStatusText(row.status))}</span>
          </div>
          <div class="cap-row-actions">
            <button class="cap-btn" data-workflow-edit="${e(row.id)}" type="button">改</button>
            <button class="cap-btn danger" data-workflow-delete="${e(row.id)}" type="button">删</button>
          </div>
        </div>
        <div class="cap-wf-issue">针对问题：<strong>${e(row.issue_type||'未分类')}</strong></div>
        ${row.description?`<p class="cap-route-why">${e(row.description)}</p>`:''}
        <div class="cap-wf-block">
          <div class="cap-wf-block-label">买家提到这些词时会命中</div>
          <div class="cap-meta">${triggers||'<span class="cap-tag">未写触发词</span>'}</div>
        </div>
        <div class="cap-wf-block">
          <div class="cap-wf-block-label">处理步骤（按顺序）</div>
          ${steps?`<ol class="cap-wf-steps">${steps}</ol>`:'<p class="cap-help">还没写步骤</p>'}
        </div>
        <div class="cap-wf-footer">
          <div><span>怎样算搞定</span><strong>${e(row.success_criteria||'未写')}</strong></div>
          <div><span>什么时候转人工</span><strong>${e(esc.length?esc.join('；'):'未写特殊规则')}</strong></div>
        </div>
      </article>`;
    }).join('');
    $('capContent').innerHTML=`
      <div class="cap-section-head">
        <div>
          <h2>售后规则</h2>
          <p>当前店铺：<strong>${e(shopName)}</strong>。规则按店隔离；可导出给店群，其他店选择性导入。</p>
        </div>
        <div class="cap-actions">
          <button class="cap-btn" id="capWorkflowExportAll" type="button">导出全部</button>
          <button class="cap-btn" id="capWorkflowExportSelected" type="button">导出已勾选</button>
          <button class="cap-btn" id="capWorkflowImport" type="button">导入规则</button>
          <button class="cap-btn" id="capWorkflowSystemPack" type="button">选用系统推荐</button>
          <button class="cap-btn primary" id="capWorkflowAdd" type="button">新增规则</button>
        </div>
      </div>

      <div class="cap-notice safe">
        <strong>店群怎么用：</strong>
        在样板店（如 VHE）整理好规则 → <b>导出</b> JSON → 其他店打开本页 → <b>导入</b> → 勾选要用的规则。<br>
        系统推荐包是可选模板，任何店都可以选用，不会自动强塞。
      </div>

      <div class="cap-route-story">
        <div class="cap-route-story-title">这页是干什么的</div>
        <ol class="cap-route-steps">
          <li><span>1</span><div><strong>写清规则</strong><small>什么问题、先做哪几步、何时转人工</small></div></li>
          <li><span>2</span><div><strong>导出 / 导入</strong><small>店群之间选择性复用，互不影响</small></div></li>
          <li><span>3</span><div><strong>先试一试</strong><small>用买家原话测会命中哪条（不发送）</small></div></li>
        </ol>
        <p class="cap-route-story-tip">${e(scope.workflow_scope||'')} · <b>只影响模拟，不会发给买家。</b></p>
      </div>

      <section class="cap-card cap-route-try">
        <div class="cap-card-head">
          <div>
            <strong>先试一试</strong>
            <small>写买家现象，看会命中哪条规则</small>
          </div>
          <button class="cap-btn primary" id="capDiagnoseRun" type="button">看看怎么排查</button>
        </div>
        <div class="cap-card-body">
          <div class="cap-grid compact">
            <label class="cap-field wide"><span>买家说了什么</span>
              <input class="cap-input" id="capDiagnoseMessage" placeholder="例如：设备亮红灯，上不去网"/>
            </label>
            <label class="cap-field"><span>假设卡状态（可选）</span>
              <select class="cap-select" id="capDiagnoseScenario">
                <option value="normal">卡正常</option>
                <option value="arrears">欠费了</option>
                <option value="inactive">还没激活</option>
                <option value="expired">套餐过期了</option>
              </select>
            </label>
          </div>
          <div id="capDiagnoseResult" class="cap-routing-result"><div class="cap-empty small">写一句售后问题，点「看看怎么排查」。</div></div>
        </div>
      </section>

      <div class="cap-section-head compact">
        <div>
          <h2>${e(shopName)} 的规则（${e(capState.workflows.length)} 条，${e(published)} 条使用中）</h2>
          <p>左侧勾选可「导出已勾选」。同名问题多条时，优先用优先级更高的那条。</p>
        </div>
      </div>
      <div class="cap-wf-list">${rows||'<div class="cap-empty">还没有规则。可「新增规则」、「导入规则」或「选用系统推荐」。</div>'}</div>`;
  }
  function capOpenWorkflowEditor(row=null){
    const item=row||{name:'',issue_type:'',description:'',status:'draft',version:1,priority:50,entry_conditions:[],required_fields:[],steps:[],success_criteria:'',escalation_rules:[]};
    const steps=Array.isArray(item.steps)?item.steps:[];
    capState.workflowEditor={item,steps,requiredFields:Array.isArray(item.required_fields)?[...item.required_fields]:[]};
    capModal(row?'改这条售后规则':'新增售后规则',`
      <form id="capWorkflowForm" data-id="${e(item.id||'')}">
        <div class="cap-wf-editor">
          <section class="cap-wf-editor-section first">
            <div class="cap-wf-editor-title"><span>1</span><div><strong>规则信息</strong><small>明确这条规则解决的问题</small></div></div>
            <div class="cap-grid">
              <label class="cap-field"><span>规则名称 *</span><input class="cap-input" name="name" value="${e(item.name)}" required placeholder="例如：上不了网综合排查"></label>
              <label class="cap-field"><span>问题类型 *</span><select class="cap-select" id="capWfIssueType" name="issue_type_choice" required>${capWfIssueOptions(item.issue_type)}</select></label>
              <label class="cap-field wide" id="capWfCustomIssueField" hidden><span>自定义问题类型 *</span><input class="cap-input" id="capWfCustomIssue" name="issue_type_custom" placeholder="例如：赠品漏发"></label>
              <label class="cap-field wide"><span>规则说明（可选）</span><textarea class="cap-textarea" name="description" placeholder="例如：先核对订单和状态，再给出一次明确的处理方案">${e(item.description)}</textarea></label>
            </div>
          </section>

          <section class="cap-wf-editor-section">
            <div class="cap-wf-editor-title"><span>2</span><div><strong>买家常见说法</strong><small>填写买家消息中会出现的说法</small></div></div>
            <div class="cap-wf-value-list" id="capWfEntryRows">${capWfListRows('entry',item.entry_conditions)}</div>
            <button class="cap-btn cap-wf-add" data-wf-list-add="entry" type="button">添加一种说法</button>
          </section>

          <section class="cap-wf-editor-section">
            <div class="cap-wf-editor-title"><span>3</span><div><strong>处理步骤</strong><small>按从上到下的顺序排列</small></div></div>
            <div class="cap-wf-step-list" id="capWfStepRows">${(steps.length?steps:[{name:'',action:'guide'}]).map((step,index)=>capWfStepRow(step,steps.length?index:'')).join('')}</div>
            <button class="cap-btn cap-wf-add" id="capWfStepAdd" type="button">添加处理步骤</button>
          </section>

          <section class="cap-wf-editor-section">
            <div class="cap-wf-editor-title"><span>4</span><div><strong>完成和转人工</strong><small>定义结束条件与人工接手边界</small></div></div>
            <label class="cap-field"><span>怎样算处理完成 *</span><textarea class="cap-textarea" name="success_criteria" required placeholder="例如：状态恢复正常，且买家确认已经可以使用">${e(item.success_criteria)}</textarea></label>
            <div class="cap-wf-subtitle">出现以下情况时转人工</div>
            <div class="cap-wf-value-list" id="capWfEscalationRows">${capWfListRows('escalation',item.escalation_rules)}</div>
            <button class="cap-btn cap-wf-add" data-wf-list-add="escalation" type="button">添加转人工条件</button>
          </section>

          <section class="cap-wf-editor-section">
            <div class="cap-wf-editor-title"><span>5</span><div><strong>使用设置</strong></div></div>
            <div class="cap-grid">
              <label class="cap-field"><span>保存后的状态</span><select class="cap-select" name="status"><option value="published" ${item.status==='published'?'selected':''}>立即使用</option><option value="draft" ${item.status==='draft'?'selected':''}>保存为草稿</option><option value="disabled" ${item.status==='disabled'?'selected':''}>暂时停用</option></select></label>
              <label class="cap-field"><span>匹配顺序</span><select class="cap-select" name="priority">${capWfPriorityOptions(item.priority)}</select></label>
            </div>
          </section>
        </div>
      </form>
    `,'<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capWorkflowSave" type="button">保存规则</button>');
    $('capModal').classList.add('workflow-open');capWfRefreshStepOrder();
  }
  async function capSaveWorkflow(button){
    const form=$('capWorkflowForm');if(!form||!form.reportValidity())return;
    const fd=new FormData(form),id=form.dataset.id;
    const choice=String(fd.get('issue_type_choice')||''),issueType=(choice==='__custom__'?String(fd.get('issue_type_custom')||''):choice).trim();
    const entryConditions=capWfReadList('entry'),steps=capWfCollectSteps();
    if(!issueType)return toast('请选择或填写问题类型');
    if(!entryConditions.length)return toast('请至少填写一句买家常见说法');
    if(!steps.length)return toast('请至少添加一个处理步骤');
    if(steps.some(step=>!step.name))return toast('请把每个处理步骤填写完整');
    const editor=capState.workflowEditor||{},requiredFields=[...(editor.requiredFields||[])];
    if(!requiredFields.length&&steps.some(step=>step.action==='tool'))requiredFields.push('account_status','real_name_status','plan_status');
    const payload={
      name:String(fd.get('name')||'').trim(),
      issue_type:issueType,
      description:String(fd.get('description')||'').trim(),
      status:fd.get('status')||'draft',
      priority:Number(fd.get('priority')||50),
      version:Number(editor.item?.version||1),
      entry_conditions:entryConditions,
      required_fields:requiredFields,
      steps,
      success_criteria:String(fd.get('success_criteria')||'').trim(),
      escalation_rules:capWfReadList('escalation')
    };
    if(!payload.name)return toast('请给规则起个名字');
    capButtonBusy(button,true,'保存中…');
    try{
      await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/workflows${id?`/${encodeURIComponent(id)}`:''}`,{method:id?'PUT':'POST',body:JSON.stringify(payload)});
      capCloseModal();toast('规则已保存');await capLoadWorkflows();
    }catch(error){toast('保存失败：'+error.message);}
    finally{capButtonBusy(button,false);}
  }
  async function capLoadImport(){
    capState.importPreview=null;capState.importPayload=null;
    $('capContent').innerHTML=`<div class="cap-section-head"><div><h2>聊天导入</h2><p>支持 TXT、CSV、JSON、JSONL、ZIP 和探域嵌套聊天格式。</p></div></div><div class="cap-notice"><strong>安全说明：</strong>只导入本地历史，不触发影子回复，不调用真实回复接口，不会向买家发送消息。</div>
      <div class="cap-card"><div class="cap-card-body"><div class="cap-grid"><label class="cap-field wide"><span>聊天文件</span><input class="cap-input" id="capImportFile" type="file" accept=".txt,.csv,.json,.jsonl,.zip"/></label><label class="cap-field"><span>账号</span><input class="cap-input" id="capImportAccount" value="${e(capState.account)}"/></label><label class="cap-field"><span>指定买家 ID（可选）</span><input class="cap-input" id="capImportBuyer"/></label></div><div class="cap-actions" style="margin-top:12px"><button class="cap-btn primary" id="capImportPreviewBtn" type="button">先预览</button><button class="cap-btn success" id="capImportConfirmBtn" type="button" disabled>确认导入</button></div><div id="capImportResult"></div></div></div>`;
  }
  async function capPreviewImport(button){
    const file=$('capImportFile').files?.[0];if(!file)return toast('请先选择聊天文件');capButtonBusy(button,true,'解析中…');
    try{const payload={filename:file.name,content_base64:await capFileBase64(file),account:$('capImportAccount').value.trim(),buyer_id:$('capImportBuyer').value.trim()};const result=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/import/chats/preview`,{method:'POST',body:JSON.stringify(payload)});capState.importPreview=result;capState.importPayload=payload;$('capImportConfirmBtn').disabled=false;$('capImportResult').innerHTML=`<div class="cap-result"><pre>${e(jsonText(result))}</pre></div>`;toast(`预览完成：解析 ${result.parsed_messages??result.parsed??0} 条`);}catch(error){toast('预览失败：'+error.message);}finally{capButtonBusy(button,false);}
  }
  const capGrowthStatusLabels={draft:'草稿',submitted:'待审核',approved:'已通过',rejected:'已驳回',archived:'已归档'};
  const capGrowthRiskLabels={low:'低风险',medium:'中风险',high:'高风险'};
  const capGrowthDeploymentStatusLabels={draft:'落地草稿',published:'已落地'};
  function capGrowthStatusTag(status){return status==='approved'?'green':status==='rejected'?'red':status==='submitted'?'orange':'';}
  function capGrowthTypeLabel(value){return (capState.growth?.proposal_types||{})[value]||value||'未分类';}
  function capGrowthClusterTitle(row){
    const dimensions=[row.intent&&`意图：${row.intent_label||'其他问题'}`,row.lifecycle&&`客户阶段：${row.lifecycle_label||'其他客户阶段'}`,row.route_target&&`处理方式：${row.route_target_label||'其他处理方式'}`].filter(Boolean);
    return `${row.signal_label||'待改进问题'}${dimensions.length?' · '+dimensions.join(' / '):''}`;
  }
  function capGrowthProposalActions(row){
    if(!capCanModule('growth',true))return '';
    if(['draft','rejected'].includes(row.status))return `<button class="cap-btn" data-growth-edit="${e(row.id)}" type="button">编辑</button><button class="cap-btn primary" data-growth-status="${e(row.id)}" data-status="submitted" type="button">提交审核</button>`;
    if(row.status==='submitted')return `<button class="cap-btn success" data-growth-status="${e(row.id)}" data-status="approved" type="button">通过</button><button class="cap-btn danger" data-growth-status="${e(row.id)}" data-status="rejected" type="button">驳回</button>`;
    if(row.status==='approved'){
      if(row.deployment?.status==='published')return `<button class="cap-btn" data-growth-deployment="${e(row.id)}" type="button">查看落地结果</button>`;
      const deploy=row.deployment?`<button class="cap-btn primary" data-growth-deployment="${e(row.id)}" type="button">检查并发布</button>`:`<button class="cap-btn primary" data-growth-prepare="${e(row.id)}" type="button">生成落地草稿</button>`;
      return `${deploy}<button class="cap-btn" data-growth-status="${e(row.id)}" data-status="draft" type="button">退回草稿</button>`;
    }
    return '';
  }
  async function capLoadGrowth(status=capState.growthProposalStatus||'all'){
    capState.growthProposalStatus=status;
    const shop=encodeURIComponent(capState.shopId);
    const [overview,proposalData]=await Promise.all([
      fetchJSON(`${CAP_BASE}/shops/${shop}/growth/overview?window_days=30&cluster_limit=16`),
      fetchJSON(`${CAP_BASE}/shops/${shop}/growth/proposals?status=${encodeURIComponent(status)}&limit=100`),
    ]);
    capState.growth={...overview,proposal_types:{...(overview.proposal_types||{}),...(proposalData.proposal_types||{})}};
    capState.growthClusters=overview.clusters||[];
    capState.growthProposals=proposalData.proposals||[];
    const summary=overview.summary||{},proposalSummary=overview.proposal_summary||{};
    const clusterRows=capState.growthClusters.map(row=>{
      const samples=(row.samples||[]).map(sample=>`<li>${e(sample.question_excerpt||sample.issue_type_label||'未保留问题摘要')}</li>`).join('');
      return `<article class="cap-growth-item">
        <div class="cap-row-top"><div class="cap-row-title"><strong>${e(capGrowthClusterTitle(row))}</strong><small>近 30 天 ${e(row.count||0)} 次 · 最近 ${e(fmtTime(row.last_seen_at))}</small></div>
        <div class="cap-row-actions"><span class="cap-tag ${row.severity==='high'?'red':row.severity==='medium'?'orange':''}">${e(capGrowthRiskLabels[row.severity]||row.severity)}</span>${row.avoidable&&capCanModule('growth',true)?`<button class="cap-btn" data-growth-propose="${e(row.cluster_key)}" type="button">形成建议</button>`:''}${capCanModule('growth',true)?`<button class="cap-btn danger" data-growth-delete="${e(row.cluster_key)}" type="button">删除</button>`:''}</div></div>
        ${samples?`<ul class="cap-growth-samples">${samples}</ul>`:'<div class="cap-help">该问题簇没有可展示的消息摘要</div>'}
      </article>`;
    }).join('');
    const proposalRows=capState.growthProposals.map(row=>{const deployment=row.deployment;return `<article class="cap-growth-item">
      <div class="cap-row-top"><div class="cap-row-title"><strong>${e(row.title)}</strong><small>${e(capGrowthTypeLabel(row.proposal_type))} · ${e(capGrowthRiskLabels[row.risk_level]||row.risk_level)} · 更新于 ${e(fmtTime(row.updated_at))}</small></div><div class="cap-row-actions">${capGrowthProposalActions(row)}</div></div>
      ${row.problem_summary?`<div class="cap-row-content">${e(row.problem_summary)}</div>`:''}
      ${row.draft_content?`<div class="cap-growth-draft">${e(row.draft_content)}</div>`:''}
      <div class="cap-meta"><span class="cap-tag ${capGrowthStatusTag(row.status)}">${e(capGrowthStatusLabels[row.status]||row.status)}</span>${deployment?`<span class="cap-tag ${deployment.status==='published'?'green':'orange'}">${e(capGrowthDeploymentStatusLabels[deployment.status]||deployment.status)}</span>`:''}<span class="cap-tag">${e(row.apply_scope||'shop')} 范围</span><span class="cap-tag">证据 ${e(row.evidence_count||0)} 条</span>${deployment?.applied_resource_id?`<span class="cap-tag">落地到 ${e(capGrowthTypeLabel(row.proposal_type))}</span>`:''}${row.review_note?`<span class="cap-tag">审核：${e(row.review_note)}</span>`:''}</div>
    </article>`;}).join('');
    $('capContent').innerHTML=`
      <div class="cap-section-head"><div><h2>成长中心</h2><p>当前店铺近 30 天运行观察与学习建议</p></div><div class="cap-actions">${capCanModule('growth',true)?'<button class="cap-btn primary" id="capGrowthAdd" type="button">新增建议</button>':''}</div></div>
      <div class="cap-notice safe"><strong>审核模式</strong>：自动采集只生成问题信号；学习建议不会自动修改知识库、固定回复、接待分工或售后规则。</div>
      <div class="cap-stats cap-growth-stats">
        <div class="cap-stat"><div class="v">${e(summary.total||0)}</div><div class="k">已观察任务</div></div>
        <div class="cap-stat"><div class="v">${e(summary.improvement_signals||0)}</div><div class="k">待改进信号</div></div>
        <div class="cap-stat"><div class="v">${e(summary.avoidable_handoff||0)}</div><div class="k">可能可避免转人工</div></div>
        <div class="cap-stat"><div class="v">${e(summary.mandatory_handoff||0)}</div><div class="k">合理转人工复核</div></div>
        <div class="cap-stat"><div class="v">${e(proposalSummary.submitted||0)}</div><div class="k">待审核建议</div></div>
      </div>
      <div class="cap-growth-layout">
        <section class="cap-growth-section"><div class="cap-growth-section-head"><div><strong>问题雷达</strong><small>${e(capState.growthClusters.length)} 个高频问题簇</small></div></div><div class="cap-growth-list">${clusterRows||'<div class="cap-empty small">当前窗口内没有待分析信号</div>'}</div></section>
        <section class="cap-growth-section"><div class="cap-growth-section-head"><div><strong>学习建议</strong><small>通过后生成草稿，人工确认发布</small></div><select class="cap-select" id="capGrowthProposalStatus"><option value="all" ${status==='all'?'selected':''}>全部状态</option><option value="draft" ${status==='draft'?'selected':''}>草稿</option><option value="submitted" ${status==='submitted'?'selected':''}>待审核</option><option value="approved" ${status==='approved'?'selected':''}>已通过</option><option value="rejected" ${status==='rejected'?'selected':''}>已驳回</option></select></div><div class="cap-growth-list">${proposalRows||'<div class="cap-empty small">当前筛选条件下没有学习建议</div>'}</div></section>
      </div>`;
  }
  function capGrowthSuggestedType(cluster){
    return {knowledge_gap:'knowledge',routing_unknown:'routing',product_context_gap:'routing',tool_gap:'tool',processing_timeout:'tool',avoidable_handoff:'workflow',required_handoff:'handoff',fallback:'workflow',model_failure:'prompt'}[cluster?.signal_type]||'regression';
  }
  function capGrowthRecommendedRoute(cluster){
    return {network_issue:'售后故障诊断',device_issue:'售后故障诊断',aftersale:'售后故障诊断',aftersale_query:'售后故障诊断',activation:'激活与使用指导',usage_help:'激活与使用指导',package_sales:'套餐咨询',logistics:'物流处理',presale:'售前接待',product_spec:'补充商品信息后接待',refund:'转人工处理',complaint:'转人工处理'}[cluster?.intent]||'对应业务接待流程';
  }
  function capGrowthSuggestedDraft(cluster){
    if(!cluster)return '';
    const intent=cluster.intent_label||'该类客户问题',lifecycle=cluster.lifecycle_label||'当前客户阶段';
    const route=cluster.route_target_label||'当前处理方式',recommendedRoute=capGrowthRecommendedRoute(cluster);
    const count=Number(cluster.count||0),sample=(cluster.samples||[]).map(item=>item.question_excerpt).find(Boolean)||'';
    const actions={
      knowledge_gap:`补充“${intent}”知识条目，覆盖“${lifecycle}”场景。知识内容应包含可直接回复的标准结论、回复前需要确认的信息，以及必须转人工的边界；同时把本问题簇的常见问法加入相似问法。`,
      routing_unknown:`在接待分工中补充“${intent}”识别规则，命中后优先进入“${recommendedRoute}”。识别条件应覆盖本问题簇的常见说法，无法确认时再澄清或转人工。`,
      product_context_gap:`在回答“${intent}”前先收集商品链接、型号或设备号，拿到商品信息后再进入“${recommendedRoute}”；信息仍不完整时使用统一澄清话术，避免猜测商品参数。`,
      tool_gap:`补齐“${intent}”处理所需的订单、设备或套餐状态查询。查询成功后按实时结果回答；查询失败时使用明确的兜底口径，并保留转人工入口。`,
      processing_timeout:`为“${intent}”处理增加一次受控重试和稳定兜底。超过时限后停止继续等待，先给出可执行的基础处理步骤；确实需要实时结果时再转人工。`,
      avoidable_handoff:`补充“${intent}”标准售后流程，让 AI 先完成必要信息收集和基础排查。只有命中高风险条件、实时数据无法取得或标准步骤无效时才转人工，并把已收集信息一并交给人工。`,
      required_handoff:`保留“${intent}”转人工规则，并在转人工前自动收集订单、设备和问题现象，生成简短处理摘要，减少人工重复询问。`,
      fallback:`针对“${intent}”补充专用处理规则，替代当前“${route}”。优先复用已审核知识和固定回复；信息不足时只追问决定下一步所必需的内容。`,
      model_failure:`提高“${intent}”场景下的模型调用稳定性：失败后进行一次受控重试，仍失败则使用已审核的固定兜底回复，并记录失败原因供排查，不把失败结果当作业务问题学习。`,
    };
    const action=actions[cluster.signal_type]||`将“${intent}”问题簇加入回归案例，补充对应的知识、接待规则或处理流程，减少继续进入“${route}”。`;
    const evidence=`近 30 天出现 ${count} 次，客户阶段为“${lifecycle}”，当前处理方式为“${route}”。${sample?`\n典型问法：${sample}`:''}`;
    const acceptance=`使用本问题簇的 ${count} 条历史记录回放，确认能够识别该问题、给出明确下一步，并且不增加错误承诺或不合理转人工。`;
    return `问题依据\n${evidence}\n\n改进动作\n${action}\n\n验收标准\n${acceptance}`;
  }
  function capOpenGrowthProposal(cluster=null,row=null){
    const item=row||{};
    const type=item.proposal_type||capGrowthSuggestedType(cluster);
    const risk=item.risk_level||cluster?.severity||'medium';
    const title=item.title||(cluster?`${cluster.signal_label||'能力缺口'}：${cluster.intent_label||cluster.route_target_label||'待分析场景'}`:'');
    const summary=item.problem_summary||(cluster?capGrowthClusterTitle(cluster):'');
    const draft=item.draft_content||(cluster?capGrowthSuggestedDraft(cluster):'');
    const samples=cluster?(cluster.samples||[]).map(sample=>sample.question_excerpt).filter(Boolean):[];
    const typeOptions=Object.entries(capState.growth?.proposal_types||{}).map(([value,label])=>`<option value="${e(value)}" ${value===type?'selected':''}>${e(label)}</option>`).join('');
    capModal(row?'编辑学习建议':cluster?'确认改进建议':'新增学习建议',`<form id="capGrowthProposalForm" data-id="${e(item.id||'')}">
      <input type="hidden" name="problem_key" value="${e(item.problem_key||cluster?.cluster_key||'')}">
      <input type="hidden" name="evidence_count" value="${e(item.evidence_count||cluster?.count||0)}">
      <div class="cap-grid one">
        <label class="cap-field wide"><span>建议标题 *</span><input class="cap-input" name="title" value="${e(title)}" required></label>
        <label class="cap-field wide"><span>发现的问题</span><textarea class="cap-textarea" name="problem_summary">${e(summary)}</textarea></label>
        <label class="cap-field wide"><span>改进方案初稿</span><textarea class="cap-textarea" name="draft_content" rows="12">${e(draft)}</textarea></label>
      </div>
      <details class="cap-advanced"><summary>范围与风险</summary><div class="cap-grid compact">
        <label class="cap-field"><span>准备改进</span><select class="cap-select" name="proposal_type">${typeOptions}</select></label>
        <label class="cap-field"><span>改动风险</span><select class="cap-select" name="risk_level"><option value="low" ${risk==='low'?'selected':''}>低风险</option><option value="medium" ${risk==='medium'?'selected':''}>中风险</option><option value="high" ${risk==='high'?'selected':''}>高风险</option></select></label>
        <label class="cap-field"><span>适用范围</span><select class="cap-select" name="apply_scope"><option value="shop" ${(item.apply_scope||'shop')==='shop'?'selected':''}>当前店铺</option><option value="product" ${item.apply_scope==='product'?'selected':''}>指定商品</option><option value="issue" ${item.apply_scope==='issue'?'selected':''}>问题类型</option></select></label>
        <label class="cap-field"><span>商品 ID</span><input class="cap-input" name="product_id" value="${e(item.product_id||'')}"></label>
      </div></details>${samples.length?`<div class="cap-result"><strong>脱敏样例</strong><pre>${e(samples.join('\n'))}</pre></div>`:''}
    </form>`,`<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capGrowthProposalSave" type="button">保存建议</button>`);
  }
  async function capSaveGrowthProposal(button){
    const form=$('capGrowthProposalForm'),data=Object.fromEntries(new FormData(form).entries()),id=form.dataset.id;
    data.evidence_count=Number(data.evidence_count||0);capButtonBusy(button,true,'保存中…');
    try{await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/growth/proposals${id?`/${encodeURIComponent(id)}`:''}`,{method:id?'PUT':'POST',body:JSON.stringify(data)});capCloseModal();toast('学习建议已保存为草稿');await capLoadGrowth();}finally{capButtonBusy(button,false);}
  }
  async function capTransitionGrowthProposal(id,status){
    let note='';
    if(['approved','rejected'].includes(status)){const label=status==='approved'?'通过':'驳回';const input=prompt(`${label}这条学习建议。可填写审核备注：`,'');if(input===null)return;note=input;}
    await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/growth/proposals/${encodeURIComponent(id)}/status`,{method:'PUT',body:JSON.stringify({status,review_note:note})});
    toast(status==='approved'?'建议已通过，等待人工发布':status==='submitted'?'已提交审核':'建议状态已更新');await capLoadGrowth();
  }
  function capGrowthCanPublish(row){
    const permission=['routing','handoff','prompt'].includes(row?.proposal_type)?'capabilities.strategy':'capabilities.write';
    return capHasPerm(permission);
  }
  function capGrowthTargetModule(proposalType){
    return {knowledge:'knowledge',script:'scripts',workflow:'workflows',routing:'routing',handoff:'force_handoff',prompt:'prompts'}[proposalType]||'';
  }
  function capGrowthDeploymentFields(row){
    const deployment=row.deployment||{},draft=deployment.draft||{},type=deployment.resource_type;
    const stageValues=['*','presale_no_order','ordered_not_received','received_no_package','package_customer','device_customer_unknown_stage','facts_unknown'];
    const intentValues=['*',...capOperatorIntentCodes()];
    const targetValues=['presale_script','logistics_support','activation_support','package_sales','activation_then_package','aftersales_diagnosis','package_service','product_clarify','human_handoff','hybrid_fallback'];
    const options=(values,selected,labeler)=>values.map(value=>`<option value="${e(value)}" ${value===selected?'selected':''}>${e(labeler(value))}</option>`).join('');
    if(['routing','handoff'].includes(type))return `<div class="cap-grid">
      <label class="cap-field wide"><span>规则名称</span><input class="cap-input" name="name" value="${e(draft.name||'')}"></label>
      <label class="cap-field"><span>客户阶段</span><select class="cap-select" name="stage">${options(stageValues,(draft.stages||[])[0]||'*',capRouteStageText)}</select></label>
      <label class="cap-field"><span>客户在问</span><select class="cap-select" name="intent">${options(intentValues,(draft.intents||[])[0]||'*',capRouteIntentText)}</select></label>
      <label class="cap-field wide"><span>接待方式</span><select class="cap-select" name="target">${options(targetValues,draft.target||'hybrid_fallback',capRouteTargetText)}</select></label>
      <label class="cap-field wide"><span>规则说明</span><textarea class="cap-textarea" name="description">${e(draft.description||'')}</textarea></label>
      <label class="cap-field"><span>匹配优先级</span><input class="cap-input" name="priority" type="number" value="${e(draft.priority||650)}"></label>
    </div>`;
    if(type==='workflow')return `<div class="cap-grid one">
      <label class="cap-field"><span>售后规则名称</span><input class="cap-input" name="name" value="${e(draft.name||'')}"></label>
      <label class="cap-field"><span>针对的问题</span><input class="cap-input" name="issue_type" value="${e(draft.issue_type||'')}"></label>
      <label class="cap-field"><span>客户常见说法</span><textarea class="cap-textarea" name="entry_conditions">${e(listText(draft.entry_conditions||[]))}</textarea></label>
      <label class="cap-field"><span>处理步骤</span><textarea class="cap-textarea" name="steps" rows="8">${e(capWfStepsEditorText(draft.steps||[]))}</textarea></label>
      <label class="cap-field"><span>怎样算解决</span><textarea class="cap-textarea" name="success_criteria">${e(draft.success_criteria||'')}</textarea></label>
      <label class="cap-field"><span>什么时候转人工</span><textarea class="cap-textarea" name="escalation_rules">${e(listText(draft.escalation_rules||[]))}</textarea></label>
    </div>`;
    if(type==='script')return `<div class="cap-grid one">
      <label class="cap-field"><span>固定回复标题</span><input class="cap-input" name="title" value="${e(draft.title||'')}"></label>
      <label class="cap-field"><span>客户可能怎么问</span><textarea class="cap-textarea" name="triggers">${e(listText(draft.triggers||[]))}</textarea></label>
      <label class="cap-field"><span>确认后回复客户的内容</span><textarea class="cap-textarea" name="answer" rows="8">${e(draft.answer||'')}</textarea></label>
      <label class="cap-field"><span>适用于</span><select class="cap-select" name="lifecycle"><option value="presale_no_order" ${(draft.lifecycle||[]).includes('presale_no_order')?'selected':''}>未下单咨询</option><option value="post_order" ${(draft.lifecycle||[]).includes('post_order')?'selected':''}>已下单咨询</option><option value="all" ${(draft.lifecycle||[]).includes('all')?'selected':''}>全部阶段</option></select></label>
    </div>`;
    if(type==='knowledge')return `<div class="cap-grid one">
      <label class="cap-field"><span>知识标题</span><input class="cap-input" name="title" value="${e(draft.title||'')}"></label>
      <label class="cap-field"><span>经过业务确认的知识内容</span><textarea class="cap-textarea" name="content" rows="12">${e(draft.content||'')}</textarea></label>
      <label class="cap-field"><span>适用商品 ID（可选）</span><input class="cap-input" name="product_id" value="${e(draft.product_id||'')}"></label>
      ${(draft.reference_questions||[]).length?`<div class="cap-result"><strong>参考问法</strong><pre>${e((draft.reference_questions||[]).join('\n'))}</pre></div>`:''}
    </div>`;
    if(type==='prompt')return `<div class="cap-grid one">
      <label class="cap-field"><span>提示词类型</span><select class="cap-select" name="prompt_name"><option value="classify" ${draft.prompt_name==='classify'?'selected':''}>意图分类</option><option value="presale" ${draft.prompt_name==='presale'?'selected':''}>售前</option><option value="aftersale" ${draft.prompt_name==='aftersale'?'selected':''}>售后</option><option value="shipping" ${draft.prompt_name==='shipping'?'selected':''}>物流</option><option value="default" ${draft.prompt_name==='default'?'selected':''}>默认</option></select></label>
      <label class="cap-field"><span>追加的模型处理要求</span><textarea class="cap-textarea" name="instruction" rows="12">${e(draft.instruction||'')}</textarea></label>
    </div>`;
    return `<div class="cap-grid one"><label class="cap-field"><span>计划完成的工作</span><textarea class="cap-textarea" name="planned_action" rows="7">${e(draft.planned_action||'')}</textarea></label><label class="cap-field"><span>实际完成内容或关联任务</span><textarea class="cap-textarea" name="completion_note" rows="6">${e(draft.completion_note||'')}</textarea></label></div>`;
  }
  function capGrowthDeploymentPayload(form,row){
    const fd=new FormData(form),deployment=row.deployment||{},current={...(deployment.draft||{})},type=deployment.resource_type;
    if(['routing','handoff'].includes(type))return {...current,name:String(fd.get('name')||'').trim(),stages:[fd.get('stage')||'*'],intents:[fd.get('intent')||'*'],target:fd.get('target')||'hybrid_fallback',description:String(fd.get('description')||'').trim(),priority:Number(fd.get('priority')||650)};
    if(type==='workflow')return {...current,name:String(fd.get('name')||'').trim(),issue_type:String(fd.get('issue_type')||'').trim(),entry_conditions:splitList(fd.get('entry_conditions')),steps:capWfParseSteps(fd.get('steps')),success_criteria:String(fd.get('success_criteria')||'').trim(),escalation_rules:splitList(fd.get('escalation_rules'))};
    if(type==='script')return {...current,title:String(fd.get('title')||'').trim(),triggers:splitList(fd.get('triggers')),answer:String(fd.get('answer')||'').trim(),lifecycle:[fd.get('lifecycle')||'post_order']};
    if(type==='knowledge')return {...current,title:String(fd.get('title')||'').trim(),content:String(fd.get('content')||'').trim(),product_id:String(fd.get('product_id')||'').trim()};
    if(type==='prompt')return {...current,prompt_name:fd.get('prompt_name')||'default',instruction:String(fd.get('instruction')||'').trim()};
    return {...current,planned_action:String(fd.get('planned_action')||'').trim(),completion_note:String(fd.get('completion_note')||'').trim()};
  }
  function capOpenGrowthDeployment(row){
    const deployment=row?.deployment;if(!deployment)return capPrepareGrowthDeployment(row?.id);
    const published=deployment.status==='published',validation=deployment.validation||{},blockers=validation.blockers||[];
    const check=published?`<div class="cap-notice safe">已于 ${e(fmtTime(deployment.published_at))} 落地到${e(capGrowthTypeLabel(row.proposal_type))}。</div>`:validation.publishable?'<div class="cap-notice safe">草稿字段完整，可以在确认内容后发布。</div>':`<div class="cap-notice warn">${e(blockers.join('；')||'草稿还需要补充')}</div>`;
    const permission=capGrowthCanPublish(row),publishLabel=deployment.resource_type==='manual'?'标记已完成':`发布到${capGrowthTypeLabel(row.proposal_type)}`,targetModule=capGrowthTargetModule(row.proposal_type);
    const footer=published?`<button class="cap-btn" data-cap-close type="button">关闭</button>${targetModule?`<button class="cap-btn primary" data-growth-open-module="${e(targetModule)}" type="button">查看${e(capGrowthTypeLabel(row.proposal_type))}</button>`:''}`:`<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn" id="capGrowthDeploymentSave" data-id="${e(row.id)}" type="button">保存草稿</button>${permission?`<button class="cap-btn primary" id="capGrowthDeploymentPublish" data-id="${e(row.id)}" type="button">${e(publishLabel)}</button>`:''}`;
    capModal(published?'落地结果':`落地到${capGrowthTypeLabel(row.proposal_type)}`,`<form id="capGrowthDeploymentForm" data-id="${e(row.id)}">${check}<div style="margin-top:12px">${capGrowthDeploymentFields(row)}</div></form>`,footer);
    if(published)$('capGrowthDeploymentForm')?.querySelectorAll('input,textarea,select').forEach(element=>element.disabled=true);
  }
  async function capPrepareGrowthDeployment(id){
    const result=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/growth/proposals/${encodeURIComponent(id)}/deployment`,{method:'POST',body:'{}'});
    const row=capState.growthProposals.find(item=>item.id===id);if(row){row.deployment=result.deployment;capOpenGrowthDeployment(row);}else await capLoadGrowth();
  }
  async function capSaveGrowthDeployment(button,publish=false){
    const id=button.dataset.id,row=capState.growthProposals.find(item=>item.id===id),form=$('capGrowthDeploymentForm');if(!row||!form)return;
    if(publish&&!confirm(`确定把这条建议发布到${capGrowthTypeLabel(row.proposal_type)}？发布后会影响当前店铺后续的 AI 处理。`))return;
    capButtonBusy(button,true,publish?'发布中…':'保存中…');
    try{
      const saved=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/growth/proposals/${encodeURIComponent(id)}/deployment`,{method:'PUT',body:JSON.stringify({draft:capGrowthDeploymentPayload(form,row)})});
      row.deployment=saved.deployment;
      if(publish){
        if(!saved.deployment?.validation?.publishable){toast(saved.deployment?.validation?.blockers?.[0]||'草稿还不能发布');return capOpenGrowthDeployment(row);}
        await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/growth/proposals/${encodeURIComponent(id)}/deployment/publish`,{method:'POST',body:'{}'});
        capCloseModal();toast(`已发布到${capGrowthTypeLabel(row.proposal_type)}`);await capLoadGrowth();
      }else{capCloseModal();toast('落地草稿已保存');await capLoadGrowth();}
    }finally{capButtonBusy(button,false);}
  }
  async function capDeleteGrowthCluster(clusterKey){
    const row=capState.growthClusters.find(item=>item.cluster_key===clusterKey);if(!row)return;
    const warning=`确定删除这组成长数据？\n\n${capGrowthClusterTitle(row)}\n共 ${row.count||0} 条观察记录。原始聊天不会删除；同类问题再次发生时会重新进入问题雷达。`;
    if(!confirm(warning))return;
    const result=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/growth/clusters/${encodeURIComponent(clusterKey)}`,{method:'DELETE',body:'{}'});
    toast(`已删除 ${result.deleted_count||row.count||0} 条成长观察`);await capLoadGrowth();
  }
  function capSyncCorrectionScope(){
    const scope=$('capCorrectionEditScope')?.value||'shop',product=$('capCorrectionEditProduct'),issue=$('capCorrectionEditIssue');
    if(product)product.hidden=scope!=='product';
    if(issue)issue.hidden=scope!=='issue';
  }
  function capOpenCorrectionEditor(row){
    if(!row)return toast('纠正记录不存在');
    capModal('再次编辑纠正',`<form id="capCorrectionEditForm" data-id="${e(row.id)}" data-shop="${e(row.shop_id)}">
      <div class="cap-notice warn" style="margin-bottom:10px">保存后会自动回到待审核；在重新通过前，修改后的内容不会用于影子回复。</div>
      <label class="cap-field"><span>正确回复</span><textarea class="cap-textarea" name="expected_reply" maxlength="3000" required>${e(row.expected_reply||'')}</textarea></label>
      <div class="cap-correction-grid" style="margin-top:10px">
        <label class="cap-field"><span>错误类型</span><select class="cap-select" name="error_type"><option value="tone">语气/情绪</option><option value="script_match">话术匹配</option><option value="knowledge">知识错误</option><option value="card_status">工具/卡状态</option><option value="sop">流程/SOP</option><option value="prompt_following">未遵循提示词</option><option value="should_handoff">应转人工</option><option value="hallucination">幻觉/编造</option><option value="other">其他</option></select></label>
        <label class="cap-field"><span>改进动作</span><select class="cap-select" name="correction_action"><option value="script">话术</option><option value="knowledge">知识</option><option value="sop">SOP</option><option value="rule">规则</option><option value="handoff">转人工</option><option value="prompt">Prompt</option><option value="regression_only">仅回归用例</option></select></label>
        <label class="cap-field"><span>服务阶段</span><select class="cap-select" name="service_stage"><option value="presale">售前（无订单）</option><option value="aftersales">售后（有订单且售后意图）</option><option value="post_order">订单其他（物流/使用等）</option><option value="unknown">未识别</option></select></label>
        <label class="cap-field"><span>作用范围</span><select class="cap-select" id="capCorrectionEditScope" name="apply_scope"><option value="shop">当前店铺</option><option value="product">指定商品</option><option value="issue">当前问题类型</option><option value="all_mobile_wifi">全部移动 WiFi</option><option value="all_shops">全部店铺</option></select></label>
        <label class="cap-field"><span>错误说明</span><input class="cap-input" name="error_detail" maxlength="500" value="${e(row.error_detail||'')}" placeholder="可选"/></label>
        <label class="cap-field" id="capCorrectionEditProduct"><span>商品 ID</span><input class="cap-input" name="product_id" value="${e(row.product_id||'')}"/></label>
        <label class="cap-field" id="capCorrectionEditIssue"><span>问题类型</span><input class="cap-input" name="issue_type" value="${e(row.issue_type||'')}"/></label>
      </div>
    </form>`,`<button class="cap-btn" data-cap-close type="button">取消</button><button class="cap-btn primary" id="capCorrectionEditSave" type="button">保存并重新审核</button>`);
    const form=$('capCorrectionEditForm');
    form.elements.error_type.value=row.error_type||'other';
    form.elements.correction_action.value=row.correction_action||'regression_only';
    form.elements.service_stage.value=row.service_stage||'unknown';
    form.elements.apply_scope.value=row.apply_scope||'shop';
    capSyncCorrectionScope();
  }
  async function capSaveCorrection(button){
    const form=$('capCorrectionEditForm');if(!form||!form.reportValidity())return;
    const values=new FormData(form),scope=String(values.get('apply_scope')||'shop'),productId=String(values.get('product_id')||'').trim();
    if(scope==='product'&&!productId)return toast('请填写商品 ID');
    const payload={expected_reply:String(values.get('expected_reply')||'').trim(),error_type:String(values.get('error_type')||'other'),error_detail:String(values.get('error_detail')||'').trim(),correction_action:String(values.get('correction_action')||'regression_only'),service_stage:String(values.get('service_stage')||'unknown'),apply_scope:scope,product_id:productId,issue_type:String(values.get('issue_type')||'').trim()};
    capButtonBusy(button,true,'保存中…');
    try{
      await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(form.dataset.shop)}/corrections/${encodeURIComponent(form.dataset.id)}`,{method:'PUT',body:JSON.stringify(payload)});
      capCloseModal();toast('纠正已更新，请重新审核');await capLoadCorrections();
    }catch(error){toast('保存失败：'+error.message);}
    finally{capButtonBusy(button,false);}
  }
  function capCorrectionSelectedShopIds(){
    return Array.from(document.querySelectorAll('input[name="capCorrectionShop"]:checked')).map(input=>input.value);
  }
  async function capLoadCorrections(status=capState.correctionStatus,shopIds=capState.correctionShopIds,serviceStage=capState.correctionServiceStage){
    await capLoadShopOptions();
    status=['all','pending','approved','rejected'].includes(status)?status:'pending';
    serviceStage=['all','presale','aftersales','post_order','unknown'].includes(serviceStage)?serviceStage:'all';
    const available=new Set(capState.shops.map(row=>String(row.shop_id))),requested=Array.isArray(shopIds)?shopIds.map(String).filter(id=>available.has(id)):[];
    const selected=requested.length===capState.shops.length?[]:[...new Set(requested)];
    capState.correctionStatus=status;capState.correctionShopIds=selected;capState.correctionServiceStage=serviceStage;
    const params=new URLSearchParams({status,limit:'500'});if(selected.length)params.set('shop_ids',selected.join(','));if(serviceStage!=='all')params.set('service_stage',serviceStage);
    const endpoint=`${CAP_BASE}/corrections?${params}`;
    const data=await fetchJSON(endpoint);capState.corrections=data.corrections||[];
    capState.correctionsShopCount=Number(data.shop_count||0);
    const shopName=row=>row.shop_name||capShopName(capState.shops.find(shop=>String(shop.shop_id)===String(row.shop_id)))||row.shop_id||'未知店铺';
    const stageLabels={presale:'售前',aftersales:'售后',post_order:'订单其他',unknown:'未识别'},stageClasses={presale:'blue',aftersales:'orange',post_order:'green',unknown:''};
    const rows=capState.corrections.map(row=>`<article class="cap-row"><div class="cap-row-top"><div class="cap-row-title"><strong>${e(row.user_message||'未记录买家问题')}</strong><small>${e(shopName(row))} · ${e(row.shop_id||'')} · ${e(fmtTime(row.created_at))} · ${e(row.error_type||'other')} · ${e(row.correction_action||'regression_only')}</small></div><div class="cap-row-actions">${row.status!=='applied'?`<button class="cap-btn" data-correction-edit="${e(row.id)}" type="button">${row.status==='approved'?'再次编辑':'编辑'}</button>`:''}${row.status==='pending'?`<button class="cap-btn success" data-correction-review="${e(row.id)}" data-correction-shop="${e(row.shop_id)}" data-status="approved" type="button">通过</button><button class="cap-btn danger" data-correction-review="${e(row.id)}" data-correction-shop="${e(row.shop_id)}" data-status="rejected" type="button">拒绝</button>`:''}</div></div><div class="cap-grid" style="margin-top:9px"><div><span class="cap-tag">AI 原回复</span><div class="cap-row-content">${e(row.ai_reply||'—')}</div></div><div><span class="cap-tag blue">期望回复</span><div class="cap-row-content">${e(row.expected_reply||'—')}</div></div></div><div class="cap-meta"><span class="cap-tag ${row.status==='approved'?'green':row.status==='rejected'?'red':'orange'}">${e(row.status)}</span><span class="cap-tag ${stageClasses[row.service_stage]||''}">${e(stageLabels[row.service_stage]||'未识别')}</span><span class="cap-tag">范围 ${e(row.apply_scope||'shop')}</span><span class="cap-tag">${row.status==='approved'?(row.conversation_context?.length?'已启用整段对话语境匹配':'旧记录：仅原句匹配'):row.status==='rejected'?'已拒绝':'待人工审核'}</span></div>${row.review_note?`<div class="cap-row-content">审核备注：${e(row.review_note)}</div>`:''}</article>`).join('');
    const allShops=!selected.length,selectedSet=new Set(selected),shopLabel=allShops?'全部店铺':selected.length===1?shopName({shop_id:selected[0]}):`已选 ${selected.length} 家店铺`;
    const shopChecks=capState.shops.map(row=>`<label class="cap-correction-shop-option"><input type="checkbox" name="capCorrectionShop" value="${e(row.shop_id)}" ${allShops||selectedSet.has(String(row.shop_id))?'checked':''}><span>${e(capShopName(row))}</span><small>${e(row.shop_id)}</small></label>`).join('');
    const shopPicker=`<details class="cap-multi-select" id="capCorrectionShopPicker"><summary>${e(shopLabel)}</summary><div class="cap-multi-select-panel"><div class="cap-multi-select-tools"><button type="button" id="capCorrectionShopAll">全选</button><button type="button" id="capCorrectionShopNone">清空</button></div><div class="cap-correction-shop-options">${shopChecks||'<div class="cap-empty small">没有可选店铺</div>'}</div><button class="cap-btn primary cap-multi-select-apply" id="capCorrectionShopApply" type="button">应用</button></div></details>`;
    $('capContent').innerHTML=`<div class="cap-section-head"><div><h2>纠正记录</h2><p>${e(shopLabel)} · ${e(status==='pending'?'待审核':status==='all'?'全部状态':status==='approved'?'已通过':'已拒绝')} · ${e(serviceStage==='all'?'全部阶段':stageLabels[serviceStage]||'未识别')} · ${e(capState.corrections.length)} 条</p></div><div class="cap-actions">${shopPicker}<select class="cap-select" id="capCorrectionServiceStage" style="width:145px"><option value="all" ${serviceStage==='all'?'selected':''}>全部阶段</option><option value="presale" ${serviceStage==='presale'?'selected':''}>售前</option><option value="aftersales" ${serviceStage==='aftersales'?'selected':''}>售后</option><option value="post_order" ${serviceStage==='post_order'?'selected':''}>订单其他</option><option value="unknown" ${serviceStage==='unknown'?'selected':''}>未识别</option></select><select class="cap-select" id="capCorrectionStatus" style="width:130px"><option value="pending" ${status==='pending'?'selected':''}>待审核</option><option value="all" ${status==='all'?'selected':''}>全部状态</option><option value="approved" ${status==='approved'?'selected':''}>已通过</option><option value="rejected" ${status==='rejected'?'selected':''}>已拒绝</option></select><button class="cap-btn" id="capCorrectionRefresh" type="button">刷新</button></div></div><div class="cap-notice warn">模型不可违背纠正中的关键事实；模型不可用时才回退到纠正原文。只应用于影子回复，不会自动发给买家。</div><div class="cap-list">${rows||'<div class="cap-empty">当前筛选条件下没有纠正记录</div>'}</div>`;
  }

  const capShopStatusLabels={pending:'\u5f85\u63a5\u5165',active:'\u8fd0\u884c\u4e2d',disabled:'\u5df2\u505c\u7528',archived:'\u5df2\u5f52\u6863'};
  function capShopAuthLabel(value){
    const text=String(value||'unknown').toLowerCase();
    if(['authorized','active','yes','true','1'].includes(text))return '\u63a2\u57df\u5df2\u6388\u6743';
    if(['unauthorized','inactive','no','false','0'].includes(text))return '\u63a2\u57df\u672a\u6388\u6743';
    return '\u63a2\u57df\u6388\u6743\u672a\u77e5';
  }
  function capShopConnectionLabel(row){
    if(row.connected===false)return {label:'\u63a2\u57df\u672a\u8fde\u63a5',className:'red'};
    const state=String(row.connection_state||'unknown').toLowerCase();
    if(['healthy','running','connected','ready'].includes(state))return {label:'\u63a2\u57df\u8fde\u63a5\u6b63\u5e38',className:'green'};
    if(['stopped','idle'].includes(state))return {label:'\u63a2\u57df\u5df2\u505c\u6b62',className:'orange'};
    if(['error','failed','disconnected','offline'].includes(state))return {label:'\u63a2\u57df\u8fde\u63a5\u5f02\u5e38',className:'red'};
    if(row.connected===true)return {label:'\u63a2\u57df\u5df2\u8fde\u63a5',className:'green'};
    return {label:'\u63a2\u57df\u8fde\u63a5\u672a\u77e5',className:''};
  }
  function capShopBrainLabel(row){
    return row.brain_mode==='own'?{label:'\u6211\u7684\u5927\u8111',className:'blue'}:{label:'\u63a2\u57df\u5927\u8111',className:''};
  }
  function capShopSendLabel(row){
    if(row.brain_mode!=='own'||row.send_policy==='blocked')return {label:'\u6211\u65b9\u7981\u6b62\u53d1\u9001',className:''};
    if(!row.brain_armed)return {label:'\u5f85\u786e\u8ba4\u6258\u7ba1\u5173\u95ed',className:'orange'};
    if(row.send_policy==='shop')return {label:'\u6211\u65b9\u5168\u5e97\u53d1\u9001',className:'red'};
    return {label:`\u6211\u65b9\u767d\u540d\u5355 ${Number((row.allowed_buyer_ids||[]).length)} \u4eba`,className:'green'};
  }
  function capShopStatusActions(row){
    if(row.status==='pending')return `<button class="cap-btn primary" data-shop-onboard="${e(row.shop_id)}" type="button">\u63a5\u5165</button>`;
    if(row.status==='active')return `<button class="cap-btn primary" data-shop-brain="${e(row.shop_id)}" type="button">\u5927\u8111\u4e0e\u53d1\u9001</button><button class="cap-btn" data-shop-edit="${e(row.shop_id)}" type="button">\u6539\u8d44\u6599</button><button class="cap-btn" data-shop-status="disabled" data-shop-id="${e(row.shop_id)}" type="button">\u505c\u7528</button><button class="cap-btn danger" data-shop-status="archived" data-shop-id="${e(row.shop_id)}" type="button">\u5f52\u6863</button>`;
    if(row.status==='disabled')return `<button class="cap-btn success" data-shop-status="active" data-shop-id="${e(row.shop_id)}" type="button">\u6062\u590d</button><button class="cap-btn" data-shop-edit="${e(row.shop_id)}" type="button">\u6539\u8d44\u6599</button><button class="cap-btn danger" data-shop-status="archived" data-shop-id="${e(row.shop_id)}" type="button">\u5f52\u6863</button>`;
    return `<button class="cap-btn success" data-shop-status="active" data-shop-id="${e(row.shop_id)}" type="button">\u6062\u590d\u63a5\u5165</button><button class="cap-btn" data-shop-edit="${e(row.shop_id)}" type="button">\u6539\u8d44\u6599</button>`;
  }
  function capPlatformLabel(row){
    const p=String(row.platform||'').toLowerCase();
    if(p==='pdd'||String(row.shop_id||'').startsWith('mall_'))return '拼多多';
    if(p==='taobao'||String(row.shop_id||'').startsWith('tb_')||String(row.shop_id||'').includes('taobao'))return '淘宝/千牛';
    if(p==='jd')return '京东';
    if(p==='douyin')return '抖音';
    if(p==='kuaishou')return '快手';
    if(p==='1688')return '1688';
    return p&&p!=='unknown'?p:'平台未识别';
  }
  async function capOpenShopManager(refresh=false, button=null){
    if(refresh&&button)capButtonBusy(button,true,'扫描中…');
    try{
      const data=await fetchJSON(`${CAP_BASE}/shop-management${refresh?'/refresh':''}`,refresh?{method:'POST',body:JSON.stringify({})}:{});
      capState.managedShops=Array.isArray(data.shops)?data.shops:[];
      const scan=data.scan||{};
      const pendingShops=capState.managedShops.filter(row=>row.status==='pending');
      const onboardedShops=capState.managedShops.filter(row=>row.status!=='pending');
      const pendingView=capState.shopManagerView==='pending';
      const visibleShops=pendingView?pendingShops:onboardedShops;
      const rows=visibleShops.map(row=>{
        const accountText=(row.accounts||[]).join(' / ');
        const auth=capShopAuthLabel(row.authorization_status),status=capShopStatusLabels[row.status]||row.status||'未知';
        const authClass=auth==='探域已授权'?'green':auth==='探域未授权'?'red':'';
        const connection=capShopConnectionLabel(row),brain=capShopBrainLabel(row),send=capShopSendLabel(row);
        const platform=capPlatformLabel(row);
        return `<article class="cap-shop-row ${e(row.status)}"><div class="cap-shop-main"><strong>${e(capShopName(row))}</strong><small>${e(row.shop_id)}${accountText?' · '+e(accountText):''}</small><div class="cap-meta"><span class="cap-tag ${row.status==='active'?'green':row.status==='pending'?'orange':''}">${e(status)}</span><span class="cap-tag blue">${e(platform)}</span><span class="cap-tag ${brain.className}">${e(brain.label)}</span><span class="cap-tag ${send.className}">${e(send.label)}</span><span class="cap-tag ${authClass}">${e(auth)}</span><span class="cap-tag ${connection.className}">${e(connection.label)}</span><span class="cap-tag">来源 ${e(row.source||'unknown')}</span></div></div><div class="cap-row-actions">${capShopStatusActions(row)}</div></article>`;
      }).join('');
      const scanNote=refresh
        ? `<div class="cap-notice">扫描完成：登记 ${e(scan.total??capState.managedShops.length)} 家；本次新发现 ${e(scan.new_count??0)} 家${(scan.new_shop_ids&&scan.new_shop_ids.length)?('：'+e(scan.new_shop_ids.join('、'))):''}。新店为「待接入」状态，请点接入。</div>`
        : '';
      const views=`<div class="cap-shop-toolbar"><div><button class="cap-btn ${pendingView?'':'primary'}" data-shop-manager-view="onboarded" type="button">已接入 ${onboardedShops.length}</button><button class="cap-btn ${pendingView?'primary':''}" data-shop-manager-view="pending" type="button">待接入 ${pendingShops.length}</button></div><div><button class="cap-btn" id="capShopDiscover" type="button">重新扫描</button><button class="cap-btn primary" id="capShopManualAdd" type="button">新增店铺</button></div></div>`;
      const empty=pendingView?'当前没有待接入店铺。':'当前没有已接入店铺。';
      capModal('管理店铺',`${scanNote}${views}<div class="cap-shop-list">${rows||`<div class="cap-empty">${empty}</div>`}</div>`,`<button class="cap-btn" data-cap-close type="button">关闭</button>`);
      if(refresh)toast(`扫描完成：共 ${scan.total??capState.managedShops.length} 家，新增 ${scan.new_count??0} 家`);
    }catch(error){
      toast('扫描/加载店铺失败：'+(error.message||error));
      throw error;
    }finally{
      if(refresh&&button)capButtonBusy(button,false);
    }
  }
  async function capOpenAiReplySetup(){
    const data=await fetchJSON(`${CAP_BASE}/shop-management`);
    capState.managedShops=Array.isArray(data.shops)?data.shops:[];
    const activeShops=capState.managedShops.filter(row=>row.status==='active');
    const rows=activeShops.map(row=>{
      const connection=capShopConnectionLabel(row),platform=capPlatformLabel(row),armed=!!row.brain_armed;
      const accountText=(row.accounts||[]).join(' / ');
      const action=armed
        ? `<button class="cap-btn danger" data-ai-reply-toggle="${e(row.shop_id)}" data-enabled="0" type="button">停止</button>`
        : `<button class="cap-btn primary" data-ai-reply-toggle="${e(row.shop_id)}" data-enabled="1" type="button">开启 AI 回复</button>`;
      return `<article class="cap-shop-row active"><div class="cap-shop-main"><strong>${e(capShopName(row))}</strong><small>${e(row.shop_id)}${accountText?' · '+e(accountText):''}</small><div class="cap-meta"><span class="cap-tag blue">${e(platform)}</span><span class="cap-tag ${armed?'red':''}">${armed?'AI 回复已开启':'AI 回复未开启'}</span><span class="cap-tag ${connection.className}">${e(connection.label)}</span></div></div><div class="cap-row-actions">${action}<button class="cap-btn" data-shop-brain="${e(row.shop_id)}" data-brain-return="ai-reply" type="button">高级设置</button></div></article>`;
    }).join('');
    capModal('AI 回复',`<div class="cap-notice warn">开启前，请先在探域关闭对应店铺的自动托管。开启后，AI 会直接回复该店所有买家；各店铺互不影响。</div><div class="cap-shop-list">${rows||'<div class="cap-empty">当前没有运行中的店铺，请先接入或恢复店铺。</div>'}</div>`,`<button class="cap-btn" data-cap-close type="button">关闭</button><button class="cap-btn" id="capAiAdvancedShopManager" type="button">高级店铺管理</button>`);
  }
  async function capToggleAiReply(button){
    const shopId=button.dataset.aiReplyToggle,row=capState.managedShops.find(item=>item.shop_id===shopId);
    if(!row)return toast('店铺不存在');
    const enabled=button.dataset.enabled==='1',shopName=capShopName(row);
    const question=enabled
      ? `开启“${shopName}”的 AI 回复？\n\n请确认：\n1. 已在探域关闭这家店的自动托管；\n2. 开启后，AI 会直接回复这家店的所有买家。\n\n确认无误后继续。`
      : `停止“${shopName}”的 AI 回复？\n停止后仍会生成影子回复，但不会自动发给买家。`;
    if(!confirm(question))return;
    capButtonBusy(button,true,enabled?'开启中…':'停止中…');
    try{
      await fetchJSON(`${CAP_BASE}/shop-management/shops/${encodeURIComponent(shopId)}/ai-reply`,{method:'PATCH',body:JSON.stringify({enabled,confirm_tanyu_hosting_off:enabled,confirm_full_shop_send:enabled,confirmation_text:enabled?(row.full_shop_confirmation_text||shopName):''})});
      toast(enabled?`已开启 ${shopName} 的 AI 回复`:`已停止 ${shopName} 的 AI 回复`);
      window.dispatchEvent(new CustomEvent('ai-reply-state-changed'));
      await capOpenAiReplySetup();
    }catch(error){toast((enabled?'开启失败：':'停止失败：')+error.message);}
    finally{capButtonBusy(button,false);}
  }
  function capOpenShopBrain(shopId){
    const row=capState.managedShops.find(item=>item.shop_id===shopId);if(!row)return toast('\u5e97\u94fa\u4e0d\u5b58\u5728');
    const brain=row.brain_mode==='own'?'own':'tanyu',policy=brain==='own'?(row.send_policy||'blocked'):'blocked';
    capModal(`\u5927\u8111\u4e0e\u53d1\u9001 \u00b7 ${e(capShopName(row))}`,`<form id="capShopBrainForm" class="cap-form" onsubmit="return false" data-shop-id="${e(row.shop_id)}" data-shop-name="${e(capShopName(row))}"><label class="cap-field"><span>\u56de\u590d\u5927\u8111</span><select class="cap-select" id="capShopBrainMode" name="brain_mode"><option value="tanyu" ${brain==='tanyu'?'selected':''}>\u63a2\u57df\u5927\u8111\uff08\u6211\u65b9\u53ea\u505a\u5f71\u5b50\u9a8c\u8bc1\uff09</option><option value="own" ${brain==='own'?'selected':''}>\u6211\u7684\u5927\u8111\uff08\u63a2\u57df\u53ea\u76d1\u542c\u548c\u53d1\u9001\uff09</option></select></label><label class="cap-field"><span>\u6211\u65b9\u53d1\u9001\u6743\u9650</span><select class="cap-select" id="capShopSendPolicy" name="send_policy"><option value="blocked" ${policy==='blocked'?'selected':''}>\u7981\u6b62\u53d1\u9001</option><option value="whitelist" ${policy==='whitelist'?'selected':''}>\u4ec5\u767d\u540d\u5355\u4e70\u5bb6</option><option value="shop" ${policy==='shop'?'selected':''}>\u5168\u5e97\u5141\u8bb8\u53d1\u9001</option></select></label><label class="cap-field cap-brain-whitelist"><span>\u767d\u540d\u5355\u4e70\u5bb6 ID\uff08\u6bcf\u884c\u4e00\u4e2a\uff09</span><textarea class="cap-textarea" name="allowed_buyer_ids" rows="4">${e((row.allowed_buyer_ids||[]).join('\n'))}</textarea></label><div class="cap-notice">\u9009\u62e9\u201c\u6211\u7684\u5927\u8111\u201d\u540e\uff0c\u4ecd\u5fc5\u987b\u5148\u5728\u63a2\u57df\u5173\u95ed\u8be5\u5e97\u94fa\u7684\u6258\u7ba1\uff0c\u5426\u5219\u53ef\u80fd\u53d1\u751f\u91cd\u590d\u56de\u590d\u3002</div><label class="cap-check cap-brain-confirm"><input id="capShopHostingOff" name="confirm_tanyu_hosting_off" type="checkbox"> \u6211\u5df2\u786e\u8ba4\u8be5\u5e97\u94fa\u7684\u63a2\u57df\u6258\u7ba1\u5df2\u5173\u95ed</label><div class="cap-brain-full"><label class="cap-check"><input id="capShopFullConfirm" name="confirm_full_shop_send" type="checkbox"> \u6211\u77e5\u9053\u8fd9\u4f1a\u5141\u8bb8\u6211\u65b9\u5bf9\u672c\u5e97\u6240\u6709\u4e70\u5bb6\u81ea\u52a8\u53d1\u9001</label><label class="cap-field"><span>\u8bf7\u8f93\u5165\u5e97\u94fa\u540d\u79f0\u786e\u8ba4</span><input class="cap-input" id="capShopFullText" name="confirmation_text" autocomplete="off" placeholder="${e(capShopName(row))}"></label></div><div class="cap-notice warn">\u53d1\u9001\u6388\u6743\u6bcf\u6b21\u540e\u7aef\u91cd\u542f\u540e\u90fd\u4f1a\u5931\u6548\uff0c\u9700\u91cd\u65b0\u786e\u8ba4\u63a2\u57df\u6258\u7ba1\u5df2\u5173\u95ed\u3002</div></form>`,`<button class="cap-btn" id="capBackBrainSettings" type="button">\u8fd4\u56de</button><button class="cap-btn primary" id="capShopBrainSave" type="button">\u4fdd\u5b58\u8bbe\u7f6e</button>`);
    capSyncBrainForm();
  }
  function capSyncBrainForm(){
    const brain=$('capShopBrainMode'),policy=$('capShopSendPolicy');if(!brain||!policy)return;
    const own=brain.value==='own';if(!own)policy.value='blocked';policy.disabled=!own;
    document.querySelectorAll('.cap-brain-whitelist').forEach(el=>{el.hidden=!own||policy.value!=='whitelist';});
    document.querySelectorAll('.cap-brain-confirm').forEach(el=>{el.hidden=!own||policy.value==='blocked';});
    document.querySelectorAll('.cap-brain-full').forEach(el=>{el.hidden=!own||policy.value!=='shop';});
  }
  async function capSaveShopBrain(button){
    const form=$('capShopBrainForm'),fd=new FormData(form),shopId=form.dataset.shopId,shopName=form.dataset.shopName;
    const brainMode=String(fd.get('brain_mode')||'tanyu'),sendPolicy=brainMode==='own'?String(fd.get('send_policy')||'blocked'):'blocked';
    const payload={brain_mode:brainMode,send_policy:sendPolicy,allowed_buyer_ids:splitList(fd.get('allowed_buyer_ids')),confirm_tanyu_hosting_off:fd.get('confirm_tanyu_hosting_off')==='on',confirm_full_shop_send:fd.get('confirm_full_shop_send')==='on',confirmation_text:String(fd.get('confirmation_text')||'')};
    if(brainMode==='own'&&sendPolicy==='whitelist'&&!payload.allowed_buyer_ids.length)return toast('\u8bf7\u586b\u5199\u767d\u540d\u5355\u4e70\u5bb6 ID');
    if(brainMode==='own'&&sendPolicy!=='blocked'&&!payload.confirm_tanyu_hosting_off)return toast('\u8bf7\u5148\u786e\u8ba4\u63a2\u57df\u6258\u7ba1\u5df2\u5173\u95ed');
    if(sendPolicy==='shop'&&(!payload.confirm_full_shop_send||payload.confirmation_text!==shopName))return toast('\u5168\u5e97\u53d1\u9001\u9700\u52fe\u9009\u98ce\u9669\u786e\u8ba4\u5e76\u8f93\u5165\u5b8c\u6574\u5e97\u94fa\u540d');
    capButtonBusy(button,true,'\u4fdd\u5b58\u4e2d\u2026');
    try{await fetchJSON(`${CAP_BASE}/shop-management/shops/${encodeURIComponent(shopId)}/brain`,{method:'PATCH',body:JSON.stringify(payload)});toast('\u5e97\u94fa\u5927\u8111\u4e0e\u53d1\u9001\u6743\u9650\u5df2\u66f4\u65b0');window.dispatchEvent(new CustomEvent('ai-reply-state-changed'));await (capShopBrainReturn==='ai-reply'?capOpenAiReplySetup():capOpenShopManager());}catch(error){toast('\u4fdd\u5b58\u5931\u8d25\uff1a'+error.message);}finally{capButtonBusy(button,false);}
  }
  function capShopCloneFields(mode){
    const hidden=mode==='clone'?'':' hidden';
    const sources=capState.managedShops.filter(row=>row.status==='active'&&row.shop_id!==($('capShopId')?.value||''));
    return `<div class="cap-shop-clone-only"${hidden}><label class="cap-field"><span>复制来源店铺</span><select class="cap-select" name="source_shop_id"><option value="">请选择运行中店铺</option>${sources.map(row=>`<option value="${e(row.shop_id)}">${e(capShopName(row))}（${e(row.shop_id)}）</option>`).join('')}</select></label><span class="cap-field-label">复制内容</span><div class="cap-shop-copy-grid">${[['settings','模型配置'],['prompts','提示词'],['routing','接待分工'],['workflows','售后规则'],['scripts','话术'],['tools','工具配置和品类']].map(([value,label])=>`<label><input type="checkbox" name="components" value="${value}" checked> ${label}</label>`).join('')}</div><div class="cap-notice warn" style="margin-top:8px">复制会创建独立副本，之后源店铺与目标店铺互不影响。工具开关会按来源复制，请接入后再次确认。</div></div>`;
  }
  function capShopProfileForm(row={}, editing=false){
    return `<form id="${editing?'capShopEditForm':'capShopOnboardForm'}" class="cap-form" onsubmit="return false"><div class="cap-grid"><label class="cap-field"><span>店铺 ID</span><input class="cap-input" id="capShopId" name="shop_id" value="${e(row.shop_id||'')}" ${row.shop_id?'readonly':''} placeholder="必须唯一，不能与其他店铺重复"></label><label class="cap-field"><span>店铺名称</span><input class="cap-input" name="shop_name" value="${e(row.shop_name===row.shop_id?'':row.shop_name||'')}" placeholder="例如 VHE远见专卖店"></label></div><label class="cap-field"><span>客服账号（每行一个）</span><textarea class="cap-textarea" name="accounts" rows="3" placeholder="探域客服账号或消息中的 account">${e((row.accounts||[]).join('\n'))}</textarea></label>${editing?'':`<label class="cap-field"><span>初始化方式</span><select class="cap-select" id="capShopInitMode" name="init_mode"><option value="blank">空白店铺（适合不同品类）</option><option value="template_mobile_wifi">移动 WiFi 模板（工具默认关闭）</option><option value="clone">复制已有店铺</option></select></label>${capShopCloneFields('blank')}`}</form>`;
  }
  function capOpenShopOnboard(shopId=''){
    const row=capState.managedShops.find(item=>item.shop_id===shopId)||{};
    capModal(row.shop_id?'接入店铺':'新增店铺',capShopProfileForm(row,false),`<button class="cap-btn" id="capBackShopManager" type="button">返回</button><button class="cap-btn primary" id="capShopOnboardSave" type="button">确认接入</button>`);
  }
  function capOpenShopEdit(shopId){
    const row=capState.managedShops.find(item=>item.shop_id===shopId);if(!row)return toast('店铺不存在');
    capModal(`修改店铺资料 · ${e(capShopName(row))}`,capShopProfileForm(row,true),`<button class="cap-btn" id="capBackShopManager" type="button">返回</button><button class="cap-btn primary" id="capShopEditSave" type="button">保存资料</button>`);
  }
  async function capSaveShopOnboard(button){
    const form=$('capShopOnboardForm'),fd=new FormData(form);const shopId=String(fd.get('shop_id')||'').trim(),shopName=String(fd.get('shop_name')||'').trim();
    if(!shopId||!shopName)return toast('请填写店铺 ID 和名称');
    const initMode=String(fd.get('init_mode')||'blank'),sourceShopId=String(fd.get('source_shop_id')||'');
    if(initMode==='clone'&&!sourceShopId)return toast('请选择复制来源店铺');
    const payload={shop_id:shopId,shop_name:shopName,accounts:splitList(fd.get('accounts')),init_mode:initMode,source_shop_id:sourceShopId,components:fd.getAll('components')};
    capButtonBusy(button,true,'接入中…');
    try{
      await fetchJSON(`${CAP_BASE}/shop-management/shops`,{method:'POST',body:JSON.stringify(payload)});
      capState.shops=[];await capLoadShopOptions(true);capState.shopId=shopId;capState.manualShop=true;sessionStorage.setItem('capabilityShopId',shopId);capRenderShopSelector();capCloseModal();toast('店铺已接入，默认不加入真实发送白名单');await capLoad(capState.module);
    }catch(error){toast('接入失败：'+error.message);}finally{capButtonBusy(button,false);}
  }
  async function capSaveShopEdit(button){
    const form=$('capShopEditForm'),fd=new FormData(form);const shopId=String(fd.get('shop_id')||'').trim(),shopName=String(fd.get('shop_name')||'').trim();
    if(!shopId||!shopName)return toast('请填写店铺 ID 和名称');
    const payload={shop_name:shopName,accounts:splitList(fd.get('accounts'))};
    capButtonBusy(button,true,'保存中…');
    try{
      await fetchJSON(`${CAP_BASE}/shop-management/shops/${encodeURIComponent(shopId)}`,{method:'PATCH',body:JSON.stringify(payload)});
      capState.shops=[];await capLoadShopOptions(true);capRenderShopSelector();toast('店铺资料已保存');await capOpenShopManager();
    }catch(error){toast('保存失败：'+error.message);}finally{capButtonBusy(button,false);}
  }
  async function capSetShopStatus(shopId,status){
    const row=capState.managedShops.find(item=>item.shop_id===shopId),label=status==='active'?'恢复':status==='disabled'?'停用':'归档';
    const warning=status==='active'?'恢复后仅允许本地模型处理；不会自动加入真实发送白名单。':'本地模型将停止处理该店铺的新消息，配置和历史数据会保留。';
    if(!confirm(`${label}店铺“${capShopName(row)}”？
${warning}`))return;
    await fetchJSON(`${CAP_BASE}/shop-management/shops/${encodeURIComponent(shopId)}`,{method:'PATCH',body:JSON.stringify({status})});
    if(shopId===capState.shopId&&status!=='active'){capState.shopId='';capState.manualShop=false;sessionStorage.removeItem('capabilityShopId');}
    capState.shops=[];await capLoadShopOptions(true);await capEnsureShop(true);toast(`店铺已${label}`);await capOpenShopManager();
  }

  async function capHandleClick(event){
    const target=event.target.closest('button,[data-cap-module]');if(!target)return;
    if(target.matches('[data-cap-close]'))return capCloseModal();
    if(target.dataset.capModule)return capLoad(target.dataset.capModule);
    if(target.id==='capManageShops'){capState.shopManagerView='onboarded';return capOpenShopManager(false);}
    if(target.id==='capAiAdvancedShopManager'){capState.shopManagerView='onboarded';return capOpenShopManager(false);}
    if(target.dataset.shopManagerView){capState.shopManagerView=target.dataset.shopManagerView;return capOpenShopManager(false);}
    if(target.id==='capShopDiscover')return capOpenShopManager(true, target);
    if(target.id==='capShopManualAdd')return capOpenShopOnboard();
    if(target.id==='capBackShopManager')return capOpenShopManager(false);
    if(target.id==='capBackBrainSettings')return capShopBrainReturn==='ai-reply'?capOpenAiReplySetup():capOpenShopManager(false);
    if(target.id==='capShopOnboardSave')return capSaveShopOnboard(target);
    if(target.id==='capShopEditSave')return capSaveShopEdit(target);
    if(target.id==='capShopBrainSave')return capSaveShopBrain(target);
    if(target.dataset.shopOnboard)return capOpenShopOnboard(target.dataset.shopOnboard);
    if(target.dataset.shopEdit)return capOpenShopEdit(target.dataset.shopEdit);
    if(target.dataset.shopBrain){capShopBrainReturn=target.dataset.brainReturn||'manager';return capOpenShopBrain(target.dataset.shopBrain);}
    if(target.dataset.aiReplyToggle)return capToggleAiReply(target);
    if(target.dataset.shopStatus)return capSetShopStatus(target.dataset.shopId,target.dataset.shopStatus);
    if(target.id==='capRefreshModule')return capLoad(capState.module);
    if(target.id==='capGrowthAdd')return capOpenGrowthProposal();
    if(target.id==='capGrowthProposalSave')return capSaveGrowthProposal(target);
    if(target.dataset.growthPropose)return capOpenGrowthProposal(capState.growthClusters.find(row=>row.cluster_key===target.dataset.growthPropose));
    if(target.dataset.growthDelete)return capDeleteGrowthCluster(target.dataset.growthDelete);
    if(target.dataset.growthPrepare)return capPrepareGrowthDeployment(target.dataset.growthPrepare);
    if(target.dataset.growthDeployment)return capOpenGrowthDeployment(capState.growthProposals.find(row=>row.id===target.dataset.growthDeployment));
    if(target.id==='capGrowthDeploymentSave')return capSaveGrowthDeployment(target,false);
    if(target.id==='capGrowthDeploymentPublish')return capSaveGrowthDeployment(target,true);
    if(target.dataset.growthOpenModule){capCloseModal();return capLoad(target.dataset.growthOpenModule);}
    if(target.dataset.growthEdit)return capOpenGrowthProposal(null,capState.growthProposals.find(row=>row.id===target.dataset.growthEdit));
    if(target.dataset.growthStatus)return capTransitionGrowthProposal(target.dataset.growthStatus,target.dataset.status);
    if(target.id==='capMsgApply'){if(!capReadMessageFilters())return;return capLoadMessages(1);}
    if(target.id==='capMsgReset'){capState.messageFilters={shop_id:'',search:'',roles:[...capMessageRoleValues],whitebox:'all',from:'',to:''};return capLoadMessages(1);}
    if(target.id==='capMsgPrev')return capLoadMessages(capState.messagePage-1);
    if(target.id==='capMsgNext')return capLoadMessages(capState.messagePage+1);
    if(target.dataset.conversationOpen)return capOpenConversation(target.dataset.account,target.dataset.buyer);
    if(target.id==='capConversationOlder')return capLoadOlderConversation(target);
    if(target.dataset.conversationMessage)return capInspectConversationMessage(target.dataset.conversationMessage);
    if(target.dataset.messageDetail)return capOpenMessageDetail(target.dataset.messageDetail);
    if(target.dataset.messageExport)return capDownloadConversation(target.dataset.account,target.dataset.buyer,target);
    if(target.dataset.messageCorrectionSubmit)return capSubmitMessageCorrection(target);
    if(target.id==='capScriptAdd')return capOpenScriptEditor();
    if(target.id==='capScriptImport')return capImportScriptsFromFile(target);
    if(target.id==='capScriptTemplate'){const a=document.createElement('a');a.href=`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/import-template`;a.download='固定回复导入模板.csv';a.click();return;}
    if(target.id==='capScriptImportConfirm')return capConfirmScriptImport(target);
    if(target.id==='capScriptSave')return capSaveScript(target);
    if(target.id==='capScriptSearchBtn')return capLoadScripts($('capScriptSearch')?.value||'',0);
    if(target.id==='capScriptSearchClear')return capLoadScripts('',0);
    if(target.id==='capScriptPrev')return capLoadScripts(capState.scriptSearch,capState.scriptPage-1);
    if(target.id==='capScriptNext')return capLoadScripts(capState.scriptSearch,capState.scriptPage+1);
    if(target.id==='capScriptSemanticRebuild'){capButtonBusy(target,true,'更新中…');try{const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/rebuild-semantic-index`,{method:'POST',body:'{}'}),result=data.result||{};toast(result.enabled?`已更新 ${result.indexed||0} 个问法`:'尚未配置向量模型，仍会使用 AI 语义判断');await capLoadScripts();}catch(error){toast('更新失败：'+error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capScriptExport'){const response=await fetch(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/export`);if(!response.ok)return toast('导出失败');const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`${capState.shopId}-scripts.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);return;}
    if(target.dataset.scriptEdit)return capOpenScriptEditor(capState.scripts.find(x=>x.id===target.dataset.scriptEdit));
    if(target.dataset.scriptToggle){await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/${encodeURIComponent(target.dataset.scriptToggle)}/enabled`,{method:'PATCH',body:JSON.stringify({enabled:target.dataset.enabled!=='1'})});toast('话术状态已更新');return capLoadScripts();}
    if(target.dataset.scriptCopy){await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/${encodeURIComponent(target.dataset.scriptCopy)}/duplicate`,{method:'POST',body:'{}'});toast('已创建副本');return capLoadScripts();}
    if(target.dataset.scriptDelete){if(!confirm('确定删除这条话术？'))return;await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/${encodeURIComponent(target.dataset.scriptDelete)}`,{method:'DELETE',body:'{}'});toast('话术已删除');return capLoadScripts();}
    if(target.id==='capScriptTest'){const q=$('capScriptQuestion').value.trim();if(!q)return toast('请输入客户问题');capButtonBusy(target,true,'判断中…');try{const d=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/scripts/test`,{method:'POST',body:JSON.stringify({question:q,product_id:$('capScriptProduct').value.trim(),lifecycle:$('capScriptLifecycle').value,top_k:5})});$('capScriptResult').innerHTML=capRenderScriptTest(d);}catch(error){toast(error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capKbUploadBtn')return $('capKbFile').click();
    if(target.id==='capKbSyncProducts'){if(!confirm('将「店铺在售」商品写入知识库？\n默认只同步店铺抓取/导入的在售商品，不含探域聊天历史里的旧商品卡。\n会覆盖上次商品库同步的文档，不影响你手动上传的其它资料。'))return;capButtonBusy(target,true,'同步中…');try{const d=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/knowledge/sync-from-products`,{method:'POST',body:JSON.stringify({replace:true,only_with_title:true,only_onsale:true})});toast(`已同步在售 ${d.product_indexed||0} 个（跳过历史卡 ${d.product_skipped_history||0}）`);await capLoadKnowledge();}catch(error){toast('同步失败：'+error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capKbRebuild'){if(!confirm('确定根据当前源文件重建本地知识索引？'))return;capButtonBusy(target,true,'重建中…');try{await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/knowledge/rebuild`,{method:'POST',body:'{}'});toast('知识索引已重建');await capLoadKnowledge();}catch(error){toast('重建失败：'+error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capKbQuery'){const q=$('capKbQuestion').value.trim();if(!q)return toast('请输入检索问题');capButtonBusy(target,true,'检索中…');try{const d=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/knowledge/query`,{method:'POST',body:JSON.stringify({question:q,product_id:$('capKbProduct').value.trim(),top_k:5})});$('capKbResult').innerHTML=`<div class="cap-result"><pre>${e(jsonText(d.hits||[]))}</pre></div>`;}catch(error){toast(error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capModelSave'){let payload;try{payload=capModelPayload();}catch(error){return toast(error.message);}capButtonBusy(target,true,'保存中…');try{await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/models`,{method:'PUT',body:JSON.stringify(payload)});toast('模型配置已保存');await capLoadModels();}catch(error){toast('保存失败：'+error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capModelTest'){capButtonBusy(target,true,'测试中…');try{const d=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/models/test`,{method:'POST',body:JSON.stringify({message:'只回复：配置正常'})});$('capModelResult').innerHTML=`<div class="cap-result"><pre>${e(jsonText(d))}</pre></div>`;toast('模型连接正常');}catch(error){toast('连接失败：'+error.message);$('capModelResult').innerHTML=`<div class="cap-result"><pre>${e(error.message)}</pre></div>`;}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capPromptSave'){capButtonBusy(target,true,'保存中…');try{await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/prompts/${encodeURIComponent(capState.promptName)}`,{method:'PUT',body:JSON.stringify({content:$('capPromptContent').value})});toast('提示词已保存');await capLoadPrompts();}catch(error){toast('保存失败：'+error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capPaymentReminderSave'){
      const form=$('capPaymentReminderForm'),fd=new FormData(form),enabled=fd.get('enabled')==='on',sendCount=Number(fd.get('send_count')||1),reminders=[1,2].map(index=>({delay_seconds:Number(fd.get(`delay_seconds_${index}`)||0),message:String(fd.get(`message_${index}`)||'').trim(),format:'text'}));
      for(let index=0;index<2;index++){const row=reminders[index];if(!Number.isInteger(row.delay_seconds)||row.delay_seconds<1||row.delay_seconds>86400)return toast(`第 ${index+1} 次等待时长应在 1 到 86400 秒之间`);if(!row.message)return toast(`请填写第 ${index+1} 次催付话术`);}
      if(enabled&&!capState.paymentReminder.enabled&&!confirm('启用后，符合条件的未下单咨询会自动向买家发送这段话术。确认启用？'))return;
      capButtonBusy(target,true,'保存中…');try{await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/payment-reminder`,{method:'PUT',body:JSON.stringify({enabled,send_count:sendCount,reminders})});toast('催付设置已保存');await capLoadPaymentReminder();}catch(error){toast('保存失败：'+error.message);}finally{capButtonBusy(target,false);}return;
    }
    if(target.id==='capRoutingSettingsSave'){capButtonBusy(target,true,'保存中…');try{await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/routing/settings`,{method:'PUT',body:JSON.stringify({enabled:$('capRoutingEnabled').checked,shadow_enforce:$('capRoutingEnforce').checked})});toast('开关已保存');await capLoadRouting();}catch(error){toast(error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capRoutingBootstrap'){if(!confirm('用系统推荐规则覆盖当前规则？你自己改过的规则会被替换。'))return;await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/routing/bootstrap`,{method:'POST',body:JSON.stringify({replace:true})});toast('已恢复系统推荐规则');return capLoadRouting();}
    if(target.id==='capRoutingAdd')return capOpenRoutingEditor();
    if(target.id==='capRoutingRuleSave')return capSaveRoutingRule(target);
    if(target.dataset.routingEdit)return capOpenRoutingEditor(capState.routingRules.find(x=>x.id===target.dataset.routingEdit));
    if(target.dataset.routingToggle){await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/routing/rules/${encodeURIComponent(target.dataset.routingToggle)}/enabled`,{method:'PATCH',body:JSON.stringify({enabled:target.dataset.enabled!=='1'})});toast(target.dataset.enabled==='1'?'已关掉这条规则':'已打开这条规则');return capLoadRouting();}
    if(target.dataset.routingDelete){if(!confirm('确定删除这条规则？'))return;await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/routing/rules/${encodeURIComponent(target.dataset.routingDelete)}`,{method:'DELETE',body:'{}'});toast('规则已删除');return capLoadRouting();}
    if(target.id==='capRoutingTest'){const message=$('capRoutingMessage').value.trim();if(!message)return toast('请先写一句买家说的话');capButtonBusy(target,true,'判断中…');try{const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/routing/test`,{method:'POST',body:JSON.stringify({message,facts:capRoutingFacts()})});$('capRoutingResult').innerHTML=capRenderRoutingDecision(data.decision||{});}catch(error){toast(error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capFhSave'){
      if($('capFhEnforce').checked && !$('capFhObserve').checked && !confirm('将开启「真转人工」：命中后会话会 handoff，AI 自动回复停止。确定保存？'))return;
      capButtonBusy(target,true,'保存中…');
      try{
        await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/force-handoff`,{method:'PUT',body:JSON.stringify(capFhCollectPayload())});
        toast('强制转人工策略已保存');
        await capLoadForceHandoff();
      }catch(error){toast('保存失败：'+error.message);}
      finally{capButtonBusy(target,false);}
      return;
    }
    if(target.id==='capFhReset'){
      if(!confirm('恢复该店铺强制转人工默认策略？（仅观察、默认词表）'))return;
      capButtonBusy(target,true,'重置中…');
      try{
        await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/force-handoff/reset`,{method:'POST',body:'{}'});
        toast('已恢复默认');
        await capLoadForceHandoff();
      }catch(error){toast(error.message);}
      finally{capButtonBusy(target,false);}
      return;
    }
    if(target.id==='capFhTest'){
      const message=$('capFhMessage').value.trim();
      if(!message)return toast('请输入买家消息');
      const intentName=$('capFhIntent').value.trim();
      const payload={
        message,
        confidence: Number($('capFhIntentConf').value||0.9),
        unknown_streak: Number($('capFhUnknownStreak').value||0),
      };
      if(intentName)payload.intent={intent:intentName,label:capRouteIntentText(intentName),confidence:payload.confidence};
      capButtonBusy(target,true,'测试中…');
      try{
        const data=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/force-handoff/test`,{method:'POST',body:JSON.stringify(payload)});
        $('capFhResult').innerHTML=capRenderForceHandoffResult(data.result||data);
      }catch(error){toast(error.message);}
      finally{capButtonBusy(target,false);}
      return;
    }
    if(target.id==='capTokenAdd')return capOpenTokenCreate();
    if(target.id==='capTokenCreateSave')return capCreateToken(target);
    if(target.id==='capTokenCopy'){
      const value=$('capCreatedToken')?.value||'';const copied=await capCopyText(value);return toast(copied?'令牌已复制':'复制失败，请手动复制');
    }
    if(target.dataset.tokenCopyValue){
      const copied=await capCopyText(target.dataset.tokenCopyValue);return toast(copied?'令牌已复制':'复制失败，请手动复制');
    }
    if(target.dataset.tokenEdit)return capOpenTokenEdit(capState.bridgeTokens.find(row=>row.id===target.dataset.tokenEdit));
    if(target.id==='capTokenEditSave'){
      const label=$('capTokenEditLabel')?.value.trim();if(!label)return toast('请填写令牌备注');capButtonBusy(target,true,'保存中…');
      try{await fetchJSON(`/api/auth/bridge-tokens/${encodeURIComponent(target.dataset.tokenId)}`,{method:'PATCH',body:JSON.stringify({label})});capCloseModal();toast('令牌备注已更新');return capLoadBridgeTokens();}finally{capButtonBusy(target,false);}
    }
    if(target.dataset.tokenUnbind){
      const row=capState.bridgeTokens.find(item=>item.id===target.dataset.tokenUnbind);if(!confirm(`解绑“${row?.label||'该令牌'}”当前电脑？下一台启动客户端的电脑将获得绑定。`))return;
      await fetchJSON(`/api/auth/bridge-tokens/${encodeURIComponent(target.dataset.tokenUnbind)}/unbind`,{method:'POST',body:'{}'});toast('设备绑定已解除');return capLoadBridgeTokens();
    }
    if(target.dataset.tokenDelete){
      const row=capState.bridgeTokens.find(item=>item.id===target.dataset.tokenDelete);if(!confirm(`撤销“${row?.label||'该令牌'}”？使用它的客户端将无法继续连接。`))return;
      await fetchJSON(`/api/auth/bridge-tokens/${encodeURIComponent(target.dataset.tokenDelete)}`,{method:'DELETE',body:'{}'});toast('令牌已撤销');return capLoadBridgeTokens();
    }
    if(target.id==='capUserAdd'){capState.usersEditing=null;return capOpenUserEditor();}
    if(target.id==='capUserSave')return capSaveUser(target);
    if(target.dataset.userEdit){
      const name=target.dataset.userEdit;
      const data=await fetchJSON('/api/auth/users');
      const row=(data.users||[]).find(u=>u.username===name);
      capState.usersEditing=name;
      return capOpenUserEditor(row||{username:name});
    }
    if(target.dataset.userDelete){
      if(!confirm(`确定删除用户 ${target.dataset.userDelete}？`))return;
      await fetchJSON(`/api/auth/users/${encodeURIComponent(target.dataset.userDelete)}`,{method:'DELETE',body:'{}'});
      toast('用户已删除');return capLoadUsers();
    }
    if(target.id==='capWorkflowAdd')return capOpenWorkflowEditor();
    if(target.dataset.wfListAdd){const kind=target.dataset.wfListAdd,container=kind==='entry'?$('capWfEntryRows'):$('capWfEscalationRows');if(container){container.insertAdjacentHTML('beforeend',capWfListRow(kind));container.lastElementChild?.querySelector('[data-wf-list-value]')?.focus();}return;}
    if(target.hasAttribute('data-wf-list-remove')){const row=target.closest('[data-wf-list-row]'),container=row?.parentElement,kind=row?.dataset.wfListRow;row?.remove();if(container&&!container.children.length)container.insertAdjacentHTML('beforeend',capWfListRow(kind));return;}
    if(target.id==='capWfStepAdd'){const container=$('capWfStepRows');if(container){container.insertAdjacentHTML('beforeend',capWfStepRow());capWfRefreshStepOrder();container.lastElementChild?.querySelector('[data-wf-step-name]')?.focus();}return;}
    if(target.hasAttribute('data-wf-step-remove')){target.closest('[data-wf-step-row]')?.remove();capWfRefreshStepOrder();return;}
    if(target.dataset.wfStepMove){const row=target.closest('[data-wf-step-row]'),direction=Number(target.dataset.wfStepMove),sibling=direction<0?row?.previousElementSibling:row?.nextElementSibling;if(row&&sibling){if(direction<0)row.parentElement.insertBefore(row,sibling);else row.parentElement.insertBefore(sibling,row);capWfRefreshStepOrder();}return;}
    if(target.id==='capWorkflowSave')return capSaveWorkflow(target);
    if(target.dataset.workflowEdit)return capOpenWorkflowEditor(capState.workflows.find(x=>x.id===target.dataset.workflowEdit));
    if(target.dataset.workflowDelete){if(!confirm('确定删除这条售后规则？'))return;await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/workflows/${encodeURIComponent(target.dataset.workflowDelete)}`,{method:'DELETE',body:'{}'});toast('规则已删除');return capLoadWorkflows();}
    if(target.id==='capWorkflowExportAll')return capExportWorkflows(target,false);
    if(target.id==='capWorkflowExportSelected')return capExportWorkflows(target,true);
    if(target.id==='capWorkflowImport')return capImportWorkflowsFromFile(target);
    if(target.id==='capWorkflowImportConfirm')return capConfirmWorkflowImport(target);
    if(target.id==='capWfImportAll'){document.querySelectorAll('input[name="capWfImportPick"]').forEach(el=>{el.checked=true;});return;}
    if(target.id==='capWfImportNone'){document.querySelectorAll('input[name="capWfImportPick"]').forEach(el=>{el.checked=false;});return;}
    if(target.id==='capWorkflowSystemPack'){
      capButtonBusy(target,true,'加载中…');
      try{
        const data=await fetchJSON(`${CAP_BASE}/workflows/system-pack`);
        capOpenWorkflowImportDialog(data.pack||data,{fromSystem:true});
      }catch(error){toast(error.message);}
      finally{capButtonBusy(target,false);}
      return;
    }
    if(target.id==='capDiagnoseRun'){const msg=$('capDiagnoseMessage').value.trim();if(!msg)return toast('请先写一句买家的问题');capButtonBusy(target,true,'排查中…');try{const d=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/diagnose`,{method:'POST',body:JSON.stringify({user_message:msg,scenario:$('capDiagnoseScenario').value})});$('capDiagnoseResult').innerHTML=capRenderDiagnoseResult(d.diagnosis||d);}catch(error){toast(error.message);}finally{capButtonBusy(target,false);}return;}
    if(target.id==='capImportPreviewBtn')return capPreviewImport(target);
    if(target.id==='capImportConfirmBtn'){if(!capState.importPayload)return toast('请先预览');if(!confirm('确认导入到本地历史？导入不会触发模拟回复或真实发送。'))return;capButtonBusy(target,true,'导入中…');try{const d=await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/import/chats`,{method:'POST',body:JSON.stringify(capState.importPayload)});$('capImportResult').innerHTML=`<div class="cap-result"><pre>${e(jsonText(d))}</pre></div>`;toast('聊天历史已导入');capButtonBusy(target,false);target.disabled=true;}catch(error){toast('导入失败：'+error.message);capButtonBusy(target,false);}return;}
    if(target.id==='capCorrectionRefresh')return capLoadCorrections();
    if(target.id==='capCorrectionShopAll'){document.querySelectorAll('input[name="capCorrectionShop"]').forEach(input=>{input.checked=true;});return;}
    if(target.id==='capCorrectionShopNone'){document.querySelectorAll('input[name="capCorrectionShop"]').forEach(input=>{input.checked=false;});return;}
    if(target.id==='capCorrectionShopApply'){
      const selected=capCorrectionSelectedShopIds();if(!selected.length)return toast('至少选择一家店铺');
      return capLoadCorrections(capState.correctionStatus,selected.length===capState.shops.length?[]:selected);
    }
    if(target.dataset.correctionEdit)return capOpenCorrectionEditor(capState.corrections.find(row=>row.id===target.dataset.correctionEdit));
    if(target.id==='capCorrectionEditSave')return capSaveCorrection(target);
    if(target.dataset.correctionReview){const shopId=String(target.dataset.correctionShop||'');if(!shopId)return toast('纠正记录缺少店铺信息');const label=target.dataset.status==='approved'?'通过':'拒绝',note=prompt(`${label}这条纠正。可填写审核备注：`,'');if(note===null)return;await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(shopId)}/corrections/${encodeURIComponent(target.dataset.correctionReview)}/review`,{method:'PUT',body:JSON.stringify({status:target.dataset.status,review_note:note})});toast(target.dataset.status==='approved'?'已通过，后续将按整段对话语境用于影子回复':`已${label}`);return capLoadCorrections();}
  }
  async function capHandleChange(event){
    if(event.target.id==='capWfIssueType')return capSyncWorkflowIssueField(true);
    if(event.target.matches('input[name="capConversationRole"]')){
      let roles=Array.from(document.querySelectorAll('input[name="capConversationRole"]:checked')).map(input=>input.value).filter(role=>capMessageRoleValues.includes(role));
      if(!roles.length){event.target.checked=true;toast('买家、客服、模拟 AI 至少勾选一项');return;}
      return capReloadConversationRoles(roles);
    }
    if(event.target.id==='capScriptProductScope'){const field=$('capScriptProductIds');if(field)field.hidden=event.target.value!=='specified';return;}
    if(event.target.id==='capShopInitMode'){document.querySelectorAll('.cap-shop-clone-only').forEach(el=>{el.hidden=event.target.value!=='clone';});return;}
    if(event.target.id==='capShopBrainMode'||event.target.id==='capShopSendPolicy')return capSyncBrainForm();
    if(event.target.id==='capPaymentReminderCount'){const step=$('capPaymentReminderStep2');if(step)step.hidden=event.target.value!=='2';return;}
    if(event.target.id==='capShopSelect'){
      const shopId=event.target.value;if(!shopId||shopId===capState.shopId)return;
      capState.shopId=shopId;capState.manualShop=true;sessionStorage.setItem('capabilityShopId',shopId);
      const selected=capState.shops.find(row=>row.shop_id===shopId);capState.account=selected?.account||'';
      capState.prompts={};capState.promptName='default';capState.importPreview=null;capState.importPayload=null;capState.scriptSearch='';capState.scriptPage=0;capState.growth={};capState.growthClusters=[];capState.growthProposals=[];
      capRenderShopSelector();toast(`已切换到 ${capShopName(selected)}`);await capLoad(capState.module);return;
    }
    if(event.target.id==='capPromptSelect'){capState.prompts[capState.promptName]=$('capPromptContent').value;capState.promptName=event.target.value;$('capPromptContent').value=capState.prompts[capState.promptName]||'';}
    if(event.target.id==='capCorrectionEditScope')return capSyncCorrectionScope();
    if(event.target.id==='capCorrectionStatus')return capLoadCorrections(event.target.value,capState.correctionShopIds);
    if(event.target.id==='capCorrectionServiceStage')return capLoadCorrections(capState.correctionStatus,capState.correctionShopIds,event.target.value);
    if(event.target.id==='capGrowthProposalStatus')await capLoadGrowth(event.target.value);
    if(event.target.id==='capKbFile'){const file=event.target.files?.[0];if(!file)return;if(file.size>20*1024*1024)return toast('文件过大，当前单文件上限 20MB');toast('正在上传并建立本地索引…');try{await fetchJSON(`${CAP_BASE}/shops/${encodeURIComponent(capState.shopId)}/knowledge/upload`,{method:'POST',body:JSON.stringify({filename:file.name,content_base64:await capFileBase64(file)})});toast('知识资料已上传');await capLoadKnowledge();}catch(error){toast('上传失败：'+error.message);}}
  }
  window.openShopBrainSettings=async function(){
    await capOpenSpace('capabilities');
    await capOpenAiReplySetup();
  };
  function initCapabilityCenter(){
    document.querySelectorAll('.mode-tab').forEach(btn=>btn.addEventListener('click',()=>capOpenSpace(btn.dataset.space)));
    $('capabilityPane').addEventListener('click',event=>{capHandleClick(event).catch(error=>toast(error.message));});
    $('capabilityPane').addEventListener('change',event=>{capHandleChange(event).catch(error=>toast(error.message));});
    $('capModal').addEventListener('click',event=>{if(event.target===$('capModal'))capCloseModal();});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&$('capModal').classList.contains('open'))capCloseModal();});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initCapabilityCenter);else initCapabilityCenter();
})();
