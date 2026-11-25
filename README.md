# AutoLink Messenger

Message your LinkedIn connections one by one, without doing it by hand: to ask
people to try a product, to open investor conversations, to invite them to
something. It imports your connections, lets you pick who to write to, drafts a
message for each person from your template, sends them from your own LinkedIn
account at a slow, human pace, and tracks who replied. It can also reach people
you aren't connected to yet, with a connection request that carries a note.

Everything runs on your computer. Your connections, messages and LinkedIn login
never leave it.

> **Read this first: account risk.** LinkedIn's User Agreement prohibits
> automated messaging, and LinkedIn can warn, restrict or ban accounts it
> catches. The defaults (20 messages a day, 1 to 3 minutes apart, no extra
> profile views) are deliberately cautious, and raising them raises the risk.
> The tool stops at once if LinkedIn shows a login wall, a security check or a
> restriction notice. It makes no attempt to hide that it's automated and
> doesn't solve CAPTCHAs. Use it on your own account, at your own risk.

## Quick start

You need **Python 3.9 or newer** ([python.org/downloads](https://www.python.org/downloads/);
Macs usually have it already) and about 500 MB of free disk space.

1. Download this folder (the green **Code** button → **Download ZIP**, then
   unzip it) or `git clone` it.
2. Open a terminal in the folder and run:

   ```bash
   python3 start.py
   ```

   On Windows: `py start.py`. The first run takes a few minutes: it installs
   what the tool needs into a `.venv` folder here, and downloads the browser it
   drives. After that it starts in a second.
3. A page opens in your browser at http://127.0.0.1:8765. It walks you through
   four tabs:

   | Tab | What you do there |
   |---|---|
   | **1. Contacts** | Import your connections from LinkedIn and filter who to message. |
   | **2. Write** | Write the message (or start from a template), preview it, create the drafts. |
   | **3. Review** | Read and edit individual messages, skip people, mark replies. |
   | **4. Send** | Do a **Test run** first (it types three messages and sends nothing), then **Start sending**. |

The first time a browser window opens on LinkedIn's login page, log in there,
including any 2FA. The tool never sees your password, and the login is
remembered on this computer. Keep the terminal window open while you use the
page; closing it stops a run. A stop always lands between two messages, never
in the middle of one.

To start it again later, run `python3 start.py` again. To update, replace the
folder's files with the new version (or `git pull`); your data stays.

### Good habits

- **Start small.** Do the test run, then send 5 real messages and look at them
  in LinkedIn before sending more.
- **Stay near 20 a day.** That is the default cap, across all campaigns.
- **Write like yourself.** A short, specific message to people who know you
  gets replies; a pitch to your whole network gets you reported.
- **Mark people you'd rather not message** with *Never contact*. They are left
  out of every campaign.

### Optional: let Claude personalize

With Claude set up, the tool can rewrite each message for the person it goes to
(and continue a conversation you already have with them, instead of
re-introducing you), and sort your connections into groups such as *founder* or
*investor*. Two ways to set it up, whichever you have:

- **An Anthropic API key.** Copy `.env.example` to `.env` in this folder and put
  the key after `ANTHROPIC_API_KEY=`. Usage is billed to that account.
- **Claude Code** installed and logged in on this computer. Nothing to
  configure; usage counts toward that plan.

Without either, everything else works and messages go out as your template
wrote them.

### Where your data lives

| What | Where |
|---|---|
| Connections, campaigns, messages | `outreach.db` in this folder (SQLite) |
| Your LinkedIn login (browser session) | `~/.outreach-linkedin-profile`. Keep it private: whoever has this folder can use your LinkedIn session |
| API key and settings | `.env` in this folder |
| Screenshots of failed sends | `failures/` in this folder |

None of these are tracked by git. To move to another computer, copy
`outreach.db` (you log in to LinkedIn again there). To start over, delete it.

### If something doesn't work

```bash
.venv/bin/python outreach.py check        # Windows: .venv\Scripts\python outreach.py check
```

prints what is set up and what is missing. The usual causes:

| What you see | What to do |
|---|---|
| "The automation browser isn't downloaded yet" | Run `python3 start.py` again; it finishes the install. |
| The browser window shows LinkedIn's login page | Log in there. The run continues by itself. |
| "STOPPED: LinkedIn is showing a ... notice" | Open LinkedIn yourself and see what it says. Don't send more until it's cleared. |
| "3 failures in a row" | LinkedIn changed its page. Look at the screenshots in `failures/`; the selectors are at the top of `linkedin_browser.py`. |
| "Can't listen on port 8765" | The page is already running: open http://127.0.0.1:8765, or start with `python3 start.py --port 8766`. |

## The command line

Everything the page does is also a command, for scripting or scheduling. The
page and the commands use the same database, so you can switch between them.
The commands below assume the tool's environment is active (`source
.venv/bin/activate`, or on Windows `.venv\Scripts\activate`); or install by
hand into your own: `pip install -r requirements.txt && playwright install
chromium`.

There are two ways to send:

- **`autosend`, fully automated.** Drives a browser that's logged in to your
  LinkedIn account. For each message it opens the profile, clicks *Message*,
  types the text and clicks *Send*, then waits 1-3 minutes before the next one.
  It stops at a daily cap. This is what the page's Send tab runs.
- **`send`, assisted.** Copies each message to your clipboard and opens the
  profile, and you press Send yourself (about 10 seconds a person). No
  automation, so no account risk.

## 1. Get your connections

LinkedIn → **Settings → Data privacy → Get a copy of your data** → choose
**Connections** → download. You get `Connections.csv` (usually within about 10
minutes). It's gitignored here, so don't commit it.

```bash
python outreach.py import ~/Downloads/Connections.csv
```

The file has names, profile URLs, company, title and connection date. Emails
appear only for people who allow it. Run `import` again any time to refresh;
contacts already in the database are updated in place.

**Without waiting for the export:** read the list straight from your
connections page:

```bash
python outreach.py import-linkedin            # --max 200 to stop early
```

It opens the page in the automation browser (if LinkedIn asks you to log in, do
it in that window and the import carries on by itself), then asks for the list
the same way the page does as you scroll, 10 people per request, about one
request a second, so a few thousand connections take minutes. If that request
can't be seen it falls back to scrolling the page. It never clicks anything
except *Show more results*, stops on a security check, saves as it goes, and
keeps what it found if you Ctrl-C. You get name, profile URL and connection
date. Title and company come from each person's headline ("Head of
Growth at Bloom" → *Head of Growth*, *Bloom*), so they're rougher than the
CSV's. Importing the CSV later overwrites them, while `import-linkedin` only
fills fields that are still empty. This is automated browsing of LinkedIn too,
with the same account risk as `autosend` below.

## 2. Pick an audience

```bash
python outreach.py list --title founder --title "head of" --title cto --since 2023-01-01
python outreach.py list --title partner --title principal --title angel --exclude-company recruit
```

You can repeat `--title` and `--company`; repeated values are OR-ed together.
There are also `--exclude-title`, `--exclude-company`, `--since`, `--before`,
`--ids 4,9,12` and `--limit`. Mark people never to contact with
`python outreach.py dnc 12,40`.

### Sorting people into groups with Claude

Keyword filters misfire: `--title remote` matches "remote patient monitoring",
and a "Founder" of a bakery and a "Founding Engineer" look alike. `tag` has
Claude read each headline in context and sort people into groups you define,
25 people per call (a network of 5,000 takes about 15 minutes; on the API that
is roughly 200 short calls, a few dollars at most):

```bash
python outreach.py tag --tags "founder: started or runs a company
investor: invests in startups (VC, angel, fund)
travel: works in travel, tourism or hospitality
engineer: builds software"
python outreach.py list --tag investor --tag founder      # has either group
python outreach.py draft beta --template t.txt --priority investor,founder   # queue them first
```

Someone can be in several groups or none. People already sorted are skipped
unless you pass `--redo`; the usual filters narrow who gets sorted. `--tag` and
`--exclude-tag` work on every command that takes filters, and `draft
--priority` puts the groups you name at the front of the queue, in that order,
so a daily cap reaches the important people first. On the web page this is the
*Sort people into groups* card on the Contacts tab.

## 3. Draft a campaign

Templates are plain text files. Placeholders are filled from each contact:
`{first_name}`, `{last_name}`, `{company}`, `{position}`. Your own values come
from `{sender_name}` and `{sender_<anything>}`, set with `--var`. Add a fallback
for empty fields like this: `{company|your team}`.

```bash
# Product feedback / beta users
python outreach.py draft beta-oct --template templates/product-beta.txt \
  --sender-name "Sam" --var product="Acme" --var audience="small agencies" \
  --var problem="tracking how AI assistants describe their clients" \
  --title founder --title "head of" --dry-run       # preview 3, save nothing

# Investors
python outreach.py draft seed-round --template templates/investor-intro.txt \
  --sender-name "Sam" --var pitch="We help ..." --var traction="We're at ..." \
  --title partner --title principal --title angel
```

Running `draft` again for the same campaign adds only people who aren't in it
yet. Anyone you've messaged in *any* campaign within the last 30 days is skipped
(change this with `--cooldown-days`).

Review and change drafts before sending:

```bash
python outreach.py review beta-oct
python outreach.py export beta-oct exports/beta-oct.csv     # edit in a spreadsheet...
python outreach.py import-drafts beta-oct exports/beta-oct.csv  # ...and load the edits back
```

### Making each message personal

Template messages read like templates. `personalize` has Claude rewrite each
draft for the person it goes to, keeping the structure, the ask, the links and
the sign-off, and using only facts it was given about them. By default those
facts are what is already stored: name, headline, company and title. With
`--enrich` it first reads the person's LinkedIn profile (About, Experience,
Education; three page loads per person, paced) and writes around one or two
real details from it.

Reading profiles is the heaviest thing this tool does on LinkedIn, and LinkedIn
limits how many profiles an account may view, so it only happens when you ask
for it, and it is worth keeping to small batches (`--max 20`).

Each rewrite also sees the opening lines of the campaign's last few messages, so
twenty messages in a row don't all start the same way: every person gets a hook
that fits only them.

When sending with `--personalize`, the tool opens the conversation first and
reads what you and the person have already said to each other on LinkedIn. The
rewrite then continues that conversation instead of restarting it: no
introducing yourself to someone you've talked to, no repeating what they already
know, and an unanswered message from them gets acknowledged first. It never
answers for you, though: a rewrite that accepts, declines or confirms a plan or
promises something in your name fails the pre-send check. When the person is
waiting on you (they asked you something, or proposed a meeting), or no rewrite
that fits the conversation passes the checks, the message is held: nothing is
sent, it's marked `skipped` with the reason, and you answer them yourself. The
rewrite also sees when each message was sent and today's date, so a dinner
proposed in March isn't mistaken for a plan this week. The earlier conversation
is kept with the message, so the Review tab shows it above what was sent.

`autosend --personalize` rewrites from what is already stored (headline,
company, title, and any profile read earlier) plus the conversation, so each
person costs one page load. Add `--read-profiles` to also open the person's
About, Experience and Education first: more to write from, three more page
loads a person.

```bash
python outreach.py personalize beta-oct --enrich \
  --brief "Get honest product feedback on Acme from founders, and open the door to a call"
python outreach.py review beta-oct          # read every message before sending
```

`--enrich` opens the browser (log in if asked). Profiles are kept for 30 days
in the database, so running again only reads new people. Messages already
personalized are left alone unless you pass `--redo`. If Claude fails on one
message, that draft keeps its template text and the rest continue.

Every rewrite is checked before it is kept. Free checks first: each link in
the template must appear unchanged, the sign-off must be identical, the first
line must still greet the person, no placeholder or preamble, and the length
rule. When those pass, one short extra Claude call lists any statement about
the person the profile doesn't support. A rewrite with a problem gets one
retry with the problems spelled out; if that still fails, the template text is
used and the reason is printed (`! Jane Doe: using the template text; the
rewrite says something the profile doesn't support: ...`). The same checks run
when `autosend --personalize` rewrites a message just before sending it.

Claude is reached one of two ways, whichever is available:

- **The Claude API**, when `ANTHROPIC_API_KEY` is set. Billed to that account.
  Put the key in a `.env` file next to `outreach.py` (copy `.env.example`; the
  file is gitignored) and both the command line and the web page pick it up.
- **Claude Code** already installed and logged in on this computer (`claude
  login`), when no key is set. Generation counts toward that plan's usage.

The model is `claude-opus-5-5`; set `OUTREACH_AI_MODEL` in `.env` to use
another.

Claude sees the campaign brief, the draft, and the profile text; never emails.
`draft --ai` does the same rewrite at drafting time, using whatever profiles
have been read (`enrich --title founder` reads profiles for a filter).

## 4. Send

### Fully automated: `autosend`

The first time, a browser window opens on LinkedIn's login page: log in
yourself, including 2FA, and the run carries on once you're in. The session is
saved in `~/.outreach-linkedin-profile`. The tool never sees your password. Keep
that folder private, because anyone who has it can use your LinkedIn session.
To log in again on its own (for example after LinkedIn logs you out):

```bash
python outreach.py login                 # add --channel chrome to use your installed Chrome
```

Do a dry run first. It opens each profile and types the message but never
clicks Send:

```bash
python outreach.py autosend beta-oct --dry-run --max 3
```

Then send for real:

```bash
python outreach.py autosend beta-oct     # asks you to type 'send' to confirm
```

It sends up to `--daily-cap` messages (default 20 per rolling 24 hours, across
all campaigns), waits a random 60-180 seconds between messages
(`--min-delay`/`--max-delay`), and marks each one sent as it goes. Stop it any
time with Ctrl-C, and the next run picks up where it left off.

| What happens | Result |
|---|---|
| Login wall, security check, "unusual activity" or restriction notice | Stops the whole run and saves a screenshot |
| Not a 1st-degree connection | That message is marked `failed`; the run continues (to reach them, see *Connecting with new people*) |
| Page didn't load properly, no Message button or message box | Nothing was sent, so that person gets one more try at the end of the run; a second failure is marked `failed` |
| Someone else's chat pops up while the page loads | Ignored: the tool only reads and types in the conversation whose header names this person, checks the text landed in that box before sending, and sends nothing if it can't find it |
| The same person was imported twice with differently encoded profile links (`%C3%A9` vs `é`) | Merged into one contact when the database opens, so nobody gets a message twice |
| LinkedIn's "Press Enter to Send" setting is on | Handled: the tool presses Enter instead of clicking Send |
| Clicked Send but couldn't confirm it went out | Confirmed from the conversation when LinkedIn is just slow; otherwise marked `failed`, never retried automatically, so nobody gets it twice |
| LinkedIn loads slowly | Page loads are retried; after three timeouts the run stops cleanly |
| Internet connection drops | Stops; nothing was sent to that person and their message stays a draft |
| 3 failures in a row | Stops; LinkedIn's layout has probably changed (selectors are at the top of `linkedin_browser.py`) |

Failure screenshots go to `failures/`. Look at failures with
`review beta-oct --status failed`. Requeue them after checking with
`mark beta-oct 12,40 draft`.

**Running it every day without you there:** schedule it with cron or Task
Scheduler, using `--yes` to skip the confirmation prompt. The computer has to
be on, and the saved login has to still be valid. For example, weekdays at 10:15:

```
15 10 * * 1-5  cd /path/to/autolink-messenger && .venv/bin/python outreach.py autosend beta-oct --yes >> autosend.log 2>&1
```

### Assisted: `send`

```bash
python outreach.py send beta-oct
```

The message is copied to your clipboard and the profile opens. In LinkedIn,
click **Message**, paste and send. Then answer in the terminal:

| Key | Action |
|---|---|
| Enter | mark as sent |
| `s` | skip for now |
| `e` | edit the message (uses `$EDITOR` if set) |
| `x` | never contact this person |
| `q` | quit; pick up where you left off next time |

## Connecting with new people

Messages only reach your connections. For anyone else, the tool sends a
**connection request with a note**: the same draft, review and send flow, with
the message as the note.

```bash
# add people by profile link; a name after the link saves opening the profile
python outreach.py add-prospects "https://www.linkedin.com/in/someone" \
  "https://www.linkedin.com/in/someone-else, Jane Doe, Acme, CTO"
python outreach.py add-prospects --file links.txt

python outreach.py list --prospects
python outreach.py draft intros --prospects --template templates/connect-note.txt \
  --sender-name "Sam" --var product="Acme"
python outreach.py autosend intros --dry-run     # types each note, then cancels
python outreach.py autosend intros --personalize
```

On the web page, paste the links under **Add people you're not connected to
yet**, then choose **Not connected yet** under *Who* before writing.

What to know:

- **The note is short.** LinkedIn's box takes 200 characters on a free account
  and 300 with Premium. The tool reads the real limit from the box, has Claude
  write to it when personalizing, and refuses a note that doesn't fit rather
  than cutting it off.
- **Free accounts get only a few notes a month.** LinkedIn shows how many are
  left and the run prints it. When LinkedIn stops offering a note, the run
  stops: an invitation never goes out without its note unless you ask for that.
- **Out of notes? Connect first, message after.** `autosend intros
  --without-note` sends the requests with no note and keeps each message
  queued. Later runs check who accepted and send those people their message as
  a normal message, which has no 200-character limit. The check is one look at
  the top of your connections list, not a visit to every profile. People who
  haven't answered are left alone, and nobody is invited twice.
- **Requests have their own cap**, `--invite-cap` (15 per 24 hours by default),
  on top of the daily cap. LinkedIn allows roughly 100 requests a week.
- **Only their own Connect button is used.** A profile's sidebar suggests other
  people with Connect buttons of their own; the tool matches the button to the
  profile's owner.
- **People in the two groups never mix.** A campaign is drafted either for your
  connections or, with `--prospects`, for people you've added. Someone who turns
  out to be connected already is skipped and moved to your connections, and so
  is anyone who shows up in your next connections import after accepting.
- A reply after they accept is picked up by `replies` like any other.

## 5. Track replies

```bash
python outreach.py replies                 # reads your inbox, marks who answered
python outreach.py mark beta-oct 12,40 replied   # or by hand
python outreach.py status
```

`replies` opens your inbox in the automation browser, scans the most recent
conversations (150 by default, `--limit` to change) and marks a message
*replied* when the latest message in that person's thread is theirs, on or
after the day you wrote. It then opens each new reply's conversation and saves
the whole reply, since the inbox only shows one cut-off line (up to 20 per
scan). The web page has a **Check replies** button on the
Send tab. `status` shows draft, sent, skipped and replied counts and the reply
rate for each campaign.

To undo a campaign: `delete-drafts NAME` removes its unsent messages and keeps
the record of what was sent; `delete-campaign NAME` removes everything (it
refuses when messages were sent unless you pass `--force`). Both are on the
Review tab too. Your data lives in `outreach.db` next to `outreach.py` (SQLite,
gitignored); set `--db` or `OUTREACH_DB` to keep it somewhere else.

## Before you message investors

In the US, a Reg D 506(b) raise can't use *general solicitation*. Sending an
investment pitch to every connection, including people you have no real prior
relationship with, can count as general solicitation. Other countries have
similar rules. Keep investor campaigns to people you actually know and who are
likely to be accredited. Ask for a conversation rather than pitching the
securities in the first message. Check with your counsel.

## Tests

```bash
python -m unittest discover -s tests
```

The browser tests run the real automation in a headless browser against a local
mock of LinkedIn's profile, messaging and connections pages. They're skipped if
Playwright isn't installed, and they never contact LinkedIn. The whole suite
takes a few minutes, most of it waiting out the timeouts of the failure cases.
