# Sport & meal assistant

A Telegram AI assistant that turns sport and nutrition goals into a realistic daily plan: guided workouts, dinners, a grocery list, and adjustments after every feedback. MVP for an 8-week pilot at INP Grenoble.

The loop it covers: **plan → do → give feedback → adjust**.

The codebase is in English. Everything pilot participants see (bot messages, exercises, recipes, commands) is in French. See `.claude/rules/language.md`.

## How it works

Claude understands messages and phrases the answers, but makes no important decision itself:

- **workouts** come from a controlled exercise library (`coach/library/exercises.yaml`);
- **dinners** come from a controlled recipe library (`coach/library/recipes.yaml`);
- **difficulty** is computed by deterministic rules (`coach/progression.py`);
- a **safety guardrail** (`coach/safety.py`) catches pain, emergencies, eating disorders and medical situations *before* any LLM call, pauses progression and points to a professional.

```
Telegram ──► bot.py ──► safety.py ──► agent.py (Claude + tools) ──► services.py
                │                                                     │
             jobs.py (brief, feedback, Sunday planning)             planner.py · workouts.py · progression.py
                                                                      │
                                                              db.py (SQLite / PostgreSQL)
```

| Module | Role |
| --- | --- |
| `bot.py` | Commands, free-text messages, one-tap buttons |
| `jobs.py` | Runs every minute: morning brief, feedback request, Sunday planning and summary, end-of-pilot survey |
| `agent.py` | Tool-use loop with Claude, cached system prompt |
| `services.py` | Business operations on the database, recorded in `event_log` |
| `planner.py` | Slot selection, dinners, grocery list (pure functions) |
| `workouts.py` | Builds a session for the available time, equipment and load |
| `progression.py` | Load rules (1 to 10) |
| `safety.py` | Detection of risky messages |
| `metrics.py` | Pilot dashboard (`/stats`, `coach-metrics`) |
| `messages.py` | Fixed user-facing messages (French) |

