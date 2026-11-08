/* Jarvis upgrades. All privileged requests use the existing phone credential.
 * Never put credentials, prompts, or captured images in URLs or browser logs. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const panel = document.createElement('section');
  panel.className = 'security';
  panel.style.cssText = 'text-align:left;line-height:1.7;max-width:800px';
  panel.innerHTML = `
    <h3>Jarvis 控制中心</h3>
    <div id="realHealth">正在检查实际连接状态…</div>
    <div id="modelRoute" style="color:#70e4ff"></div>
    <button class="save" id="refreshHealth">刷新状态</button>
    <button class="save" id="checkCloud">检测云端模型权限（不生成回答）</button>
    <button class="save" id="stopReply">停止回答 / 播报</button>
    <hr><h4>文字与联网问答</h4>
    <textarea id="chatText" rows="2" maxlength="2000" placeholder="可输入问题；需要最新信息时明确说“联网搜索…”" style="width:95%"></textarea>
    <button class="save" id="sendText">发送</button>
    <div id="replySources"></div>
    <hr><h4>项目任务</h4>
    <button class="save" id="refreshTasks">刷新任务列表</button>
    <div id="taskList"></div>
    <hr><h4>电脑桌面控制</h4>
    <p>通过本地 Windows 控件操作白名单应用。先选择窗口并查看控件；点击、输入和勾选会先展示目标，确认后才执行。不会把窗口内容发送给聊天模型。</p>
    <button class="save" id="desktopRefresh">查看电脑窗口</button>
    <select id="desktopWindow" style="width:95%" aria-label="选择电脑窗口"></select>
    <button class="save" id="desktopInspect">查看控件</button>
    <button class="save" id="desktopMinimize">最小化</button>
    <button class="save" id="desktopMaximize">最大化</button>
    <button class="save" id="desktopRestore">还原</button>
    <div id="desktopControls" hidden>
      <select id="desktopControl" style="width:95%" aria-label="选择窗口控件"></select>
      <select id="desktopOperation" aria-label="桌面操作">
        <option value="click">点击</option><option value="set-value">填写文本框</option>
        <option value="check">勾选</option><option value="uncheck">取消勾选</option>
        <option value="expand">展开</option><option value="collapse">收起</option>
        <option value="scroll-up">向上滚动</option><option value="scroll-down">向下滚动</option>
      </select>
      <input id="desktopValue" maxlength="2000" autocomplete="off" placeholder="填写普通文字（不支持密码或密钥）" style="width:90%">
      <button class="save" id="desktopPrepare">预览这项操作</button>
    </div>
    <div id="desktopApproval" hidden>
      <div id="desktopPreview"></div>
      <button class="save" id="desktopConfirm">确认执行一次</button>
      <button class="save" id="desktopCancel">取消</button>
    </div>
    <div id="desktopStatus"></div>
    <hr><h4>电脑窗口分析 · 仅本地</h4>
    <p>只主动截取当前前台应用窗口，不后台监视。会尽力拦截密码、密钥、微信和终端等敏感窗口，但不能识别所有私密内容；请务必检查预览后再确认。请先把电脑切到普通应用窗口。</p>
    <button class="save" id="captureScreen">截取当前窗口并预览</button>
    <div id="screenBox" hidden>
      <img id="screenImage" alt="主动截取的电脑窗口预览" style="max-width:100%;border-radius:12px">
      <input id="screenQuestion" maxlength="500" placeholder="想了解什么？例如：解释报错原因" style="width:90%">
      <button class="save" id="analyzeScreen">确认图片，仅在本地分析</button>
      <button class="save" id="discardScreen">丢弃预览</button>
    </div>
    <div id="screenStatus"></div>
    <hr><h4>明确记忆</h4>
    <p>普通对话仅短期保留；只有在这里明确保存的偏好才会长期记住。请勿保存密码、密钥或私密内容。</p>
    <input id="memoryText" maxlength="300" placeholder="例如：回答尽量简短；常用项目是…" style="width:90%">
    <button class="save" id="saveMemory">保存这条记忆</button>
    <button class="save" id="clearHistory">清空短期对话</button>
    <div id="memoryList"></div><div id="upgradeNotice"></div>`;
  const existing = document.querySelector('.security');
  if (existing) existing.insertAdjacentElement('afterend', panel);
  else document.body.appendChild(panel);

  let activeRequest = null, activeController = null, previewToken = null, previewUrl = null;
  let desktopToken = null, desktopTimer = null, desktopBusy = false, desktopEpoch = 0;
  let previewTimer = null, replyEpoch = 0, speechQueue = [], speechWorking = false, sentenceBuffer = '', currentSpeechUrl = null, displayedRouteTime = 0;
  const notice = text => { el('upgradeNotice').textContent = text; };
  async function api(path, options = {}) {
    const headers = { 'X-Jarvis-Key': apiKey, ...(options.body ? {'Content-Type':'application/json'} : {}), ...options.headers };
    const response = await fetch(path, { cache:'no-store', ...options, headers });
    if (!response.ok) {
      let message = response.status === 401 ? '请先保存正确的 Jarvis 手机密钥' : '请求未完成';
      try { const value = await response.json(); if (typeof value.detail === 'string') message = value.detail; } catch (_) {}
      throw new Error(message);
    }
    return response;
  }
  function route(value, fromHealth = false) {
    if (!value || !value.backend) return;
    const stamp=Date.parse(value.updated_at || '') || 0;
    if(fromHealth && (stamp<=displayedRouteTime || value.reason==='processing'))return;
    displayedRouteTime=stamp || Date.now();
    const label = {none:'尚无对话',unavailable:'暂不可用',local:'本地',openai:'云端',cloud:'云端','local-vision':'本地视觉'}[value.backend] || value.backend;
    const reason={timeout:'连接超时',connection:'连接失败',quota_exhausted:'API额度不足',rate_limited:'请求限流',quota_or_rate_limit:'额度或频率限制',unsupported_region:'地区不受支持',search_unverified:'联网来源未核实',partial_response_failed:'回答中途失败',cancelled:'已停止'}[value.reason];
    el('modelRoute').textContent = `本轮：${label} ${value.model || ''}${value.fallback ? ' · 已自动兜底' : ''}${reason ? ' · '+reason : ''}`;
    el('replySources').replaceChildren();
    for (const source of value.sources || []) {
      const href = typeof source === 'string' ? source : source.url;
      try {
        const url = new URL(href); if (!['http:','https:'].includes(url.protocol)) continue;
        if (url.username || url.password) continue;
        const a = document.createElement('a'); a.href = url.href; a.target = '_blank';
        a.rel = 'noopener noreferrer'; a.textContent = (source.title || url.hostname).slice(0,150);
        a.style.cssText = 'display:block;color:#70e4ff'; el('replySources').appendChild(a);
      } catch (_) {}
    }
  }
  async function health() {
    if (!apiKey) { el('realHealth').textContent = '请先保存 Jarvis 手机密钥'; return; }
    try {
      const value = await (await api('/health/details')).json();
      const labels = {ready:'就绪',connected:'已连接',unavailable:'不可用',recovering:'恢复中',
        unknown:'未确认',stale:'状态已过期',stopped:'已退出',model_missing:'模型未安装',
        configured_unverified:'密钥已配置，尚未验证',model_accessible:'模型权限可访问，回答尚未验证',not_configured:'未配置'};
      const show = x => labels[x] || x || '未确认';
      const reasons={timeout:'连接超时',connection:'网络未连接',authentication_failed:'凭据校验失败',quota_exhausted:'API额度不足',rate_limited:'请求过于频繁',quota_or_rate_limit:'额度或频率限制',unsupported_region:'当前地区不受支持',model_unavailable:'模型不可用',access_forbidden:'访问受限'};
      el('realHealth').textContent = `手机识别：${show(value.phone_voice)}；桌面桥：${show(value.desktop_bridge)}；耳机监听：${show(value.desktop?.microphone)}；本地模型：${show(value.local?.state)}；云端：${show(value.cloud?.state)}${value.cloud?.reason ? '（'+(reasons[value.cloud.reason] || '请求未完成')+'）':''}`;
      route(value.route,true);
    } catch (error) { el('realHealth').textContent = error.message; }
  }
  function stopSpeech() {
    replyEpoch++; speechQueue = []; sentenceBuffer = '';
    const audio = el('voiceReplyAudio'); if (audio) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
    if(currentSpeechUrl)URL.revokeObjectURL(currentSpeechUrl);currentSpeechUrl=null;
    if (window.speechSynthesis) window.speechSynthesis.cancel();
  }
  async function stopReply() {
    stopSpeech();
    if (activeRequest) api('/ai/cancel/' + encodeURIComponent(activeRequest), {method:'POST'}).catch(() => {});
    if (activeController) activeController.abort();
    activeController = null; activeRequest = null;
    el('state').textContent = '已停止当前回答；电脑操作和项目任务不会因此取消';
  }
  async function speakSentence(text, epoch) {
    if (!voiceReplyEnabled || epoch !== replyEpoch) return;
    let url;
    try {
      const response = await api('/speech/dynamic', {method:'POST', body:JSON.stringify({text:text.slice(0,190)})});
      const blob = await response.blob();
      if (epoch !== replyEpoch) return;
      url = URL.createObjectURL(blob);
      if(currentSpeechUrl)URL.revokeObjectURL(currentSpeechUrl);currentSpeechUrl=url;
      const audio = el('voiceReplyAudio'); audio.src = url;
      await new Promise(async resolve => {
        const done = () => { clearTimeout(timer); resolve(); };
        const timer = setTimeout(done, 45000);
        audio.onended = done; audio.onerror = done; audio.onabort = done; audio.onemptied = done;
        try { await audio.play(); } catch (_) { el('voiceReplyStatus').textContent = '手机限制自动播放，请点击播放器播放'; done(); }
      });
    } catch (_) {
      if (epoch === replyEpoch) el('voiceReplyStatus').textContent = '分句语音暂不可用，回答文字仍可查看';
    } finally {
      // Keep a manually playable URL if browser autoplay was denied; replace on the next sentence.
      if (url && el('voiceReplyAudio').src !== url) URL.revokeObjectURL(url);
    }
  }
  async function drainSpeech() {
    if (speechWorking) return;
    speechWorking = true;
    try { while (speechQueue.length) { const item = speechQueue.shift(); await speakSentence(item.text,item.epoch); } }
    finally { speechWorking = false; }
  }
  function feedSpeech(delta, final = false) {
    sentenceBuffer += delta;
    while (sentenceBuffer.length) {
      const match = /[。！？!?；;\n]/.exec(sentenceBuffer);
      let length = match ? match.index + 1 : sentenceBuffer.length >= 120 ? 120 : final ? sentenceBuffer.length : 0;
      if (!length) break;
      const text = sentenceBuffer.slice(0,length).trim(); sentenceBuffer = sentenceBuffer.slice(length);
      if (text && voiceReplyEnabled) speechQueue.push({text,epoch:replyEpoch});
    }
    drainSpeech();
  }
  async function showGuardedQr(value, epoch) {
    const response=await api('/wechat/login-qr/'+encodeURIComponent(value.qr_token));
    const blob=await response.blob();if(epoch!==replyEpoch)return;
    activeQrUrl=URL.createObjectURL(blob);el('wechatQrImage').src=activeQrUrl;
    el('wechatQrSave').href=activeQrUrl;el('wechatQrBox').style.display='block';
    qrExpiryTimer=setTimeout(clearLoginQr,Math.min(60,Math.max(1,value.qr_expires_seconds || 60))*1000);
  }
  async function displayResult(value, streamed = false, epoch = replyEpoch) {
    if(epoch!==replyEpoch)return;
    if (value.transcript) el('transcript').textContent = '你说：' + value.transcript;
    el('reply').textContent = 'Jarvis：' + (value.message || '未生成回答');
    el('state').textContent = value.success ? '✓ 已完成' : value.codex_draft_token ? '等待项目确认' : '本轮未完成';
    route(value.route);
    if (value.qr_token) await showGuardedQr(value,epoch);
    if(epoch!==replyEpoch)return;
    if (value.codex_draft_token) showCodexDraft(value.codex_draft_token,value.codex_projects || []);
    if(value.windows || value.controls || value.desktop_token) {
      desktopEpoch++;clearDesktopApproval();showDesktopResult(value);
    }
    if (!streamed) speakReply(value.speech_text || value.message || '');
  }
  async function consumeResponse(response, epoch) {
    const current = () => { if(epoch !== replyEpoch) throw new DOMException('旧回答已停止','AbortError'); };
    current();
    if (!response.ok) {
      let data = {}; try { data = await response.json(); } catch (_) {}
      throw new Error(response.status === 401 ? '手机密钥不正确' : data.detail || '请求失败');
    }
    if (!(response.headers.get('content-type') || '').includes('application/x-ndjson')) {
      const data=await response.json();current();await displayResult(data,false,epoch);return;
    }
    let buffer = '', full = '', received = false, ownRequest = null;
    const reader = response.body.getReader(), decoder = new TextDecoder();
    const event = async value => {
      current();
      if (value.type === 'start') { ownRequest=value.request_id;activeRequest = ownRequest; if(value.transcript) el('transcript').textContent='你说：'+value.transcript; }
      if (value.type === 'delta') { received = true; full += value.text; el('reply').textContent='Jarvis：'+full; feedSpeech(value.text); }
      if (value.type === 'result') { feedSpeech('',true); await displayResult(value,true,epoch); current();if(!received && value.message) feedSpeech(value.message,true); }
    };
    try {
      while (true) {
        const {value,done} = await reader.read();
        if(done) break;
        buffer += decoder.decode(value,{stream:true});
        let index; while((index=buffer.indexOf('\n'))>=0) {
          const line = buffer.slice(0,index).trim(); buffer=buffer.slice(index+1);
          if(line) await event(JSON.parse(line));
        }
      }
      buffer += decoder.decode(); if(buffer.trim()) await event(JSON.parse(buffer));
      feedSpeech('',true);
    } finally { reader.releaseLock(); if(activeRequest===ownRequest)activeRequest=null; }
  }
  // Preserve existing Skills/WeChat draft handling, but stream only read-only chat.
  window.uploadAudio = async function(blob) {
    await stopReply(); clearLoginQr();
    const form = new FormData(); form.append('audio',blob,'voice.webm');
    const controller = new AbortController(),epoch=replyEpoch; activeController=controller;
    el('state').textContent='正在识别…';
    try {
      const response=await fetch('/voice-command?stream=true',{method:'POST',headers:{'X-Jarvis-Key':apiKey},body:form,signal:controller.signal});
      await consumeResponse(response,epoch);
    } catch(error) { if(epoch===replyEpoch && error.name!=='AbortError'){el('state').textContent='本轮未完成';el('reply').textContent=error.message;} }
    finally {if(activeController===controller)activeController=null;}
  };
  el('sendText').onclick = async () => {
    const text=el('chatText').value.trim(); if(!text)return;
    await stopReply(); const controller=new AbortController(),epoch=replyEpoch;activeController=controller;
    el('state').textContent='正在生成回答…';el('reply').textContent='';
    try { await consumeResponse(await api('/ai/chat/stream',{method:'POST',body:JSON.stringify({text}),signal:controller.signal}),epoch); }
    catch(error){if(epoch===replyEpoch && error.name!=='AbortError')notice(error.message);}
    finally{if(activeController===controller)activeController=null;}
  };
  async function tasks() {
    if(!apiKey)return;
    try {
      const data=await (await api('/codex/tasks')).json();el('taskList').replaceChildren();
      const labels={queued:'排队中',running:'执行中',cancel_requested:'已请求停止',completed:'完成',failed:'失败',cancelled:'已停止',interrupted:'中断，需重新确认'};
      for(const task of data.tasks || []) {
        const row=document.createElement('div');row.style.cssText='border-bottom:1px solid #254050;padding:8px 0';
        const text=document.createElement('div');text.textContent=`${task.project || '项目'} · ${labels[task.status] || task.status} · ${task.stage || ''}\n${task.message || ''}`;row.appendChild(text);
        if(['queued','running','cancel_requested'].includes(task.status)) {
          const button=document.createElement('button');button.className='save';button.textContent='请求停止任务';
          button.onclick=async()=>{if(!confirm('停止任务不会撤销已经完成的文件修改，确认请求停止？'))return;try{await api('/codex/tasks/'+encodeURIComponent(task.task_id || task.id)+'/cancel',{method:'POST'});await tasks();}catch(error){notice(error.message);}};row.appendChild(button);
        }
        el('taskList').appendChild(row);
      }
      if(!data.tasks?.length)el('taskList').textContent='暂无任务记录';
    }catch(error){el('taskList').textContent=error.message;}
  }
  async function memories() {
    if(!apiKey)return;
    try{
      const data=await(await api('/ai/memories')).json();el('memoryList').replaceChildren();
      for(const memory of data.memories || []){
        const row=document.createElement('div'),text=document.createElement('span'),button=document.createElement('button');
        text.textContent=memory.text;button.className='save';button.textContent='删除';
        button.onclick=async()=>{if(!confirm('确认删除这条明确记忆？'))return;try{await api('/ai/memories/'+encodeURIComponent(memory.id),{method:'DELETE'});await memories();}catch(error){notice(error.message);}};
        row.append(text,button);el('memoryList').appendChild(row);
      }
      if(!data.memories?.length)el('memoryList').textContent='暂无长期记忆';
    }catch(error){notice(error.message);}
  }
  async function discard() {
    const token=previewToken;previewToken=null;clearTimeout(previewTimer);
    if(token)api('/screen/preview/'+encodeURIComponent(token),{method:'DELETE'}).catch(()=>{});
    el('screenBox').hidden=true;el('screenImage').removeAttribute('src');
    if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=null;
  }
  function clearDesktopApproval() {
    desktopToken=null;clearTimeout(desktopTimer);el('desktopApproval').hidden=true;
  }
  function clearDesktopControls() {
    el('desktopControls').hidden=true;el('desktopControl').replaceChildren();
    el('desktopValue').value='';
  }
  function showDesktopResult(value) {
    if(value.windows) {
      clearDesktopControls();el('desktopWindow').replaceChildren();
      for(const item of value.windows) {
        const option=document.createElement('option');option.value=item.id;
        option.textContent=`${item.app} · ${item.title}${item.minimized ? '（已最小化）':''}`;
        el('desktopWindow').appendChild(option);
      }
    }
    if(value.controls) {
      el('desktopControl').replaceChildren();
      for(const item of value.controls) {
        const option=document.createElement('option');option.value=item.id;
        option.textContent=`${item.name}（${item.role}）`;el('desktopControl').appendChild(option);
      }
      el('desktopControls').hidden=false;
    }
    if(value.desktop_token && value.preview) {
      clearDesktopApproval();desktopToken=value.desktop_token;
      const p=value.preview;
      const op={click:'点击','set-value':'填写文字',check:'勾选',uncheck:'取消勾选',expand:'展开',collapse:'收起','scroll-up':'向上滚动','scroll-down':'向下滚动'}[p.operation] || '操作';
      el('desktopPreview').textContent=`${op}：${p.window.app} · ${p.window.title} → ${p.control}。请核对目标；45秒内有效。`;
      el('desktopApproval').hidden=false;
      desktopTimer=setTimeout(()=>{clearDesktopApproval();el('desktopStatus').textContent='桌面操作已过期，请重新选择';},45000);
    }
    if(value.local_only && (value.windows || value.controls || value.desktop_token || value.delivery))
      el('desktopStatus').textContent=value.message || '';
  }
  async function desktopRequest(payload) {
    if(desktopBusy)return;
    desktopBusy=true;const epoch=++desktopEpoch;
    clearDesktopApproval();el('desktopStatus').textContent='正在请求电脑桌面…';
    try {
      const value=await(await api('/desktop/control',{method:'POST',body:JSON.stringify(payload)})).json();
      if(epoch!==desktopEpoch)return;
      el('desktopStatus').textContent=value.message || '';showDesktopResult(value);
      if(payload.operation==='confirm' || payload.operation==='cancel' || ['minimize','maximize','restore'].includes(payload.operation))clearDesktopControls();
    } catch(error) { if(epoch===desktopEpoch)el('desktopStatus').textContent=error.message; }
    finally { desktopBusy=false; }
  }
  el('desktopRefresh').onclick=()=>desktopRequest({operation:'windows'});
  el('desktopInspect').onclick=()=>desktopRequest({operation:'inspect',window_id:el('desktopWindow').value});
  for(const [id,operation] of [['desktopMinimize','minimize'],['desktopMaximize','maximize'],['desktopRestore','restore']])
    el(id).onclick=()=>desktopRequest({operation,window_id:el('desktopWindow').value});
  el('desktopPrepare').onclick=()=>desktopRequest({operation:el('desktopOperation').value,
    window_id:el('desktopWindow').value,control_id:el('desktopControl').value,
    value:el('desktopOperation').value==='set-value' ? el('desktopValue').value:''});
  el('desktopConfirm').onclick=()=>{const token=desktopToken;if(token)return desktopRequest({operation:'confirm',token});};
  el('desktopCancel').onclick=()=>desktopRequest({operation:'cancel'});
  el('desktopWindow').onchange=()=>{desktopEpoch++;clearDesktopApproval();clearDesktopControls();};
  for(const id of ['desktopControl','desktopOperation'])el(id).onchange=()=>{desktopEpoch++;clearDesktopApproval();};
  el('desktopValue').oninput=()=>{desktopEpoch++;clearDesktopApproval();};
  el('captureScreen').onclick=async()=>{
    if(!confirm('确认截取电脑当前前台窗口并在本手机预览？截图不会发送云端。'))return;
    await discard();el('screenStatus').textContent='正在请求桌面窗口预览…';
    try{
      const data=await(await api('/screen/preview',{method:'POST',body:JSON.stringify({confirmed:true})})).json();
      previewToken=data.token;
      const png=await(await api('/screen/preview/'+encodeURIComponent(previewToken))).blob();
      previewUrl=URL.createObjectURL(png);el('screenImage').src=previewUrl;el('screenBox').hidden=false;
      el('screenStatus').textContent='请检查图片，再决定是否本地分析。45 秒后自动清除。';
      previewTimer=setTimeout(()=>{discard();el('screenStatus').textContent='预览已过期';},45000);
    }catch(error){await discard();el('screenStatus').textContent=error.message;}
  };
  el('analyzeScreen').onclick=async()=>{
    if(!previewToken)return;
    await stopReply();const epoch=replyEpoch;
    const token=previewToken;previewToken=null;clearTimeout(previewTimer);
    el('analyzeScreen').disabled=true;el('screenStatus').textContent='仅在电脑本地分析中…';
    try{
      const data=await(await api('/screen/analyze',{method:'POST',body:JSON.stringify({confirmed:true,token,question:el('screenQuestion').value.trim() || '请解释当前窗口，若有报错请说明原因和安全的下一步。'})})).json();
      if(epoch===replyEpoch){el('reply').textContent='Jarvis：'+data.message;el('screenStatus').textContent='本地分析完成，截图未上传云端';route({backend:'local-vision',updated_at:new Date().toISOString(),sources:[]});speakReply(data.message.slice(0,180));}
    }catch(error){el('screenStatus').textContent=error.message;}
    finally{el('analyzeScreen').disabled=false;await discard();}
  };
  el('discardScreen').onclick=discard;
  el('saveMemory').onclick=async()=>{const text=el('memoryText').value.trim();if(!text)return;try{await api('/ai/memories',{method:'POST',body:JSON.stringify({text})});el('memoryText').value='';await memories();}catch(error){notice(error.message);}};
  el('clearHistory').onclick=async()=>{if(!confirm('清空手机短期对话？明确保存的记忆不会删除。'))return;try{await stopReply();await api('/ai/history',{method:'DELETE'});notice('短期对话已清空');}catch(error){notice(error.message);}};
  el('refreshHealth').onclick=health;el('refreshTasks').onclick=tasks;el('stopReply').onclick=stopReply;
  el('checkCloud').onclick=async()=>{el('checkCloud').disabled=true;try{await api('/health/cloud-check',{method:'POST'});await health();}catch(error){notice(error.message);}finally{el('checkCloud').disabled=false;}};
  const priorSave=window.saveKey;window.saveKey=function(){priorSave();health();tasks();memories();};
  // Every reply, including screen analysis, uses local-only cancellable TTS.
  window.speakReply=function(message){stopSpeech();feedSpeech(String(message || '').slice(0,1200),true);};
  const priorStart=window.startRecording;window.startRecording=async function(){await stopReply();return priorStart();};
  // New terminal states stop the old single-task polling loop as well.
  window.pollCodexTask=async function(taskId){
    if(activeCodexTaskTimer)clearTimeout(activeCodexTaskTimer);
    try{
      const task=await(await api('/codex/tasks/'+encodeURIComponent(taskId))).json();
      el('codexTaskBox').style.display='block';el('codexTaskStatus').textContent=(task.message || task.status)+(task.stage ? ' · '+task.stage:'');
      if(['completed','failed','cancelled','interrupted'].includes(task.status)){localStorage.removeItem('jarvis_codex_task_id');await tasks();return;}
    }catch(error){el('codexTaskStatus').textContent=error.message;}
    activeCodexTaskTimer=setTimeout(()=>window.pollCodexTask(taskId),3000);
  };
  health();tasks();memories();
  setInterval(()=>{if(!document.hidden){health();tasks();}},10000);
})();
