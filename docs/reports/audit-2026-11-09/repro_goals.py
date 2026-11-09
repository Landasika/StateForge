import json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from stateforge.state.goal_evaluation import ObservationView, BindingEnv, evaluate_goal
from stateforge.state.runtime_observer import StateObservation, StateKey, Evidence
from stateforge.state.relational import Field, Const, RelOp, RelPredicate, VarProvenance
from stateforge.state.goal_derivation import StateGoal

def obs(subject, entity, val, *, truth=True, kind='STRING', source='db:users'):
    return StateObservation(StateKey(subject, entity, 'user'), truth,
        Evidence.RUNTIME_VERIFIED, source, subject, val, kind, 0)

def auth_conflict():
    goal=StateGoal('target', 'f', 'views.py', (), relations=(RelPredicate(RelOp.EQ, Field('?user','id'),Const(7)),), var_provenance=(VarProvenance('?user','users',auth_subject=True),))
    user=obs('User.id','bob',7,kind='NUMBER')
    a=obs('RequestAuth.subject','req','alice',source='probe:auth')
    b=obs('RequestAuth.subject','req','bob',source='probe:auth')
    for label, seq in [('alice,bob',[user,a,b]),('bob,alice',[user,b,a])]:
        v=ObservationView.from_observations(seq)
        r=evaluate_goal(goal,v,BindingEnv(fixed={'?user':['bob']}),trace_covered=True)
        print('AUTH_CONFLICT',label,'accepted_subject='+str(v.request_auth_subject),'status='+r.achievement_status,'witness='+str(r.witness is not None))

def existence_conflict():
    rows=[obs('User.exists','bob',None,truth=False,kind=None),obs('User.id','bob',7,kind='NUMBER')]
    goal=StateGoal('target','f','views.py',(),relations=(RelPredicate(RelOp.EQ,Field('?u','id'),Const(7)),),var_provenance=(VarProvenance('?u','users'),))
    v=ObservationView.from_observations(rows)
    r=evaluate_goal(goal,v,BindingEnv(fixed={'?u':['bob']}),trace_covered=True)
    print('EXISTENCE_CONFLICT','absent='+str(sorted(v.confirmed_absent)),'snapshot='+str(v.flat_snapshot()),'status='+r.achievement_status)

def http_transport(tmp):
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.core.model import PlanSegment, PlanStep
    from stateforge.runtime.http_plan_executor import execute_app_chain
    root=tmp/'transport';root.mkdir()
    source=root/'views.py'
    source.write_text('def accept(method, data):\n    return {"status_code": 200}\n')
    import importlib.util
    spec=importlib.util.spec_from_file_location('audit_transport_views',source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    ir=build_icfg(root)
    target=next(b.id for b in ir.blocks.values() if b.function=='accept' and b.kind not in ('entry','exit'))
    class Client:
        def post(self,path,data,**extra):
            self.actual=('POST',path,data);return SimpleNamespace(**mod.accept('POST',data))
        def generic(self,method,path,data,content_type,**extra):
            self.actual=(method,path,data);return SimpleNamespace(**mod.accept(method,data))
    client=Client()
    result=execute_app_chain(PlanSegment(goal='g',steps=[PlanStep(action='DELETE /item',form={'confirm':'yes'})]), client,ir,target,root)
    print('HTTP_METHOD','actual='+str(client.actual),'recorded='+str(result.steps[0].request),'verified='+str(result.verified))

def cold_hash(tmp):
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.solving.goal_checkpoint import GoalCheckpoint
    from stateforge.solving.http_checkpoint_replay import cold_replay_http
    root=tmp/'cold';root.mkdir()
    (root/'audit_cold_views.py').write_text('def final():\n    marker = "original"\n    return 200\n')
    (root/'audit_cold_harness.py').write_text('from types import SimpleNamespace\nimport audit_cold_views\nclass Client:\n    def get(self,path,**kw):\n        return SimpleNamespace(status_code=audit_cold_views.final())\ndef build_client(authenticated=False):\n    return Client()\n')
    ir=build_icfg(root)
    target=next(b.id for b in ir.blocks.values() if b.function=='final' and b.kind not in ('entry','exit'))
    old=ir.metadata['source_hash']
    # An actual edit after analysis: a different runtime body, same source lines.
    (root/'audit_cold_views.py').write_text('def final():\n    marker = "CHANGED SOURCE AFTER CHECKPOINT"\n    return 200\n')
    ck=GoalCheckpoint('gc-audit',target,str(root/'audit_cold_views.py'),[],[{'method':'GET','path':'/final'}],source_hash=old,transport='http')
    ckp=ck.save(root/'ckpts');irp=root/'ir.json';ir.to_json(irp)
    os.environ['PYTHONPATH']=str(root)+os.pathsep+str(Path(__file__).resolve().parents[3])
    ev=cold_replay_http(ckp,irp,root/'evidence.json',harness='audit_cold_harness',app_root=root)
    from stateforge.analysis.static.icfg import _source_hash
    print('HTTP_STALE_HASH','changed='+str(old!=_source_hash(root)),'verified='+str(ev['verified']),'returncode='+str(ev['returncode']),'error='+str(ev.get('error')))

def endpoint_collision(tmp):
    from stateforge.analysis.static.icfg import build_icfg
    from stateforge.core.model import AppModel, Endpoint, Precondition, Effect
    from stateforge.state.goal_app_bridge import app_goal_constraints, plan_app_chain
    root=tmp/'collision';root.mkdir()
    (root/'a.py').write_text('def edit(request):\n    return 1\n')
    (root/'b.py').write_text('def edit(request):\n    return 2\n')
    ir=build_icfg(root)
    block=next(b.id for b in ir.blocks.values() if Path(b.file).name=='b.py' and b.kind not in ('entry','exit'))
    model=AppModel(app_root=str(root),framework='django',endpoints=[
        Endpoint(func='a.edit',route='/a/edit',methods=['GET'],file=str(root/'a.py'),preconditions=[Precondition(var='Session.authed_as',op='truthy',file=str(root/'a.py'),line=1)]),
        Endpoint(func='b.edit',route='/b/edit',methods=['GET'],file=str(root/'b.py'),preconditions=[Precondition(var='db.b.status',op='eq',value='READY',file=str(root/'b.py'),line=1)]),
        Endpoint(func='login',route='/login',methods=['POST'],effects=[Effect(var='Session.authed_as',op='set',file='login.py',line=1)])])
    constraints,seg=plan_app_chain(model,ir,block)
    print('ENDPOINT_COLLISION','target='+block,'constraints='+str([p.var for p in constraints]),'steps='+str([s.action for s in seg.steps]))

if __name__=='__main__':
    auth_conflict();existence_conflict()
    with tempfile.TemporaryDirectory(prefix='audit-sf-goals-') as t:
        tmp=Path(t);http_transport(tmp);cold_hash(tmp);endpoint_collision(tmp)
