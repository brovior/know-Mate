/* Execute the production frontend against a small DOM and controllable Bridge promises. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve=a; reject=b; }); return {promise, resolve, reject}; };
const tick = async () => { for(let i=0;i<20;i++) await Promise.resolve(); };

function fixture() {
  const elements = new Map();
  function element() {
    return { value: '', checked: true, style: {}, dataset: {}, children: [], disabled: false,
      classList: {add(){}, remove(){}, toggle(){}},
      appendChild(el) { this.children.push(el); },
      querySelector(){ return null; }, querySelectorAll(){ return []; },
      addEventListener(){}, focus(){}, remove(){},
      set innerHTML(value) { this._html=value; this.children=[]; }, get innerHTML(){ return this._html || ''; },
    };
  }
  const calls = {sent:[], saved:[], deleted:[], cancelled:[], list:[], restored:[], blocks:[], toasts:[], writes:[]};
  const timers = new Map(); let nextTimer = 0;
  const bridge = {
    sendQuery(payload){ calls.sent.push(JSON.parse(payload)); },
    saveThread(mode,payload) { const d=deferred(); calls.saved.push({mode,thread:JSON.parse(payload), ...d}); return d.promise; },
    deleteThread(mode,id) { const d=deferred(); calls.deleted.push({mode,id,...d}); return d.promise; },
    getThreads(mode){ const d=deferred(); calls.list.push({mode,...d}); return d.promise; },
    getThread(mode,id){ const d=deferred(); calls.restored.push({mode,id,...d}); return d.promise; },
    cancelQuery(id){ calls.cancelled.push(id); },
    getQueryState(){ return Promise.resolve(JSON.stringify({busy:false,state:'IDLE',request_id:null,timeout_ms:100})); },
    excludeFile(){ calls.writes.push('exclude'); return Promise.resolve('query_busy'); },
    unexcludeFile(){ calls.writes.push('unexclude'); return Promise.resolve('query_busy'); },
    removeWatchFolder(){ calls.writes.push('remove'); return Promise.resolve('[]'); },
  };
  const context = vm.createContext({console, setTimeout(fn){const id=++nextTimer;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    QWebChannel: function(){}, qt:{webChannelTransport:{}}, window:{confirm:()=>true},
    document: {getElementById(id){if(!elements.has(id)) elements.set(id, element());return elements.get(id);},
      createElement:element, createDocumentFragment:element, querySelector(){return null;},
      querySelectorAll(selector){return selector === '.btn-send' ? [this.getElementById('send')] : []; }},
    testBridge:bridge, testCalls:calls });
  vm.runInContext(source, context);
  vm.runInContext('bridge=testBridge; backendBusy=false; queryTimeoutMs=100; appendAiBlocks=(blocks)=>testCalls.blocks.push(blocks); showToast=(msg)=>testCalls.toasts.push(msg);',context);
  return {calls, bridge, timers, context, elements, run(code){return vm.runInContext(code,context);},
    input(text){context.document.getElementById('inputPill').value=text;},
    state(busy,state='IDLE',id){context.stateArg={busy,state,request_id:id,timeout_ms:100};return vm.runInContext('onQueryStateChanged(stateArg)',context);},
  };
}
async function accepted(f) {
  f.input('질문'); const p=f.run('sendMsg()'); await tick();
  assert.equal(f.calls.saved.length,1); assert.equal(f.calls.sent.length,0);
  f.calls.saved[0].resolve('{"ok":true}'); await p;
  assert.equal(f.calls.sent.length,1);
  return f.calls.sent[0];
}
function response(f, req, status='success',extra={}) {
  f.context.responseArg=JSON.stringify({...req,status,blocks:[{type:'text',content:'답변'}],...extra});
  return f.run('onResponse(responseArg)');
}
const tests = [];
function test(name,run){tests.push({name,run});}

test('save ACK precedes dispatch and duplicate send is reserved', async()=>{
 const f=fixture();f.input('first');const p=f.run('sendMsg()'); await tick();
 f.run('sendMsg()');assert.equal(f.calls.saved.length,1);assert.equal(f.calls.sent.length,0);
 assert.equal(f.elements.get('inputPill').disabled,false);assert.equal(f.elements.get('send').disabled,true);
 f.calls.saved[0].resolve('{"ok":true}');await p;assert.equal(f.calls.sent.length,1);
});
test('empty scopes never create or save a question', async()=>{
 const f=fixture();f.input('q'); await f.run('scopeLocal=false;scopeShared=false;scopeMail=false;sendMsg()');
 assert.equal(f.calls.saved.length,0);assert.equal(f.calls.sent.length,0);assert.equal(f.run('currentThread'),null);
});
test('failed or malformed save preserves dirty question without dispatch',async()=>{
 for (const result of ['{"ok":false}','invalid']) {
 const f=fixture();f.input('q');const p=f.run('sendMsg()');await tick();f.calls.saved[0].resolve(result);await p;
 assert.equal(f.calls.sent.length,0);assert.equal(f.run('currentThread.messages.length'),1);
 const nav=f.run('newThread()');await tick();f.calls.saved[1].resolve('{"ok":false}');await nav;
 assert.equal(f.run('currentThread.messages.length'),1);
 }
});
test('navigation during user save dispatches the original question after its ACK',async()=>{
 const f=fixture();f.input('q');const send=f.run('sendMsg()');await tick();const nav=f.run('newThread()');
 f.calls.saved[0].resolve('{"ok":true}');await Promise.all([send,nav]);
 assert.equal(f.calls.sent.length,1);assert.equal(f.run('currentThread'),null);
 assert.equal(f.calls.sent[0].thread_id,f.calls.saved[0].thread.id);assert.equal(f.calls.cancelled.length,0);
});

test('mode switch while awaiting user save keeps the original request mode',async()=>{
 const f=fixture();f.input('question');const send=f.run('sendMsg()');await tick();
 const away=f.run('switchMode("mes")');await tick();assert.equal(f.calls.sent.length,0);
 f.calls.saved[0].resolve('{"ok":true}');await Promise.all([send,away]);
 assert.equal(f.run('currentMode'),'mes');assert.equal(f.calls.sent.length,1);
 assert.equal(f.calls.sent[0].mode,'knowledge');assert.equal(f.run('waiting'),false);
 const answer=response(f,f.calls.sent[0]);await tick();assert.equal(f.calls.saved[1].mode,'knowledge');
 assert.equal(f.calls.blocks.length,0);f.calls.saved[1].resolve('{"ok":true}');await answer;
});
test('returning to a pending conversation keeps the request and receives its answer',async()=>{
 const f=fixture();const req=await accepted(f);const original=f.run('JSON.stringify(currentThread)');
 const away=f.run('restoreThread({id:"other",mode:"knowledge"})');await tick();
 f.calls.restored[0].resolve('{"id":"other","mode":"knowledge","messages":[]}');await away;
 assert.equal(f.calls.cancelled.length,0);assert.equal(f.run('waiting'),false);
 f.context.origin={id:req.thread_id,mode:req.mode};
 const back=f.run('restoreThread(origin)');await tick();f.calls.restored[1].resolve(original);await back;
 assert.equal(f.run('waiting'),true);
 const answer=response(f,req);await tick();assert.equal(f.calls.saved.length,2);
 assert.equal(f.calls.saved[1].thread.id,req.thread_id);assert.equal(f.calls.saved[1].thread.messages.length,2);
 f.calls.saved[1].resolve('{"ok":true}');await answer;
 assert.equal(f.run('currentThread.messages.length'),2);assert.equal(f.calls.blocks.length,1);
});

test('background answer is saved only to its origin and appears on a later restore',async()=>{
 const f=fixture();const req=await accepted(f);
 const away=f.run('restoreThread({id:"other",mode:"knowledge"})');await tick();
 f.calls.restored[0].resolve('{"id":"other","mode":"knowledge","messages":[]}');await away;
 const answer=response(f,req);await tick();
 assert.equal(f.calls.blocks.length,0);assert.equal(f.run('currentThread.messages.length'),0);
 assert.equal(f.calls.saved[1].thread.id,req.thread_id);
 const stored=JSON.stringify(f.calls.saved[1].thread);
 f.calls.saved[1].resolve('{"ok":true}');await answer;
 f.context.origin={id:req.thread_id,mode:req.mode};const back=f.run('restoreThread(origin)');await tick();
 f.calls.restored[1].resolve(stored);await back;
 assert.equal(f.run('currentThread.messages.length'),2);assert.equal(f.calls.blocks.length,1);
 assert.equal(f.run('waiting'),false);assert.equal(f.calls.cancelled.length,0);
});

test('new conversation leaves a request running and still enforces single flight',async()=>{
 const f=fixture();const req=await accepted(f);await f.run('newThread()');
 f.input('next');await f.run('sendMsg()');assert.equal(f.calls.sent.length,1);
 assert.equal(f.calls.cancelled.length,0);assert.equal(f.run('currentThread'),null);
 const answer=response(f,req);await tick();assert.equal(f.calls.saved[1].thread.id,req.thread_id);
 await response(f,req);assert.equal(f.calls.saved.length,2);assert.equal(f.calls.blocks.length,0);
 f.calls.saved[1].resolve('{"ok":true}');await answer;
 assert.equal(f.run('backendBusy'),true);f.state(false,'IDLE',req.request_id);
 assert.equal(f.run('queryLocked()'),false);
});

test('answer arriving during origin restore cannot be replaced by its older stored snapshot',async()=>{
 const f=fixture();const req=await accepted(f);const old=f.run('JSON.stringify(currentThread)');await f.run('newThread()');
 f.context.origin={id:req.thread_id,mode:req.mode};const back=f.run('restoreThread(origin)');await tick();
 const answer=response(f,req);await tick();f.calls.saved[1].resolve('{"ok":true}');await answer;
 f.calls.restored[0].resolve(old);await back;
 assert.equal(f.run('currentThread.id'),req.thread_id);assert.equal(f.run('currentThread.messages.length'),2);
 assert.equal(f.calls.blocks.length,1);assert.equal(f.run('navigationPending'),false);
});

test('background persistence does not invalidate an unrelated conversation restore',async()=>{
 const f=fixture();const req=await accepted(f);const away=f.run('restoreThread({id:"other",mode:"knowledge"})');await tick();
 const answer=response(f,req);await tick();f.calls.saved[1].resolve('{"ok":true}');await answer;
 f.calls.restored[0].resolve('{"id":"other","mode":"knowledge","messages":[]}');await away;
 assert.equal(f.run('currentThread.id'),'other');assert.equal(f.run('currentThread.messages.length'),0);
 assert.equal(f.run('waiting'),false);
});

test('failed background save survives a later request and is retried from its original conversation',async()=>{
 const f=fixture();const req=await accepted(f);const old=f.run('JSON.stringify(currentThread)');await f.run('newThread()');
 const answer=response(f,req);await tick();f.calls.saved[1].resolve('{"ok":false}');await answer;
 f.state(false,'IDLE',req.request_id);f.input('second');const second=f.run('sendMsg()');await tick();
 f.calls.saved[2].resolve('{"ok":true}');await second;
 f.context.origin={id:req.thread_id,mode:req.mode};const back=f.run('restoreThread(origin)');await tick();
 f.calls.restored[0].resolve(old);await back;
 assert.equal(f.run('currentThread.messages.length'),2);assert.equal(f.run('waiting'),false);
 const leave=f.run('newThread()');await tick();assert.equal(f.calls.saved[3].thread.id,req.thread_id);
 assert.equal(f.calls.saved[3].thread.messages.length,2);f.calls.saved[3].resolve('{"ok":true}');await leave;
 assert.equal(f.run('retainedThreads.has(origin.mode+":"+origin.id)'),false);
});

test('deleting the background request cancels only it and never saves its late answer',async()=>{
 const f=fixture();const req=await accepted(f);await f.run('newThread()');
 f.context.origin={id:req.thread_id,mode:req.mode};const deletion=f.run('deleteQuestion(origin)');await tick();
 assert.deepEqual(f.calls.cancelled,[req.request_id]);await response(f,req);
 assert.equal(f.calls.saved.length,1);f.calls.deleted[0].resolve('{"ok":true}');await deletion;
 assert.equal(f.run('retainedThreads.has(origin.mode+":"+origin.id)'),false);
});

test('deletion waits for a dispatched background save so it cannot recreate the conversation',async()=>{
 const f=fixture();const req=await accepted(f);await f.run('newThread()');
 const answer=response(f,req);await tick();f.context.origin={id:req.thread_id,mode:req.mode};
 const deletion=f.run('deleteQuestion(origin)');await tick();assert.equal(f.calls.deleted.length,0);
 f.calls.saved[1].resolve('{"ok":true}');await answer;await tick();assert.equal(f.calls.deleted.length,1);
 f.calls.deleted[0].resolve('{"ok":true}');await deletion;await response(f,req);
 assert.equal(f.calls.saved.length,2);assert.equal(f.run('currentThread'),null);
});

test('deleting a restore target rejects its stale snapshot even after deletion completes',async()=>{
 const f=fixture();const req=await accepted(f);const back=f.run('restoreThread({id:"other",mode:"knowledge"})');await tick();
 const deletion=f.run('deleteQuestion({id:"other",mode:"knowledge"})');await tick();
 f.calls.deleted[0].resolve('{"ok":true}');await deletion;
 f.calls.restored[0].resolve('{"id":"other","mode":"knowledge","messages":[]}');await back;
 assert.equal(f.run('currentThread.id'),req.thread_id);assert.equal(f.run('navigationPending'),false);
 assert.equal(f.calls.cancelled.length,0);
});

test('returning across modes resumes the existing request without redispatch',async()=>{
 const f=fixture();const req=await accepted(f);const old=f.run('JSON.stringify(currentThread)');
 await f.run('switchMode("mes")');await f.run('switchMode("knowledge")');
 f.context.origin={id:req.thread_id,mode:req.mode};const back=f.run('restoreThread(origin)');await tick();
 f.calls.restored[0].resolve(old);await back;
 assert.equal(f.run('waiting'),true);assert.equal(f.calls.sent.length,1);assert.equal(f.calls.cancelled.length,0);
 const answer=response(f,req);await tick();f.calls.saved[1].resolve('{"ok":true}');await answer;
 assert.equal(f.run('currentThread.messages.length'),2);
});

test('timeout still retires a background request without attaching content to the visible conversation',async()=>{
 const f=fixture();const req=await accepted(f);await f.run('newThread()');
 await response(f,req,'timeout');f.state(true,'RETIRED_PENDING_EXIT',req.request_id);
 assert.equal(f.run('currentThread'),null);assert.equal(f.calls.saved.length,1);
 assert.equal(f.run('retainedThreads.size'),0);assert.equal(f.run('queryLocked()'),true);
});

test('mode switch keeps the pending answer in its originating conversation',async()=>{
 const f=fixture();const req=await accepted(f);await f.run('switchMode("mes")');const answer=response(f,req);await tick();
 assert.equal(f.run('currentMode'),'mes');assert.equal(f.run('currentThread'),null);
 assert.equal(f.calls.blocks.length,0);assert.equal(f.calls.saved.length,2);assert.deepEqual(f.calls.cancelled,[]);
 assert.equal(f.calls.saved[1].mode,'knowledge');assert.equal(f.calls.saved[1].thread.id,req.thread_id);
 f.calls.saved[1].resolve('{"ok":true}');await answer;
 assert.equal(f.run('backendBusy'),true);
});
test('terminal and duplicate cannot release busy before actual IDLE',async()=>{
 const f=fixture();const req=await accepted(f);const p=response(f,req);await tick();
 assert.equal(f.run('backendBusy'),true);assert.equal(f.calls.saved.length,2);
 await response(f,req);assert.equal(f.calls.blocks.length,1);
 f.calls.saved[1].resolve('{"ok":true}');await p;f.state(false,'IDLE',req.request_id);
 assert.equal(f.run('backendBusy'),false);
});
test('malformed and stale terminal or state do not affect current request',async()=>{
 const f=fixture();const req=await accepted(f);await f.run('onResponse("{")');
 await response(f,req,'success',{thread_id:'other'}); f.state(false,'IDLE','old');
 assert.equal(f.run('backendBusy'),true);assert.equal(f.run('waiting'),true);assert.equal(f.calls.saved.length,1);
 const failed=f.run('restoreThread({id:"missing",mode:"knowledge"})');await tick();
 f.calls.restored[0].resolve('null');await failed;
 assert.equal(f.run('currentThread.id'),req.thread_id);assert.equal(f.run('waiting'),true);
 assert.equal(f.calls.cancelled.length,0);
});
test('cancel timeout keeps send locked and preserves editable input',async()=>{
 const f=fixture();const req=await accepted(f);await response(f,req,'timeout');f.state(true,'RETIRED_PENDING_EXIT',req.request_id);
 f.input('next');await f.run('sendMsg()');assert.equal(f.calls.sent.length,1);
 assert.equal(f.elements.get('inputPill').disabled,false);assert.match(f.elements.get('inputPill').placeholder,/정리/);
});
test('current delete waits ACK and late answer never resurrects deleted thread',async()=>{
 const f=fixture();const req=await accepted(f);const p=f.run('deleteQuestion(currentThread)');await tick();
 assert.notEqual(f.run('currentThread'),null);assert.equal(f.calls.deleted.length,1);
 await response(f,req);f.calls.deleted[0].resolve('{"ok":true}');await p;
 assert.equal(f.run('currentThread'),null);assert.equal(f.calls.saved.length,1);
});
test('delete failure preserves thread',async()=>{
 const f=fixture();await accepted(f);const p=f.run('deleteQuestion(currentThread)');await tick();
 f.calls.deleted[0].resolve('{"ok":false}');await p;assert.notEqual(f.run('currentThread'),null);
});
test('older list cannot overwrite newer list in same view',async()=>{
 const f=fixture();f.run('renderRecentList=(items)=>testCalls.blocks.push(items);loadRecentQuestions();loadRecentQuestions()');
 f.calls.list[1].resolve('[{"id":"new"}]');await tick();f.calls.list[0].resolve('[{"id":"old"}]');await tick();
 assert.equal(f.calls.blocks.length,1);assert.equal(f.calls.blocks[0][0].id,'new');
});
test('save mutation invalidates list already in flight',async()=>{
 const f=fixture();f.run('renderRecentList=(items)=>testCalls.blocks.push(items);loadRecentQuestions()');
 const req=await accepted(f);f.calls.list[0].resolve('[{"id":"old"}]');await tick();assert.equal(f.calls.blocks.length,0);
 f.calls.list.at(-1).resolve('[{"id":"latest"}]');await tick();assert.equal(f.calls.blocks[0][0].id,'latest');
 assert.ok(req.thread_id);
});
test('inactive delete invalidates pending list until delete ACK refresh',async()=>{
 const f=fixture();f.run('renderRecentList=(items)=>testCalls.blocks.push(items);loadRecentQuestions()');
 const p=f.run('deleteQuestion({id:"inactive",mode:"knowledge"})');
 f.calls.list[0].resolve('[{"id":"inactive"}]');await tick();assert.equal(f.calls.blocks.length,0);
 f.calls.deleted[0].resolve('{"ok":true}');await p;f.calls.list.at(-1).resolve('[]');await tick();assert.equal(f.calls.blocks.length,1);
});
test('restore fetches fresh thread and ignores reversed requests',async()=>{
 const f=fixture();const a=f.run('restoreThread({id:"a",mode:"knowledge",messages:[{role:"user",content:"stale"}]})');await tick();
 const b=f.run('restoreThread({id:"b",mode:"knowledge"})');await tick();
 f.calls.restored[1].resolve('{"id":"b","mode":"knowledge","messages":[]}');await b;
 f.calls.restored[0].resolve('{"id":"a","mode":"knowledge","messages":[]}');await a;
 assert.equal(f.run('currentThread.id'),'b');assert.equal(f.run('navigationPending'),false);
});
test('restore and mode navigation wait for dirty response save ACK',async()=>{
 const f=fixture();const req=await accepted(f);const p=response(f,req);await tick();
 const nav=f.run('switchMode("mes")');await tick();assert.equal(f.run('currentMode'),'knowledge');
 f.calls.saved[1].resolve('{"ok":true}');await Promise.all([p,nav]);assert.equal(f.run('currentMode'),'mes');
});
test('query policy writes are blocked while reading remains possible',async()=>{
 const f=fixture();await accepted(f);f.run('excludeFailureFile("x");unexcludeFailureFile("x");removeFolder(null,"x");saveSettings();runBulkFailureAction()');
 assert.equal(f.calls.writes.length,0);assert.ok(f.calls.toasts.length >= 4);
});
test('scope edits during request affect only next dispatch snapshot',async()=>{
 const f=fixture();const req=await accepted(f);f.run('scopeLocal=false;scopeShared=false;scopeMail=true');
 assert.deepEqual(req.scopes,['local','shared','mail']);
});
test('safety timer requests cancel without opening another execution slot',async()=>{
 const f=fixture();const req=await accepted(f);
 f.bridge.getQueryState=()=>Promise.resolve(JSON.stringify({busy:true,state:'RETIRED_PENDING_EXIT',request_id:req.request_id,timeout_ms:100}));
 const callback=[...f.timers.values()][0];await callback();assert.equal(f.run('backendBusy'),true);assert.deepEqual(f.calls.cancelled,[req.request_id]);
});
test('pending current delete blocks resending and restoring until ACK',async()=>{
 const f=fixture();const req=await accepted(f);f.state(false,'IDLE',req.request_id);
 const id=f.run('currentThread.id');const del=f.run('deleteQuestion(currentThread)');await tick();
 f.input('next');await f.run('sendMsg()');assert.equal(f.calls.sent.length,1);
 f.context.restoreTarget={id,mode:'knowledge'};await f.run('restoreThread(restoreTarget)');
 assert.equal(f.calls.restored.length,0);f.calls.deleted[0].resolve('{"ok":true}');await del;
 assert.equal(f.run('currentThread'),null);
});
test('old safety callback cannot cancel a newly accepted request',async()=>{
 const f=fixture();const first=await accepted(f);const callback=[...f.timers.values()][0];
 f.state(false,'IDLE',first.request_id);f.input('next');const send=f.run('sendMsg()');await tick();
 f.calls.saved[1].resolve('{"ok":true}');await send;await callback();assert.equal(f.calls.cancelled.length,0);
 assert.equal(f.calls.sent.length,2);assert.equal(f.run('backendBusy'),true);
});
test('rejected admission reconciles authoritative backend state without terminal mutation',async()=>{
 const f=fixture();const req=await accepted(f);f.bridge.getQueryState=()=>Promise.resolve(JSON.stringify({busy:true,state:'RUNNING',request_id:'other',timeout_ms:100}));
 await response(f,req,'rejected');await tick();assert.equal(f.run('backendBusy'),true);
 assert.equal(f.run('activeRequest.terminalSeen'),false);assert.equal(f.calls.blocks.length,0);
});
test('failed user save preserves input and retry sends only one user message',async()=>{
 const f=fixture();f.input('draft');const first=f.run('sendMsg()');await tick();
 f.calls.saved[0].resolve('{"ok":false}');await first;
 assert.equal(f.elements.get('inputPill').value,'draft');
 const retry=f.run('sendMsg()');await tick();f.calls.saved[1].resolve('{"ok":true}');await retry;
 assert.equal(f.calls.sent.length,1);assert.equal(f.run('currentThread.messages.length'),1);
 assert.equal(f.elements.get('inputPill').value,'');
});
test('save ACK never overwrites newly edited input',async()=>{
 const f=fixture();f.input('first');const send=f.run('sendMsg()');await tick();f.input('next draft');
 f.calls.saved[0].resolve('{"ok":true}');await send;
 assert.equal(f.elements.get('inputPill').value,'next draft');assert.equal(f.calls.sent[0].query,'first');
});
test('changed unsent draft replaces pending user message on retry',async()=>{
 const f=fixture();f.input('first');const send=f.run('sendMsg()');await tick();f.calls.saved[0].resolve('{"ok":false}');await send;
 f.input('revised');const retry=f.run('sendMsg()');await tick();f.calls.saved[1].resolve('{"ok":true}');await tick();
 f.calls.saved[2].resolve('{"ok":true}');await retry;
 assert.equal(f.calls.sent.length,1);assert.equal(f.calls.sent[0].query,'revised');assert.equal(f.run('currentThread.messages.length'),1);
 assert.equal(f.run('currentThread.messages[0].content'),'revised');
});
test('dispatch errors preserve draft and retry without duplicate message',async()=>{
 for (const rejectAsync of [false,true]) {
 const f=fixture();const originalSend=f.bridge.sendQuery;
 f.bridge.sendQuery=()=>{if(rejectAsync) return Promise.reject(new Error('transport'));throw new Error('transport');};
 f.input('draft');const send=f.run('sendMsg()');await tick();f.calls.saved[0].resolve('{"ok":true}');await send;await tick();
 assert.equal(f.elements.get('inputPill').value,'draft');assert.equal(f.run('frontendSubmitting'),false);
 f.bridge.sendQuery=originalSend;await f.run('sendMsg()');assert.equal(f.calls.sent.length,1);assert.equal(f.run('currentThread.messages.length'),1);
 }
});
test('same active ID rejection never removes running spinner',async()=>{
 const f=fixture();const req=await accepted(f);
 f.bridge.getQueryState=()=>Promise.resolve(JSON.stringify({busy:true,state:'RUNNING',request_id:req.request_id,timeout_ms:100}));
 await response(f,req,'rejected');await tick();assert.equal(f.run('waiting'),true);assert.equal(f.run('backendBusy'),true);
 assert.equal(f.run('activeRequest.terminalSeen'),false);
});
test('old running state read cannot relock same request after IDLE signal',async()=>{
 const f=fixture();const req=await accepted(f);const stale=deferred();f.bridge.getQueryState=()=>stale.promise;
 const read=f.run('refreshQueryState()');f.state(false,'IDLE',req.request_id);
 stale.resolve(JSON.stringify({busy:true,state:'RUNNING',request_id:req.request_id,timeout_ms:100}));await read;
 assert.equal(f.run('backendBusy'),false);assert.equal(f.run('queryState'),'IDLE');
});
test('newer state read supersedes older response within same request',async()=>{
 const f=fixture();const req=await accepted(f);const old=deferred();const latest=deferred();let count=0;
 f.bridge.getQueryState=()=>++count===1 ? old.promise : latest.promise;
 const a=f.run('refreshQueryState()');const b=f.run('refreshQueryState()');
 latest.resolve(JSON.stringify({busy:false,state:'IDLE',request_id:req.request_id,timeout_ms:100}));await b;
 old.resolve(JSON.stringify({busy:true,state:'RUNNING',request_id:req.request_id,timeout_ms:100}));await a;
 assert.equal(f.run('backendBusy'),false);
});
(async()=>{for(const {name,run} of tests){await run();console.log('PASS '+name);}console.log(`${tests.length} frontend scenarios passed`);})().catch(error=>{console.error(error);process.exitCode=1;});
