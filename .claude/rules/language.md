# Language

The repository is written in English. The product speaks French to its users.

## English

Everything a developer or operator reads is in English:

- code identifiers: variables, functions, classes, modules, enum values, tags, ids (exercise and recipe ids included);
- comments, docstrings, type hints, log messages, exceptions meant for developers;
- tests: names, docstrings, comments;
- documentation: README, `.claude/` files, YAML headers and field names;
- prompts and tool descriptions sent to Claude in `coach/agent.py` (the system prompt tells Claude to answer users in French);
- operator-facing output: `coach-metrics`, `/stats`, CLI help;
- commit messages and pull request descriptions.

## French

Everything a pilot participant sees stays in French:

- bot messages and button labels (`coach/messages.py`, `coach/bot.py`);
- Telegram command names (`/aujourdhui`, `/semaine`, `/courses`...);
- exercise `name` and `cue`, recipe `name`, ingredient names and `steps` in `coach/library/*.yaml`;
- display labels (aisles, focus labels, day and month names, units);
- `ServiceError` messages, which can be shown to users as-is;
- safety guardrail patterns and replies in `coach/safety.py` (patterns match French input);
- free-form food terms accepted from users (`EXCLUSION_ALIASES` keys).

When a French string sits in English code, keep it inside a clearly named user-facing constant or function, with a short English comment saying it is user-facing. Never translate user-facing text to English, and never put French in identifiers.
