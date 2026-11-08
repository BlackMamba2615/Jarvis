// DOM simulation: no browser credentials, microphone, real network or audio.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
class Element {
  constructor() { this.style={};this.value='';this.textContent='';this.children=[];this.disabled=false;this.hidden=false; }
  append(...values){this.children.push(...values);}
  appendChild(value){this.children.push(value);return value;}
  replaceChildren(...values){this.children=[...values];}
  insertAdjacentElement(_where,value){this.children.push(value);}
  removeAttribute(name){delete this[name];}
  pause(){}
  load(){if(this.onemptied)this.onemptied();}
  async play(){queueMicrotask(()=>this.onended?.());}
}
const get = id => {if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let remoteVoiceCalled=0, queuedStreams=[], desktopCalls=[];
const context = {
  document:{getElementById:get,querySelector:()=>get('original-security'),createElement:()=>new Element(),body:new Element(),hidden:false},
  apiKey:'test-only-key',voiceReplyEnabled:false,activeCodexTaskTimer:null,activeQrUrl:null,qrExpiryTimer:null,
  TextDecoder,TextEncoder,ReadableStream,Response,Blob,URL,AbortController,DOMException,
  setTimeout,clearTimeout,setInterval:()=>1,clearInterval:()=>{},confirm:()=>true,
  localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  saveKey(){},startRecording(){},clearLoginQr(){},showLoginQr(){},showCodexDraft(){},
  speakReply(){remoteVoiceCalled++;},speechSynthesis:{cancel(){}},
  fetch:async(path,options)=>{
    assert.equal(options.headers['X-Jarvis-Key'],'test-only-key');
    if(path==='/health/details')return Response.json({phone_voice:'ready',desktop_bridge:'unavailable',desktop:{microphone:'unknown'},local:{state:'ready'},cloud:{state:'configured_unverified'}});
    if(path==='/codex/tasks')return Response.json({tasks:[]});
    if(path==='/ai/memories')return Response.json({memories:[]});
    if(path==='/ai/chat/stream')return queuedStreams.shift();
    if(path.startsWith('/ai/cancel/'))return Response.json({cancelled:true});
    if(path==='/desktop/control'){
      const payload=JSON.parse(options.body);desktopCalls.push(payload);
      if(payload.operation==='windows')return Response.json({success:true,local_only:true,windows:[{id:'w-test',app:'test.exe',title:'<script>untrusted</script>'}]});
      if(payload.operation==='inspect')return Response.json({success:true,local_only:true,controls:[{id:'control-test',name:'测试按钮',role:'button'}]});
      if(payload.operation==='click')return Response.json({success:false,desktop_token:'approval-test',preview:{operation:'click',window:{app:'test.exe',title:'测试'},control:'测试按钮'}});
      return Response.json({success:true,local_only:true,message:'已处理'});
    }
    throw new Error('unexpected test path');
  },
};
context.window=context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(__dirname+'/upgrade_ui.js','utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function streamReply(id,text,hold=false){
  const encoder=new TextEncoder();let controller;
  const stream=new ReadableStream({start(c){controller=c;c.enqueue(encoder.encode(JSON.stringify({type:'start',request_id:id})+'\n'));if(!hold){c.enqueue(encoder.encode(JSON.stringify({type:'delta',text})+'\n'+JSON.stringify({type:'result',message:text,success:true,route:{backend:'local',model:'test'}})+'\n'));c.close();}}});
  return {response:new Response(stream,{headers:{'content-type':'application/x-ndjson'}}),controller};
}
(async()=>{
  await tick();
  assert.match(get('realHealth').textContent,/手机识别：就绪/);
  assert.match(get('realHealth').textContent,/桌面桥：不可用/);
  const first=streamReply('a','测试回答。');queuedStreams.push(first.response);
  get('chatText').value='你好';await get('sendText').onclick();
  assert.equal(get('reply').textContent,'Jarvis：测试回答。');
  assert.match(get('modelRoute').textContent,/本地 test/);
  assert.equal(remoteVoiceCalled,0);
  // A late old stream may not overwrite the next request's answer or id.
  const old=streamReply('old','',true);queuedStreams.push(old.response);
  get('chatText').value='old';const oldRun=get('sendText').onclick();await tick();
  const next=streamReply('next','新回答。');queuedStreams.push(next.response);
  get('chatText').value='new';await get('sendText').onclick();
  old.controller.enqueue(new TextEncoder().encode(JSON.stringify({type:'delta',text:'过期内容'})+'\n'));old.controller.close();
  await oldRun;
  assert.equal(get('reply').textContent,'Jarvis：新回答。');
  context.speakReply('窗口分析结果');
  assert.equal(remoteVoiceCalled,0);
  await get('stopReply').onclick();
  assert.match(get('state').textContent,/已停止当前回答/);
  await get('desktopRefresh').onclick();
  assert.equal(get('desktopWindow').children[0].textContent,'test.exe · <script>untrusted</script>');
  get('desktopWindow').value='w-test';await get('desktopInspect').onclick();
  get('desktopControl').value='control-test';get('desktopOperation').value='click';
  await get('desktopPrepare').onclick();assert.equal(get('desktopApproval').hidden,false);
  get('desktopValue').oninput();assert.equal(get('desktopApproval').hidden,true);
  await get('desktopConfirm').onclick();assert.equal(desktopCalls.filter(x=>x.operation==='confirm').length,0);
  await get('desktopPrepare').onclick();
  await get('desktopConfirm').onclick();await get('desktopConfirm').onclick();
  assert.equal(desktopCalls.filter(x=>x.operation==='confirm').length,1);
  assert.equal(desktopCalls.at(-1).token,'approval-test');
  assert.equal(get('desktopApproval').hidden,true);
  console.log('DOM smoke checks passed: health, stream, stale-response isolation, local-only speech, stop.');
})().catch(error=>{console.error(error);process.exitCode=1;});
