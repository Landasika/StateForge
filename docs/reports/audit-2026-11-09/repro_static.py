import ast, json, tempfile
from pathlib import Path
from stateforge.analysis.static.icfg import build_icfg
from stateforge.analysis.static.cfg import build_function_cfg
from stateforge.runtime.tracer import TraceSession
from stateforge.runtime.mapper import map_trace
from stateforge.evaluation.experiment.epoch import _novel_seed_gain, run_epochs
from stateforge.fuzzing.corpus_manager import CorpusManager
from unittest.mock import patch

root=Path(tempfile.mkdtemp(prefix='sf-audit-'))
def case(name,src,invoke):
 d=root/name; d.mkdir(); p=d/'app.py'; p.write_text(src)
 ir=build_icfg(d); ns={}; exec(compile(src,str(p),'exec'),ns)
 with TraceSession(d) as s: invoke(ns)
 mt=map_trace(s.events,ir)
 return ir,mt,s

ir,mt,s=case('methods','class A:\n    def run(self):\n        return 1\nclass B:\n    def run(self):\n        return 2\n',lambda ns:ns['B']().run())
print('METHOD_ENTRIES',[(k,v.entry) for k,v in ir.functions.items()])
print('METHOD_EXECUTED_B_MAPPED',mt.blocks)
print('METHOD_UNEXECUTED_A_ENTRY_COVERED',ir.functions['app.A.run'].entry in mt.block_hits)
print('METHOD_CALL_EDGES',[(e.src,e.callee) for e in ir.edges if e.kind=='call'])

ir,mt,s=case('samecall','def f():\n    return 1\ndef h():\n    f(); f()\n',lambda ns:ns['h']())
print('SAMECALL_SYNTHETIC',[(ir.blocks[b].kind,b) for b in mt.blocks if ir.blocks[b].kind in ('callsite','returnsite')])
print('SAMECALL_RETURNS',[(a,b) for a,b in mt.edges if ir.blocks[a].kind=='exit' and ir.blocks[b].kind=='returnsite'])

ir,mt,s=case('repeat','def f():\n    return 1\ndef h():\n    for i in range(3):\n        f()\n',lambda ns:ns['h']())
print('LOOP_REAL_F_CALLS',sum(e.event_type=='call' and e.function=='f' for e in s.events))
print('LOOP_CS_HITS',sum(mt.block_hits[b] for b in mt.block_hits if ir.blocks[b].kind=='callsite'))
print('LOOP_UNMATCHED',len(mt.unmatched_transitions))

src='def h():\n    try:\n        return 1\n    finally:\n        cleanup = 2\n'
ir,mt,s=case('finally',src,lambda ns:ns['h']())
print('FINALLY_EDGES',[(ir.blocks[e.src].start_line,ir.blocks[e.src].kind,ir.blocks[e.dst].start_line,ir.blocks[e.dst].kind) for e in ir.edges])
print('FINALLY_UNMATCHED',[(ir.blocks[a].start_line,ir.blocks[b].start_line) for a,b in mt.unmatched_transitions])

# nsg after the controller's public reinjection has copied SF candidates into native corpus
wd=root/'nsg'; cm=CorpusManager(wd/'epochs'/'spool'); cm.save(b'novel-sf-seed',1,1,0,1,0)
(wd/'lf_corpus').mkdir(); imported=wd/'corp_in_e2'; cm.export_seed_dir(imported,extra=[wd/'lf_corpus'])
import shutil
for f in imported.iterdir(): shutil.copy2(f,wd/'lf_corpus'/f.name)
print('NSG_IMPORTED_ONLY',_novel_seed_gain(wd))

