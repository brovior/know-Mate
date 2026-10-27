/* Execute production exclusion-job UI with controlled transport and DOM state. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const tick = async () => { for (let n=0;n<30;n++) await Promise.resolve(); };
const deferred = () => { let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject}; };
const idle = () => ({job_id:'',revision:0,active:false,terminal:false,policy_unknown:false});
const state = (job_id='job-a',extra={}) => ({job_id,revision:1,action:'exclude',stage:'documents',done:45,total:150,active:true,terminal:false,policy_unknown:false,settings_saved:true,result:{},...extra});
function fixture() {
  const elements=new Map(),timers=new Map();let next=0;
  function element() {
    const classes=new Set();
    return {style:{},dataset:{},attrs:{},disabled:false,hidden:false,textContent:'',scrollTop:0,
      classList:{add(x){classes.add(x);},remove(x){classes.delete(x);},contains(x){return classes.has(x);},toggle(x,on){if(on===undefined)on=!classes.has(x);if(on)classes.add(x);else classes.delete(x);}},
      setAttribute(name,value){this.attrs[name]=value;},removeAttribute(name){delete this.attrs[name];},
      querySelectorAll(){return [];},querySelector(){return null;},addEventListener(){},focus(){},
    };
  }
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const calls={starts:[],status:0,toasts:[],index:0,retry:0,query:0,legacy:0,folders:0};
  let backend=idle();
  const bridge={
    startFailurePolicyJob(action,payload){const reply=deferred();calls.starts.push({action,paths:JSON.parse(payload),...reply});return reply.promise;},
    getFailurePolicyJobStatus(){calls.status++;return Promise.resolve(JSON.stringify(backend));},
    getFailures(){return Promise.resolve('[]');},
    startReindex(){calls.index++;return Promise.resolve('ok');},retryFile(){calls.retry++;return Promise.resolve('ok');},
    sendQuery(){calls.query++;},addWatchFolder(){calls.folders++;return Promise.resolve('[]');},
    excludeFile(){calls.legacy++;throw new Error('synchronous fallback');},excludeFiles(){calls.legacy++;throw new Error('synchronous fallback');},
    unexcludeFile(){calls.legacy++;throw new Error('synchronous fallback');},unexcludeFiles(){calls.legacy++;throw new Error('synchronous fallback');},
  };
  const context=vm.createContext({console,QWebChannel:function(){},qt:{webChannelTransport:{}},window:{confirm:()=>true,addEventListener(){}},
    setTimeout(fn,ms){const id=++next;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);},
    setInterval(fn,ms){const id=++next;timers.set(id,{fn,ms});return id;},clearInterval(id){timers.delete(id);},
    document:{getElementById:get,querySelector(){return null;},querySelectorAll(selector){
      if(selector==='.btn-send')return [get('send')];
      if(selector.includes('#btnReindex'))return [get('btnReindex'),get('btnRetryAll'),get('obStartBtn'),get('retry')];
      if(selector.includes('.btn-settings-save'))return [get('settingsSave'),get('folderAdd'),get('exclude'),get('unexclude')];
      return [];
    }},testBridge:bridge,testCalls:calls});
  vm.runInContext(source,context);
  const run=code=>vm.runInContext(code,context);
  run('bridge=testBridge;backendBusy=false;showToast=message=>testCalls.toasts.push(message);');
  function apply(value,live=true){context.value=JSON.stringify(value);run(live?'onFailurePolicyJobChanged(value)':'_applyFailurePolicyState(value)');}
  return {run,calls,timers,bridge,get,setBackend(value){backend=value;},apply,context};
}
const tests=[];
const test=(name,check)=>tests.push({name,check});

test('request feedback appears before acknowledgement and never uses synchronous exclusion',async()=>{
  const f=fixture();const p=f.run('excludeFailureFile("selected.txt")');await tick();
  assert.equal(f.calls.starts.length,1);assert.equal(f.calls.legacy,0);
  assert.equal(f.get('failurePolicyPanel').hidden,false);
  assert.match(f.get('failurePolicyPanelTitle').textContent,/요청 중/);
  assert.equal(f.run('failurePolicyBlocked()'),true);
  f.setBackend(state());f.calls.starts[0].resolve('{"ok":true,"accepted":true,"job_id":"job-a"}');await p;
  assert.equal(f.run('failurePolicyBlocked()'),true);
  assert.equal(f.get('failurePolicyPanelCount').textContent,'45 / 150건');
});
test('closing only hides the popup and reopening restores progress without a toast',async()=>{
  const f=fixture();f.setBackend(state());f.apply(state());f.run('openFailuresPanel()');await tick();
  f.run('closeFailuresPanel()');assert.equal(f.get('failuresOverlay').classList.contains('show'),false);
  assert.equal(f.get('failurePolicySidebar').hidden,false);
  assert.match(f.get('failurePolicySidebarHelp').textContent,/인덱싱과 제외 설정/);
  await f.run('openFailuresPanel()');assert.equal(f.get('failuresOverlay').classList.contains('show'),true);
  assert.equal(f.get('failurePolicyPanelCount').textContent,'45 / 150건');assert.equal(f.calls.toasts.length,0);
});
test('active job blocks programmatic indexing retry question and policy mutations',async()=>{
  const f=fixture();f.apply(state());f.run('startReindex();retryFailureFile("x");excludeFailureFile("x");unexcludeFailureFile("x");sendMsg();saveSettings();');await tick();
  assert.equal(f.calls.index+f.calls.retry+f.calls.query+f.calls.starts.length,0);
  for(const id of ['send','btnReindex','btnRetryAll','settingsSave','folderAdd','exclude','unexclude','retry'])assert.equal(f.get(id).disabled,true,id);
  assert(f.calls.toasts.every(message=>message.includes('문서 제외 작업이 진행 중')));
});
test('terminal-looking stage does not unlock before backend confirms join',()=>{
  const f=fixture();f.apply(state('job-a',{stage:'complete',done:150}));
  assert.equal(f.run('failurePolicyBlocked()'),true);assert.equal(f.calls.toasts.length,0);
  assert.match(f.get('failurePolicyPanelTitle').textContent,/마무리/);
  assert.equal(f.get('failurePolicyPanelTrack').classList.contains('indeterminate'),true);
});
test('real terminal unlocks once and an older progress revision cannot relock',async()=>{
  const f=fixture();f.apply(state());const done=state('job-a',{revision:5,stage:'complete',active:false,terminal:true,result:{changed:150},counts:{local_count:0}});
  f.apply(done);f.apply(done);f.apply(state());await tick();
  assert.equal(f.run('failurePolicyBlocked()'),false);assert.equal(f.calls.toasts.length,1);
  assert.match(f.calls.toasts[0],/150건 제외 설정 적용/);
  assert.equal(f.get('failurePolicySidebar').hidden,true);
});
test('restored recent terminal shows its summary without another completion toast',()=>{
  const f=fixture();f.apply(state('old',{active:false,terminal:true,stage:'complete',result:{changed:150}}),false);
  assert.equal(f.calls.toasts.length,0);assert.equal(f.get('failurePolicyPanel').hidden,false);
  assert.match(f.get('failurePolicyPanelDetail').textContent,/150건 제외 설정 적용/);
});
test('equal-revision late progress cannot undo a confirmed native join',()=>{
  const f=fixture();f.apply(state('job-a',{revision:5,active:false,terminal:true,stage:'complete',result:{changed:150}}));
  f.apply(state('job-a',{revision:5,stage:'complete'}));
  assert.equal(f.run('failurePolicyBlocked()'),false);assert.equal(f.run('_failurePolicyState.terminal'),true);
});
test('partial cleanup distinguishes durable settings from failed and unattempted documents',()=>{
  const f=fixture();f.apply(state('job-a',{active:false,terminal:true,stage:'error',result:{changed:150,failed:2,not_attempted:1},counts:null}));
  assert.match(f.calls.toasts[0],/150건 제외 설정 적용/);assert.match(f.calls.toasts[0],/3건 정리 미완료/);
  assert.equal(f.get('failurePolicyPanel').dataset.state,'warning');assert.equal(f.run('failurePolicyBlocked()'),false);
});
test('unknown saved policy stays blocked after join and allows only explicit reconciliation',async()=>{
  const f=fixture();f.apply(state('unknown',{active:false,terminal:true,stage:'error',policy_unknown:true,settings_saved:null}));
  assert.equal(f.run('failurePolicyBlocked()'),true);assert.equal(f.get('failurePolicyPanelRecheck').hidden,false);
  const p=f.run('recheckFailurePolicy()');await tick();assert.equal(f.calls.starts[0].action,'reconcile');assert.deepEqual(f.calls.starts[0].paths,[]);
  f.setBackend(state('recheck',{action:'reconcile',active:false,terminal:true,stage:'complete'}));
  f.calls.starts[0].resolve('{"ok":true,"accepted":true,"job_id":"recheck"}');await p;
  assert.equal(f.run('failurePolicyBlocked()'),false);
});
test('a rejected request never leaves a fake running state',async()=>{
  const f=fixture();const p=f.run('excludeFailureFile("x")');await tick();f.calls.starts[0].resolve('{"ok":false,"error":"busy"}');await p;
  assert.equal(f.run('failurePolicyBlocked()'),false);assert.equal(f.get('failurePolicyPanel').hidden,true);
});
test('bulk and single exclusions use the same asynchronous admission with exact path strings',async()=>{
  const f=fixture();f.context.paths=['C:\\Users\\A\\quote\'s.docx','other.txt'];
  f.run('_failCards=paths.map(path=>({path,excluded:false,kind:"UNKNOWN_TRANSIENT"}))');
  const p=f.run('runBulkFailureAction()');await tick();assert.deepEqual(f.calls.starts[0].paths,f.context.paths);
  f.calls.starts[0].resolve('{"ok":false,"error":"busy"}');await p;
  assert.equal(f.calls.legacy,0);
});
test('lost acknowledgement queries authoritative state and never resubmits a write',async()=>{
  const f=fixture();const p=f.run('excludeFailureFile("x")');await tick();f.setBackend(state());f.calls.starts[0].reject(new Error('lost ACK'));await p;
  assert.equal(f.calls.starts.length,1);assert.equal(f.calls.status,1);assert.equal(f.run('failurePolicyBlocked()'),true);
  assert.equal(f.run('_failurePolicyState.job_id'),'job-a');
});
test('an acknowledgement timeout queries status while keeping execution closed',async()=>{
  const f=fixture();const p=f.run('excludeFailureFile("x")');await tick();f.setBackend(state());
  [...f.timers.values()].find(timer=>timer.ms===3000).fn();await p;
  assert.equal(f.calls.starts.length,1);assert.equal(f.calls.status,1);assert.equal(f.run('failurePolicyBlocked()'),true);
});
test('a failed status read keeps controls closed and retry reads status without writing',async()=>{
  const f=fixture();f.bridge.getFailurePolicyJobStatus=()=>Promise.reject(new Error('offline'));
  await f.run('refreshFailurePolicyStatus()');assert.equal(f.run('failurePolicyBlocked()'),true);
  assert.equal(f.get('failurePolicyPanelRecheck').hidden,false);
  f.bridge.getFailurePolicyJobStatus=()=>Promise.resolve(JSON.stringify(idle()));await f.run('recheckFailurePolicy()');
  assert.equal(f.calls.starts.length,0);assert.equal(f.run('failurePolicyBlocked()'),false);
});
test('an old job notification cannot unlock a new request with an unknown acknowledgement',async()=>{
  const f=fixture();f.apply(state('old',{revision:5,active:false,terminal:true,stage:'complete'}),false);
  const p=f.run('excludeFailureFile("new.txt")');await tick();
  f.bridge.getFailurePolicyJobStatus=()=>Promise.reject(new Error('offline'));
  f.calls.starts[0].reject(new Error('lost new ACK'));await p;
  assert.equal(f.run('failurePolicyBlocked()'),true);
  f.apply(state('old',{revision:6,active:false,terminal:true,stage:'complete'}));
  assert.equal(f.run('failurePolicyBlocked()'),true);
  f.bridge.getFailurePolicyJobStatus=()=>Promise.resolve(JSON.stringify(state('new')));
  await f.run('recheckFailurePolicy()');assert.equal(f.run('_failurePolicyState.job_id'),'new');
  assert.equal(f.calls.starts.length,1);
});
test('an old status response cannot replace a newer live terminal',async()=>{
  const f=fixture();f.apply(state());const d=deferred();f.bridge.getFailurePolicyJobStatus=()=>d.promise;
  const p=f.run('refreshFailurePolicyStatus()');
  f.apply(state('job-a',{revision:9,active:false,terminal:true,stage:'complete',result:{changed:150}}));d.resolve(JSON.stringify(state()));await p;
  assert.equal(f.run('failurePolicyBlocked()'),false);assert.equal(f.run('_failurePolicyState.revision'),9);
});
test('a previously retired job never overwrites the current one',()=>{
  const f=fixture();f.apply(state('old',{revision:9,active:false,terminal:true,stage:'complete'}));f.apply(state('new'));
  f.apply(state('old',{revision:99,active:false,terminal:true,stage:'complete'}));
  assert.equal(f.run('_failurePolicyState.job_id'),'new');assert.equal(f.run('failurePolicyBlocked()'),true);
});
test('mail counting and finalization stages do not display fake numeric completion',()=>{
  const f=fixture();for(const stage of ['mail','counts','finishing']) {
    f.apply(state('job-a',{revision:2+['mail','counts','finishing'].indexOf(stage),stage,done:150}));
    assert.equal(f.get('failurePolicyPanelCount').textContent,'');assert.equal(f.get('failurePolicyPanelTrack').classList.contains('indeterminate'),true);
  }
});
(async()=>{for(const {name,check} of tests){await check();console.log('PASS '+name);}console.log(tests.length+' exclusion policy frontend scenarios passed');})().catch(error=>{console.error(error);process.exitCode=1;});
