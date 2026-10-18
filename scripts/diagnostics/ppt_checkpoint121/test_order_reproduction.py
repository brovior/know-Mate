import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from knowmate.tests.test_ppt_identity_observation import Fake, ComFailure
from scripts.ppt_reader.ppt_identity_controller import observe_export_prepare

class KnownGetterFailure(Fake):
    def get(self,value,name):
        self.calls.append(name)
        if name=='Application' and len(self.calls)==1:raise ComFailure(-2147352570)
        if name=='Application' and len(self.calls)==4:raise ComFailure(-2147352567,-2147467259)
        return value

def test_exact_121_getter_failure_blocks_host_before_observation():
    app=object();backend=KnownGetterFailure();host=[];prepare=[];exports=[]
    result=observe_export_prepare(app,('nonce',app),'fixture',backend,lambda x:host.append(x),exports.append,lambda *x:prepare.append(x))
    assert backend.calls==['Application','COMAddIns','Item','Application']
    assert host==prepare==[] and result['prepare_calls']==0
    assert result['controller']['primary']==dict(row='COMAddIns.Item.Application',operation='Application',outcome='unknown',hresult=-2147352567,inner_hresult=-2147467259)
    assert all(r['status']=='not_run' for r in exports[0]['host']['rows'])
    assert result['controller']['candidate_same_as_rot'] is True

def test_required_identity_failure_blocks_all_getters_and_host():
    class BrokenCore(Fake):
        def canonical(self,value):raise ComFailure(-2147352567,-2147467259)
    app=object();backend=BrokenCore();host=[];prepare=[];exports=[]
    result=observe_export_prepare(app,('nonce',app),'fixture',backend,lambda x:host.append(x),exports.append,lambda *x:prepare.append(x))
    assert backend.calls==host==prepare==[]
    assert result['controller']['primary']['operation']=='canonical_IUnknown'
    assert result['prepare_calls']==0 and result['exported']
