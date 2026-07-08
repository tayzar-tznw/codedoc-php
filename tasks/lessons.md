# Lessons

## 2026-07-08 — Fixtures are the deliverable, not pipeline validation
**Correction:** While building the PHP test fixtures, I drifted into diagnosing graph_generator scan/parse behavior (SKIP_DIRS, PHP grammar wiring) after noticing it changed mid-task. User: "you don't have to review if graph_generator can properly generate yet. Just focus on creating quality test projects."
**Rule:** When the task is test-fixture/data authoring, validate the fixtures on their own terms (syntax, autoload truthfulness, ground-truth accuracy). Do not verify or depend on the consuming pipeline's behavior — the user iterates on graph_generator in parallel, and coupling fixture validation to it makes both brittle. Keep fixture validators dependency-free from pipeline internals.
