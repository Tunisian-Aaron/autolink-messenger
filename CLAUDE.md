# Working on AutoLink Messenger with Claude Code

This file is read by Claude Code when you open this folder in it. It tells Claude how to run
the tool for you and how to change its code. People: see "Using it with Claude Code" in the
README for what to ask.

## What this is

A local tool that messages the user's LinkedIn connections from their own account:
`outreach.py` (all commands), `linkedin_browser.py` (the browser automation; every LinkedIn
selector is at the top), `webapp.py` + `web/index.html` (the local page), `start.py` (setup).
Data is in `outreach.db` (SQLite) next to the code.

## Running it for the user

Set up once with `python3 start.py --setup-only`, then call commands with the tool's own
Python: `.venv/bin/python outreach.py <command>` (Windows: `.venv\Scripts\python`).
`outreach.py check` says what is set up; `outreach.py status` shows every campaign and how
many messages went out in the last 24 hours. `outreach.py --help` lists the commands.

The usual order: `import-linkedin` (or `import Connections.csv`) → `list` with filters →
`draft <campaign> --template FILE --dry-run`, then without `--dry-run` → `review` →
`autosend <campaign> --dry-run --max 3` → `autosend <campaign> --max N --yes` → `replies`.

Rules when you run it:

- **Sending needs the user's go-ahead, every time.** Before a real `autosend`, show the
  message text, who it goes to and how many, and wait for a clear yes. A yes covers that run
  only. Do a `--dry-run` first on any new campaign.
- **Keep it small.** The default is 20 messages per rolling 24 hours across all campaigns.
  Raise `--daily-cap` only when the user asks for it, for that run. Check `status` first.
- **Don't read profiles in bulk.** `enrich`, `personalize --enrich` and
  `autosend --read-profiles` open three pages per person, and LinkedIn limits profile views.
  Use them only when asked, on small batches.
- **Only people the user names.** Never have the browser search LinkedIn for people. New
  people come from profile links the user gives (`add-prospects`).
- **Mark people the user is already talking to elsewhere** as do-not-contact (`dnc IDS`)
  before a run.
- **A run takes 1 to 3 minutes per message.** Start it in the background with its output in a
  log file (`PYTHONUNBUFFERED=1 ... > run.log 2>&1`) and read the log; don't schedule it with
  a shell `sleep`, which dies with the session. Stop a run with Ctrl-C (`kill -INT`);
  everything sent so far is recorded, and the next run carries on from there.
- **`STOPPED:` in the output means stop.** A login wall, security check or restriction
  notice ends the run. Tell the user what it said. Don't retry, don't work around it, never
  solve a CAPTCHA, and never type the user's LinkedIn password: they log in themselves in the
  browser window.
- **Never disguise the automation** (no stealth plugins, fake fingerprints or proxy rotation).
- **"held for you" means the person is waiting on the user's own reply.** Those messages are
  marked skipped on purpose. List them for the user instead of sending anything.
- **Failed messages are never resent automatically** once Send was pressed. Look at the
  screenshot in `failures/` and ask before requeueing (`mark <campaign> IDS draft`).
- **The user's data is private.** Don't send contacts, messages or the contents of
  `outreach.db` to any outside service, and don't paste them into issues or commits.

## Changing the code

- Run the tests: `.venv/bin/python -m unittest discover -s tests` (a few minutes; the browser
  tests drive a local mock of LinkedIn). Never test against the real LinkedIn.
- When LinkedIn changes its pages, fix the selectors at the top of `linkedin_browser.py` and
  add the new markup to the mock in the tests.
- Keep the safety behaviour: stop on any block notice, check the conversation names the person
  before typing, never retry after Send was pressed, never send an invitation without its note
  unless asked.
- Keep it general. No real names, companies, profile links or message text in code, tests,
  templates or commit messages: use made-up ones (Jane Doe, Acme).
- Never commit `outreach.db`, `.env`, `Connections.csv`, `failures/`, logs, or anything from
  the browser profile folder. `.gitignore` covers them; check `git status` before committing.
- Standard library only, plus Playwright and the Anthropic SDK. When the schema changes, extend
  `migrate()` in `outreach.py` and raise `SCHEMA_VERSION`.