# Existing experiment output is silently reused; fake only the actual subprocess boundary.
wd=root/'reuse'; (wd/'lf_corpus').mkdir(parents=True); (wd/'lf_corpus'/'prior-seed').write_bytes(b'old-secret')
(wd/'epochs').mkdir(); (wd/'epochs'/'global_state.json').write_text(json.dumps({'blocks':['prior-only-block'],'edges':[]}))
seen={}
def fake_driver(target,ir,corpus_dir,out_dir,seconds,mode,seed,group,epoch):
 seen['old_corpus']=(corpus_dir/'prior-seed').exists(); seen['old_state']=json.loads((out_dir/'global_state.json').read_text())
 (out_dir/f'{group}-e{epoch}-snapshots.jsonl').write_text(json.dumps({'executions':1,'blocks':1,'edges':0})+'\n')
with patch('stateforge.evaluation.experiment.epoch._run_driver',fake_driver):
 run_epochs('unused','unused',wd,1,1,7,'B','measure',False)
print('NEW_RUN_REUSED',seen)
# With bodies are flattened into a single opaque block, so branch lines have no CFG node.
src='def h(cm, flag):\n    with cm:\n        if flag:\n            return 1\n        return 0\n'
from contextlib import nullcontext
ir,mt,s=case('withbody',src,lambda ns:ns['h'](nullcontext(),True))
print('WITH_BRANCH_COUNT',sum(b.kind=='branch' for b in ir.blocks.values()))
print('WITH_UNRESOLVED_LINES',[(e.line,e.event_type) for e in mt.unresolved])

src='def helper():\n    return 1\nclass A:\n    def run(self):\n        return helper()\n'
ir,mt,s=case('methodcalls',src,lambda ns:ns['A']().run())
print('METHOD_HELPER_STATIC_CALLS',[(e.src,e.callee) for e in ir.edges if e.kind=='call'])
print('METHOD_HELPER_REAL_CALLS',sum(e.function=='helper' and e.event_type=='call' for e in s.events))

# Only the first branch operand executes; ICFG unconditionally chains both operands.
src='def left():\n    return False\ndef right():\n    return True\ndef h():\n    if left() and right():\n        return 1\n    return 0\n'
ir,mt,s=case('shortcircuit',src,lambda ns:ns['h']())
print('SHORT_CIRCUIT_REAL_CALLS',[e.function for e in s.events if e.event_type=='call'])
print('SHORT_CIRCUIT_STATIC_CHAIN',[(ir.blocks[e.src].kind,ir.blocks[e.dst].kind,e.callee) for e in ir.edges if ir.blocks[e.src].function=='h'])
print('SHORT_CIRCUIT_UNMATCHED',[(ir.blocks[a].kind,ir.blocks[b].kind,ir.blocks[b].start_line) for a,b in mt.unmatched_transitions])
from stateforge.fuzzing.feedback.engine import compute_feedback
r=compute_feedback(ir,s.events)
print('OVERLAY_NONSTATIC_EDGE_COUNT',len(r.overlay.covered_edges-set((e.src,e.dst) for e in ir.edges)))
from stateforge.fuzzing.feedback.distance_oracle import DistanceOracle
from stateforge.core.ir import ProgramIR, BasicBlock, FlowEdge
mini=ProgramIR(blocks={x:BasicBlock(x,'x.py','x','h',1,1) for x in ('s','a','b')},edges=[FlowEdge('s','a','true'),FlowEdge('s','b','false')])
o=DistanceOracle(mini); o.rebuild([('s','a'),('s','b')],1)
print('ORACLE_TWO_FRONTIERS_ONLY_NEAREST',o.distance({'s'}),o._nearest)
d=root/'crosscaller'; d.mkdir()
sources={'lib.py':'def f():\n    return 1\n','a.py':'def a():\n    return f()\n','b.py':'def b():\n    return f()\n'}
ns={}
for name,src in sources.items():
 p=d/name;p.write_text(src);exec(compile(src,str(p),'exec'),ns)
ir=build_icfg(d)
with TraceSession(d) as s: ns['a']()
mt=map_trace(s.events,ir)
print('CROSSCALLER_ONLY_A_RETURN_SITES',[b for b in mt.blocks if ir.blocks[b].kind=='returnsite'])
print('CROSSCALLER_ONLY_A_CALL_SITES',[b for b in mt.blocks if ir.blocks[b].kind=='callsite'])
print('ROOT',root)