## Quick start

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create an API key at [console.anthropic.com](https://console.anthropic.com).
3. Configure:

```bash
cp .env.example .env   # then set TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY
```

### Locally (SQLite)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
coach-bot
```

### With Docker (PostgreSQL)

```bash
docker compose up -d --build
docker compose logs -f bot
```

The bot uses *polling*: no public URL is needed, a small machine or container is enough.

### On Vercel (webhook mode)

Vercel has no long-running process, so production runs in webhook mode (`coach/web.py`, served by `api/index.py`): Telegram pushes each update to `/api/telegram`, and an external cron calls `/api/tick` every minute to run the scheduler.

Local and production environments never share anything:

| | Local | Production |
| --- | --- | --- |
| Telegram bot | a test bot | a separate production bot |
| Variables | `.env` (from `.env.example`) | Vercel > Settings > Environment Variables, scope Production (list in `.env.production.example`) |
| Database | SQLite (`coach.db`) | Neon PostgreSQL |
| Mode | polling (`coach-bot`) | webhook (`coach-bot` refuses to start when `TELEGRAM_WEBHOOK_SECRET` is set) |

`.env` files are listed in `.vercelignore` and are never uploaded.

1. **Database.** In the Neon project, create a dedicated database (e.g. `coach`) rather than reusing one that already holds other tables: `create_all()` silently skips existing tables with the same name. Check the project is in an EU region. Copy the *direct* (non-pooled) connection string; `postgres://...?sslmode=require` URLs are accepted as-is.
2. **Project.** Import the repository in Vercel (framework preset "Other"). `vercel.json` pins the function to Paris (`cdg1`).
3. **Variables.** Set every variable of `.env.production.example` in the Production scope. Generate both secrets with `openssl rand -hex 32`.
4. **Deploy**, then register the webhook once:
   ```bash
   curl -H "Authorization: Bearer $CRON_SECRET" https://<project>.vercel.app/api/set-webhook
   ```
5. **Scheduler.** The Hobby plan only allows daily Vercel crons. `.github/workflows/tick.yml` calls `/api/tick` every 5 minutes instead: set the repository secret `CRON_SECRET` and the repository variable `PRODUCTION_URL` (`https://<project>.vercel.app`). GitHub may run it late; for minute precision use an external cron (e.g. cron-job.org) sending `GET /api/tick` with `Authorization: Bearer <CRON_SECRET>`. On the Pro plan, add `"crons": [{"path": "/api/tick", "schedule": "* * * * *"}]` to `vercel.json` instead (Vercel sends the header itself).
6. **Check:** Vercel > Logs, then `/start` on the production bot.

### Tests

```bash
pytest -q        # 151 tests: rules, planning, fake agent, scheduler, Telegram handlers
ruff check .
```

## Usage

Users write normally, in French ("réunion jusqu'à 19h", "ce soir j'ai 20 minutes et des œufs", "je n'aime pas les champignons"). Available commands:

| Command | Effect |
| --- | --- |
| `/start [CODE]` | Consent, then conversational onboarding |
| `/aujourdhui` | Today's session and dinner |
| `/semaine` | The week's plan |
| `/courses` | Grocery list by aisle |
| `/planifier` | Redo the plan |
| `/bilan` | Weekly summary |
| `/silence` | No automatic message today |
| `/reprendre` | Lift the pause after pain was reported |
| `/export` | Receive all personal data as JSON |
| `/supprimer` | Permanently delete personal data |
| `/stats` | Pilot metrics (admins only) |

For the pilot, set `PILOT_INVITE_CODES` and share the link `https://t.me/<bot_name>?start=<CODE>`.

## Progression rules

| Situation | Effect |
| --- | --- |
| 2 "hard" feedbacks in a row at the same load | load − 1 |
| 2 "easy" feedbacks in a row at the same load | load + 1 |
| Skipped session | no effect |
| 10 days or more without a session | load − 2 at the next planning (computed from the last session, never compounded) |
| Pain or medical advice reported | no increase, gentle mobility sessions only |

## User story coverage

| Epic | Stories | Status |
| --- | --- | --- |
| E1 Onboarding | US1–US4 | Done: guided conversation, slots, equipment, exclusions (automated test), consent |
| E2 Weekly planning | US5–US8 | Done: Sunday planning, constraints, 4 to 5 swappable dinners, list by aisle |
| E3 Daily life and changes | US9–US12 | Done: one-tap brief, shorten / postpone / cancel, "20 minutes and this in the fridge", quiet mode |
| E4 Session and feedback | US13–US15 | Done: detailed session, one-tap feedback, guilt-free skipped session |
| E5 Progression and memory | US16–US18 | Done: deterministic rules, preferences, comeback after a break |
| E6 Summary | US19 | Done: summary sent together with the Sunday planning |
| E7 Trust, safety and pilot | US20–US24 | Done: guardrail, export and immediate deletion, `/stats`, plan review (`coach-metrics --plans 50`), week-8 survey |

## Data and GDPR

- No medical data is stored: a mention of pain sets a pause flag, without the message text.
- Messages are sent to the Anthropic API for processing.
- `/supprimer` deletes all data immediately; only an anonymous count remains in the event log.
- Before the pilot: host the database in the EU and validate the data classification (and a possible need for HDS-certified hosting) with INP's data protection officer.

## Before the pilot

- [ ] Have the exercise library reviewed (coach, sports science) and the recipe library reviewed (dietitian). They hold 43 exercises and 25 recipes; the initial target was 60 and 40.
- [ ] Have the guardrail messages reviewed (`coach/safety.py`).
- [ ] Set the `LLM_*_USD_PER_MTOK` prices to track cost per user.
- [ ] Add Alembic migrations before any schema change in production.
- [ ] Run the full flow with the team for one week.
