# StateForge

StateForge is a static-dynamic structural feedback layer for Atheris. Atheris
owns input mutation, execution, and the native fuzzing loop. StateForge builds
the program structure and reports where each input lands in that structure.

The first-stage pipeline is deliberately small:

```text
Python source
  -> Basic-block ICFG (ProgramIR)
  -> ordered runtime trace
  -> static/dynamic overlay
  -> covered-to-uncovered frontier
  -> directed ICFG distance
  -> seed/corpus score
  -> corpus metadata for Atheris
```

## Commands

Build a stable static artifact:

```bash
PYTHONPATH=/home/landasika/icassp \
python -m stateforge.cli analyze targets/vulnshop --out results/vulnshop
```

The command writes `program_ir.json` and `summary.json`. The IR is the shared
contract for tracing, feedback, and visualisation; it should be built once and
reused for an experiment.

Map a trace artifact produced by `stateforge.fuzz.harness`:

```bash
PYTHONPATH=/home/landasika/icassp \
python -m stateforge.cli feedback \
  results/vulnshop/program_ir.json trace-1.json trace-2.json \
  --out results/vulnshop/feedback.json
```

`trace.json` may be either a list of `TraceEvent` objects or a harness result
object containing an `events` list. Multiple trace files are merged as
independent request/seed runs. Feedback includes basic-block, edge, and
call-edge coverage, frontiers, distances, and dynamic edge kinds.

The combined command builds the static artifact and optionally consumes a
trace:

```bash
python -m stateforge.cli experiment targets/vulnshop \
  --out results/vulnshop --trace trace.json
```

## Python integration

The Atheris boundary is intentionally thin:

```python
from stateforge.fuzz.harness import run_one

result = run_one(data, target_function, root=target_root)
result.save_trace("trace.json")
```

Use `stateforge.feedback.corpus.evaluate_seed` to attach a structural score to
each executed seed and `stateforge.fuzz.atheris_runner.rank_corpus` to produce
deterministic corpus ordering metadata. No mutation or replacement fuzz loop is
implemented in StateForge.

## Layout

```text
core/       ProgramIR and artifact serialization
static/     AST basic-block CFG and project ICFG
dynamic/    ordered trace collection, mapping, coverage
feedback/   overlay, frontier, distance, scoring, corpus metadata
fuzz/       thin Atheris boundary and harness
```

The former state graph, LLM lifting, snapshot, and request-generation code is
not part of the structural feedback path. It remains outside the CLI as legacy
compatibility code so first-stage experiments have one reproducible algorithmic
boundary.
