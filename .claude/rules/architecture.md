# Architecture invariants

- Claude understands and phrases; it never decides. Workouts come from `coach/library/exercises.yaml`, dinners from `coach/library/recipes.yaml`, the training load from `coach/progression.py`. Never let the LLM invent an exercise, a recipe, a quantity or a load.
- `coach/safety.py` runs on every user message before the agent. Messages in the `crisis`, `emergency` and `eating` categories never reach the LLM. Do not weaken detection without adding tests.
- Never store medical details or the text of a message that triggered the guardrail: only the `safety_hold` flag and the category in `event_log`.
- Food exclusions are strict: no suggested recipe may contain an excluded food (US3). Keep `tests/test_core.py::test_dinners_never_contain_excluded_food` passing.
- Planning code in `planner.py`, `workouts.py` and `progression.py` is pure and deterministic for a given seed. Database access goes through `services.py`.
- Record significant changes with `log_event` so the pilot metrics stay complete.
- The scheduler (`jobs.py`) commits its de-duplication markers before sending a message.
- Every change ships with tests. Run `pytest -q` and `ruff check .` before committing.
