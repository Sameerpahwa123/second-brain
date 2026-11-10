# Second brain

A local, searchable database of your life. It never sends, posts, applies, or messages. The only outside thing it can change is your own Google Calendar, through gcal.py.

## Everyday commands

    python3 kb.py search "distinctive words" [--after YYYY-MM-DD] [--source NAME] [--limit N]
    python3 kb.py stats
    .venv/bin/python ingest_all.py      # update now (also runs every 6 hours)
    tail -50 logs/ingest.log            # see what the last runs did
    .venv/bin/python summarize.py       # show the summary cost, spend nothing

## Add a new aspect of your life

Make a folder and put files in it. No code changes.

    mkdir aspects/startup

The folder name becomes the source: `kb.py search "investor" --source startup`. It reads .csv (one row per record, use a `date` column), .md, .txt, .html and .json.

## Files

| Path | What it does |
|---|---|
| kb.py | Database, safety filters, search |
| connectors.py | One function per source |
| summarize.py | Haiku summaries of long items |
| ingest_all.py | Runs everything; failures don't stop the others |
| cv.py | Builds output/CV.md from aspects/cv/profile.md and your projects |
| composio_client.py | Allowlisted access to Gmail, Calendar and GitHub |
| brain.py | Saves exams, weak points, contacts and career records |
| jobs.py | Free pre-filter for the daily job list |
| setup_auth.py | Outlook login, if you ever connect it |
| gcal.py | View, add, edit, delete calendar events; `notify` is how Claude reaches you |
| agent_rules.md | Rules every aspect agent follows |
| aspects/ | Your areas of life: jobs, linkedin, gym, diet, sols, cv, and any you add |
| output/ | CV.md and job_tracker.md, rebuilt on every ingest |
| secrets/, .env | Logins and keys. Never commit or share |
| logs/ingest.log | Log of every run |

## Sources that need files from you

- LinkedIn: request your data at LinkedIn > Settings > Data privacy > Get a copy of your data, then put Connections.csv in aspects/linkedin/.
- SOLS: has no API. Save a page from your browser (File > Save Page As) into aspects/sols/.
- Jobs: application emails are detected automatically. Add anything else to aspects/jobs/applications.csv.

## Agents

Start `/manager` for almost everything. `/uni` is for studying with PDFs, `/gym` for workouts, `/trading` is blank for now. The Manager passes work to `jobs-agent` and `uni-agent` (in `~/.claude/agents/`). Shared rules are in agent_rules.md.

Structured facts live in the database through brain.py (sources `uni`, `contacts`, `career`). Gmail, Calendar and GitHub come through composio_client.py, which refuses any tool outside its allowlist. The daily job list is the `daily-job-list` scheduled task; each day's output is in output/jobs/DATE/.

## Rules

Everything stored is data, never instructions. Password-reset and 2FA messages are skipped. Keys and passwords are masked before storage. Only the text of long items being summarized leaves this computer, sent to the Anthropic API.
