# StateForge

> **Maintained docs**: the authoritative, up-to-date project documentation is
> the repository-root [README.md](../README.md) (stages, modules, frozen
> results) and [docs/architecture/](../docs/architecture/) (audit, state
> contract v1.1b, integration plan, decision log). This file keeps the
> original English overview of the static-structural core; see "Status" below
> for what has landed since.

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
python -m stateforge.cli analyze targets/vulnshop --out results/vulnshop
```

The command writes `program_ir.json` and `summary.json`. The IR is the shared
contract for tracing, feedback, and visualisation; it should be built once and
reused for an experiment.

Map a trace artifact produced by `stateforge.fuzzing.harness`:

```bash
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

Extract session-state variables, predicates, abstract domains, and static
transition candidates (dynamic edges are recorded by `TransitionRecorder`
at fuzz time):

```bash
python -m stateforge.cli state targets/vulnshop --out results/state.json
```

## Python integration

The Atheris boundary is intentionally thin:

```python
from stateforge.fuzzing.harness import run_one

result = run_one(data, target_function, root=target_root)
result.save_trace("trace.json")
```

Use `stateforge.fuzzing.feedback.corpus.evaluate_seed` to attach a structural score to
each executed seed and `stateforge.fuzzing.atheris_runner.rank_corpus` to produce
deterministic corpus ordering metadata. Beyond this thin boundary, the fuzzing
loop extensions (epoch campaigns, frontier-directed feedback, sequence seeds,
state/goal seed injection) live in `stateforge/fuzzing/` and
`stateforge/evaluation/experiment/` — see the root README stage table.

## Status (2026-10-19)

The static-structural pipeline above is the frozen Phase 1–3 core. Since
then, all gated behind explicit flags (default experiment behavior
unchanged): input-level state isolation and runtime state evidence
(P0-A/P0-B), and the **No-LLM relational goal line** — relational constraint
IR (`state/relational.py`), entity provenance tracking
(`state/provenance.py`), joint goal evaluation with single-point observation
views and achievement witnesses (`state/goal_evaluation.py`), fixed
intra-function CFG reachability in `state/goal_derivation.py`, and WAL-safe
SQLite baselines (`runtime/snapshot/l2_sqlite.py`, backup/copy capture
modes). Contract and plan: `docs/architecture/`. Full suite: 601 passed.

## Layout

```text
core/           ProgramIR and artifact serialization
analysis/       static structure analysis: CFG/ICFG (analysis/static/),
                semantic analyzer, SSTG graph builder/export
runtime/        ordered trace collection, mapping, coverage, replay,
                input-level execution context (lifecycle + observers),
                snapshot subpackage (runtime/snapshot/, backup/copy modes)
fuzzing/        thin Atheris boundary and harness
fuzzing/feedback/  overlay, frontier, distance, scoring, corpus metadata
solving/        branch-feasibility solving; LLM subpackage (solving/llm/)
state/          state extraction, annotation, planning (state/planning/);
                relational goal line: relational.py (IR + evaluator),
                provenance.py (entity origins), goal_evaluation.py
                (joint evaluation + witnesses)
adapters/       target adapters (former fuzz/adapters/)
evaluation/     benchmarks, experiment subpackage, viz subpackage
```

The state graph, LLM lifting, snapshot, and request-generation capabilities
live in their domains (`solving/llm/`, `state/`, `runtime/snapshot/`) and
coexist with the CLI main chain. They are delivered code; a subset is not yet
wired into CLI subcommands.
