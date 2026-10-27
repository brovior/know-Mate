/* Production failure-panel promises, revisions and timers under a small DOM. */
'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
function fixture() {
  const elements = new Map(), timers = new Map(); let next = 0;
  const calls = {reads: [], renders: 0, running: 0, toasts: []};
  function element() {
    const classes = new Set();
    return {style: {}, dataset: {}, scrollTop: 0, textContent: '',
      classList: {add(x){classes.add(x);},remove(x){classes.delete(x);},contains(x){return classes.has(x);},toggle(){}},
      querySelectorAll(){return [];},querySelector(){return null;},addEventListener(){},
      setAttribute(name,value){this[name]=value;},removeAttribute(name){delete this[name];},
    };
  }
  const bridge = {getFailures(){const d=deferred();calls.reads.push(d);return d.promise;},
    retryFile(){return Promise.resolve('ok');}, startReindex(){return Promise.resolve('ok');}};
  const context = vm.createContext({console,QWebChannel:function(){},qt:{webChannelTransport:{}},
    window:{addEventListener(){}},setTimeout(){},clearTimeout(){},
    setInterval(fn){const id=++next;timers.set(id,fn);return id;},clearInterval(id){timers.delete(id);},
    document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
      querySelectorAll(){return [];},querySelector(){return null;}}, testBridge:bridge,testCalls:calls});
  vm.runInContext(source,context);
  const run = code => vm.runInContext(code,context);
  run('bridge=testBridge; renderFailuresPanel=()=>testCalls.renders++; showIndexRunning=()=>testCalls.running++; showToast=msg=>testCalls.toasts.push(msg);');
  return {run,calls,timers,bridge,elements,context};
}
const card = name => ({path:name,name,excluded:false,next_retry_ts:100,kind:'UNKNOWN_TRANSIENT'});
(async () => {
  const f=fixture();
  const opening=f.run('openFailuresPanel()');
  f.run('_failFilter="waiting"');
  f.elements.get('failBody') || f.context.document.getElementById('failBody');
  f.elements.get('failBody').scrollTop=77;
  const changing=f.run('openFailuresPanel()');
  f.run('_failFilter="waiting"');
  assert.equal(f.calls.reads.length,1,'one inflight read');
  f.calls.reads[0].resolve(JSON.stringify([card('old')]));await tick();
  assert.equal(f.calls.renders,0,'stale reply cannot render');
  assert.equal(f.calls.reads.length,2,'one replacement read');
  f.calls.reads[1].resolve(JSON.stringify([card('new')]));await opening;await changing;
  assert.equal(f.calls.renders,1);
  assert.equal(f.run('_failCards[0].name'),'new');
  assert.equal(f.run('_failFilter'),'waiting');
  assert.equal(f.elements.get('failBody').scrollTop,77);
  assert.equal(f.elements.get('failAttentionText').textContent,'확인 필요한 문서 1건');
  assert.equal(f.timers.size,1);
  const count=f.calls.reads.length;
  for(const fn of f.timers.values())fn();await tick();
  assert.equal(f.calls.reads.length,count,'time labels cannot fetch');
  assert.equal(f.calls.renders,1,'time labels cannot rebuild cards');
  f.run('closeFailuresPanel()');assert.equal(f.timers.size,0);
  const closed=f.run('refreshFailAttention()');f.calls.reads[2].resolve('[]');await closed;
  assert.equal(f.calls.renders,1,'closed panel is not rendered');
  assert.equal(f.elements.get('btnFailAttention').style.display,'none');

  for(const result of ['busy','error','not_found','persistence_pending']) {
    const e=fixture();e.bridge.retryFile=()=>Promise.resolve(result);
    e.run('retryFailureFile("a.pptx")');await tick();
    assert.equal(e.calls.running,0,result+' cannot fake running UI');
    assert.equal(e.calls.toasts.length,1);
  }
  const accepted=fixture();accepted.run('openFailuresPanel()');
  accepted.calls.reads[0].resolve('[]');await tick();
  accepted.run('retryFailureFile("a.pptx")');await tick();
  assert.equal(accepted.calls.running,1);
  assert.equal(accepted.run('_failuresPanelOpen()'),true,'single retry keeps panel visible');
  accepted.run('closeFailuresPanel()');

  const streaming=fixture();
  const streamOpening=streaming.run('openFailuresPanel()');
  for(let index=0;index<5;index++) {
    streaming.run('refreshFailAttention()');
    assert.equal(streaming.calls.reads.length,index+1,'one inflight read while dirty');
    streaming.calls.reads[index].resolve(JSON.stringify([card('result'+index)]));
    await tick();
    assert.equal(streaming.calls.renders,index+1,'dirty events cannot starve rendering');
    assert.equal(streaming.run('_failCards[0].name'),'result'+index);
  }
  streaming.calls.reads[5].resolve('[]');await streamOpening;
  assert.equal(streaming.calls.renders,6);
  streaming.run('closeFailuresPanel()');
  console.log('10 failure frontend scenarios passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
