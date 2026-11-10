"""End-to-end test of autosend against a local mock of LinkedIn's profile + messaging UI.

Runs a real headless browser; skipped when Playwright isn't installed. Set
OUTREACH_BROWSER_PATH if Playwright's bundled Chromium isn't installed.
"""

import json
import re
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import linkedin_browser as lb  # noqa: E402
import outreach  # noqa: E402

try:
    import playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

# The composer, as on LinkedIn's /messaging/compose page and in the older overlay.
COMPOSER = """<div id="overlay" {hidden}>
  {thread}
  <div class="msg-form__contenteditable" contenteditable="true" role="textbox"
       aria-label="Write a message…" style="min-height:40px;border:1px solid"></div>
  <button class="msg-form__send-button" type="submit" disabled>Send</button>
  <button aria-label="Close your conversation with {name}">x</button>
</div>
<script>
const box = document.querySelector('.msg-form__contenteditable');
const msgBtn = document.querySelector('main button');
if (msgBtn) msgBtn.onclick = () => document.getElementById('overlay').hidden = false;
document.querySelector('[aria-label^="Close"]').onclick = () => document.getElementById('overlay').hidden = true;
box.addEventListener('keydown', e => {{ if (e.key === 'Enter') window.enterPressed = true; }});
// Like LinkedIn: Send is disabled until text is in the box, and only enables a moment after input.
box.addEventListener('input', () => {{ setTimeout(() => {{ document.querySelector('.msg-form__send-button').disabled = !box.innerText.trim(); }}, 400); }});
document.querySelector('.msg-form__send-button').onclick = async () => {{
  if ({stuck}) return;
  await fetch('/sent', {{method: 'POST', body: JSON.stringify({{who: '{slug}', text: box.innerText}})}});
  box.innerText = '';
}};
</script>"""

# The same composer with LinkedIn's "Press Enter to Send" setting on: no Send button at all,
# Enter sends and Shift+Enter is a line break.
ENTER_COMPOSER = COMPOSER
for a, b in [('<button class="msg-form__send-button" type="submit" disabled>Send</button>',
              '<span>Press Enter to Send</span>'),
             ("document.querySelector('.msg-form__send-button').disabled = !box.innerText.trim();", ""),
             ("document.querySelector('.msg-form__send-button').onclick = async () => {{",
              "box.addEventListener('keydown', async e => {{ if (e.key !== 'Enter' || e.shiftKey) return; e.preventDefault();"),
             ("  box.innerText = '';\n}};", "  box.innerText = '';\n}});")]:
    assert ENTER_COMPOSER.count(a) == 1, a
    ENTER_COMPOSER = ENTER_COMPOSER.replace(a, b)

# LinkedIn slow to empty the box after sending: the message shows up in the conversation first.
SLOW_COMPOSER = COMPOSER.replace(
    "  box.innerText = '';",
    "  const item = document.createElement('div'); item.className = 'msg-s-event-listitem';\n"
    "  item.innerHTML = '<span class=\"msg-s-message-group__name\">Sam Sender</span>"
    "<p class=\"msg-s-event-listitem__body\"></p>';\n"
    "  item.querySelector('p').innerText = box.innerText; document.getElementById('overlay').prepend(item);")
assert SLOW_COMPOSER != COMPOSER

# A profile. Like the real page: name with a degree badge, the Message control, and a
# "More profiles" sidebar whose other people are 2nd degree.
PROFILE = """<!doctype html><html><body><main>
<section><div><div><h1>{name}</h1><span>· {degree}</span></div>
<p>Some headline</p>{button}</div></section>
<section><h2>About</h2><p>Ten years in {name}'s field.</p></section>
<aside><h2>More profiles for you</h2><p>Someone Else · 2nd</p><button>Follow</button></aside>
</main>
{composer}</body></html>"""

COMPOSE_PAGE = """<!doctype html><html><body><main><h2>New message</h2>{composer}</main></body></html>"""

# New layout: Message is a link to the compose page. Old layout: a button opening an overlay.
LINK = '<a href="/messaging/compose/?recipient={slug}&interop=msgOverlay">Message</a>'
BUTTON = '<button aria-label="Message {name}">Message</button>'
PREMIUM = '<a href="/messaging/compose/?recipient={slug}&premium=1" aria-label="Message with Premium">Say hello</a>'

# An existing conversation above the composer, as LinkedIn renders it: the sender's name only
# on the first message of a run.
THREAD = """<ul class="msg-s-message-list-content">
<li><time class="msg-s-message-list__time-heading">MAR 2</time></li>
<li class="msg-s-message-list__event"><div class="msg-s-event-listitem">
  <span class="msg-s-message-group__name">Sam Sender</span><time class="msg-s-message-group__timestamp">Mar 2</time>
  <p class="msg-s-event-listitem__body">Great meeting you at the summit, Raj.</p></div></li>
<li class="msg-s-message-list__event"><div class="msg-s-event-listitem">
  <p class="msg-s-event-listitem__body">Let's catch up when you're back.</p></div></li>
<li class="msg-s-message-list__event"><div class="msg-s-event-listitem">
  <span class="msg-s-message-group__name">Raj Patel</span>
  <p class="msg-s-event-listitem__body">Likewise! Back on the 10th.</p></div></li>
</ul>"""

PAGES = {
    "ok1": dict(name="Jane Doe", degree="1st", button=LINK, stuck="false", thread=""),
    "ok2": dict(name="Raj Patel", degree="1st", button=BUTTON, stuck="false", thread=THREAD),
    "nomsg": dict(name="Maria Gomez", degree="1st", button='<button>Follow</button>', stuck="false", thread=""),
    "stuck": dict(name="Tom Lee", degree="1st", button=LINK, stuck="true", thread=""),
    "second": dict(name="Ava Second", degree="2nd", button=PREMIUM, stuck="false", thread=""),
    "flaky": dict(name="Flo Flaky", degree="1st", button=LINK, stuck="false", thread=""),
    "slow": dict(name="Sol Slow", degree="1st", button=LINK, stuck="false", thread="", composer=SLOW_COMPOSER),
    "enter": dict(name="Eve Enter", degree="1st", button=LINK, stuck="false", thread="", composer=ENTER_COMPOSER),
}


def profile_html(slug):
    p = dict(PAGES[slug], slug=slug)
    p["button"] = p["button"].format(**p)
    tpl = p.pop("composer", COMPOSER)
    # the overlay composer only exists in the old (button) layout
    p["composer"] = tpl.format(hidden="hidden", **p) if p["button"].startswith("<button aria") else ""
    return PROFILE.format(**p)


class Handler(BaseHTTPRequestHandler):
    sent = []
    visits = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        slug = self.path.strip("/").split("/")[-1]
        if "/details/" in self.path:
            part = self.path.rstrip("/").split("/")[-1].capitalize()
            html = f"<html><body><main><h2>{part}</h2><p>Something at Somewhere</p><aside><h3>More profiles for you</h3></aside></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
            return
        if self.path.startswith("/messaging/compose/"):
            q = dict(p.split("=") for p in self.path.split("?")[1].split("&"))
            if "premium" in q:
                html = "<html><body><main><h2>InMail</h2><p>Buy Premium to send InMail.</p></main></body></html>"
            else:
                slug = q["recipient"]
                tpl = PAGES[slug].get("composer", COMPOSER)
                html = COMPOSE_PAGE.format(composer=tpl.format(hidden="", slug=slug, **PAGES[slug]))
        elif slug == "restricted":
            html = "<html><body><main>We've detected unusual activity on your account.</main></body></html>"
        elif slug in PAGES:
            Handler.visits[slug] = Handler.visits.get(slug, 0) + 1
            # "flaky" comes up blank the first time, like LinkedIn on a slow minute
            html = "<html><body><main></main></body></html>" if (slug, Handler.visits[slug]) == ("flaky", 1) \
                else profile_html(slug)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Handler.sent.append(json.loads(body))
        self.send_response(204)
        self.end_headers()


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class SlowPageTest(unittest.TestCase):
    """A page load that times out is retried; one that keeps timing out stops the run cleanly."""

    class Page:
        def __init__(self, slow):
            self.slow, self.url, self.visits = slow, "", 0

        def goto(self, url, wait_until=None):
            self.visits += 1
            if self.visits <= self.slow:
                raise type("TimeoutError", (Exception,), {})("Page.goto: Timeout 30000ms exceeded.")
            self.url = url

        def wait_for_timeout(self, ms):
            pass

    def test_retries_a_slow_load(self):
        page = self.Page(slow=2)
        lb.ensure_logged_in(page, "https://www.linkedin.com/feed/")
        self.assertEqual(page.visits, 3)

    def test_gives_up_cleanly(self):
        with self.assertRaises(lb.StopRun):
            lb.ensure_logged_in(self.Page(slow=99), "https://www.linkedin.com/feed/")


class AutosendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/in/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.sent.clear()
        Handler.visits.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        outreach.main(["--db", self.dbpath, "import", str(HERE / "sample_connections.csv")])
        outreach.main(["--db", self.dbpath, "draft", "c", "--template", str(HERE / "templates/product-beta.txt"),
                       "--sender-name", "Sam", "--var", "product=P", "--var", "audience=A", "--var", "problem=Q"])

    def tearDown(self):
        self.tmp.cleanup()

    def point_contacts(self, slugs):
        """Map contacts (in queue order) to mock pages; extra contacts get do-not-contact."""
        db = outreach.connect(self.dbpath)
        ids = [r["contact_id"] for r in outreach.campaign_messages(db, "c", "draft")]
        for cid, slug in zip(ids, slugs):
            db.execute("UPDATE contacts SET url=? WHERE id=?", (self.base + slug, cid))
        for cid in ids[len(slugs):]:
            db.execute("UPDATE contacts SET do_not_contact=1 WHERE id=?", (cid,))
        db.commit()
        return db, ids

    def autosend(self, db, *extra):
        args = outreach.build_parser().parse_args(
            ["--db", self.dbpath, "autosend", "c", "--headless", "--yes", "--min-delay", "0",
             "--max-delay", "0", "--profile-dir", str(Path(self.tmp.name) / "profile"),
             "--screenshots", str(Path(self.tmp.name) / "shots"), *extra])
        outreach.cmd_autosend(db, args, sleep=lambda s: None)

    def statuses(self, db):
        return {r["contact_id"]: r["status"] for r in db.execute("SELECT contact_id, status FROM messages")}

    def test_sends_and_records(self):
        # ok1: Message is a link to the compose page (current layout); ok2: a button + overlay (old);
        # nomsg: no Message control; second: a 2nd-degree profile whose only link is paid InMail.
        db, ids = self.point_contacts(["ok1", "nomsg", "ok2", "second"])
        self.autosend(db)
        self.assertEqual([s["who"] for s in Handler.sent], ["ok1", "ok2"])
        self.assertIn("Hi ", Handler.sent[0]["text"])
        # multi-line body arrives exactly as drafted, in one message
        expected = outreach.campaign_messages(db, "c", "sent")[0]["body"]
        self.assertEqual(Handler.sent[0]["text"], expected)
        st = self.statuses(db)
        self.assertEqual([st[i] for i in ids[:4]], ["sent", "failed", "sent", "failed"])
        errs = {cid: db.execute("SELECT error FROM messages WHERE contact_id=?", (cid,)).fetchone()[0] for cid in ids[:4]}
        self.assertIn("Message button", errs[ids[1]])
        self.assertIn("1st-degree", errs[ids[3]])
        self.assertTrue(any(Path(self.tmp.name, "shots").glob("failed-*.png")))

    def test_unconfirmed_send_is_not_retried(self):
        db, ids = self.point_contacts(["stuck"])
        import linkedin_browser
        orig = linkedin_browser.LinkedInSender.__init__
        linkedin_browser.LinkedInSender.__init__ = lambda s, p, **k: orig(s, p, timeout_s=5, confirm_timeout_s=1)
        try:
            self.autosend(db)
        finally:
            linkedin_browser.LinkedInSender.__init__ = orig
        self.assertEqual(self.statuses(db)[ids[0]], "failed")

    def test_retries_a_page_that_failed_to_load(self):
        # Flo's profile is blank on the first visit: nothing was sent, so she gets one more try
        # after everyone else instead of being marked failed.
        db, ids = self.point_contacts(["flaky", "ok1"])
        import linkedin_browser
        orig = linkedin_browser.LinkedInSender.__init__
        linkedin_browser.LinkedInSender.__init__ = lambda s, p, **k: orig(s, p, timeout_s=3, confirm_timeout_s=5)
        try:
            self.autosend(db)
        finally:
            linkedin_browser.LinkedInSender.__init__ = orig
        self.assertEqual([s["who"] for s in Handler.sent], ["ok1", "flaky"])
        self.assertEqual(set(self.statuses(db)[i] for i in ids[:2]), {"sent"})

    def test_send_confirmed_by_the_conversation(self):
        db, ids = self.point_contacts(["slow"])
        import linkedin_browser
        orig = linkedin_browser.LinkedInSender.__init__
        linkedin_browser.LinkedInSender.__init__ = lambda s, p, **k: orig(s, p, timeout_s=5, confirm_timeout_s=3)
        try:
            self.autosend(db)
        finally:
            linkedin_browser.LinkedInSender.__init__ = orig
        self.assertEqual(len(Handler.sent), 1)
        self.assertEqual(self.statuses(db)[ids[0]], "sent")

    def test_stops_on_security_notice(self):
        db, ids = self.point_contacts(["ok1", "restricted", "ok2"])
        self.autosend(db)
        self.assertEqual([s["who"] for s in Handler.sent], ["ok1"])
        st = self.statuses(db)
        self.assertEqual([st[i] for i in ids[:3]], ["sent", "draft", "draft"])

    def test_enter_to_send_layout(self):
        # "Press Enter to Send" on: no Send button exists, Enter sends, line breaks stay intact.
        db, ids = self.point_contacts(["enter"])
        self.autosend(db)
        self.assertEqual([s["who"] for s in Handler.sent], ["enter"])
        self.assertEqual(Handler.sent[0]["text"], outreach.campaign_messages(db, "c", "sent")[0]["body"])
        self.assertEqual(self.statuses(db)[ids[0]], "sent")

    def test_stops_when_the_network_is_down(self):
        # A refused connection raises the browser's net:: error, like a dropped internet connection.
        db, ids = self.point_contacts(["ok1", "ok2", "ok1"])
        with socket.socket() as sock:  # a port nothing listens on
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        db.execute("UPDATE contacts SET url=? WHERE id=?", (f"http://127.0.0.1:{port}/in/offline", ids[1]))
        db.commit()
        self.autosend(db)
        self.assertEqual([s["who"] for s in Handler.sent], ["ok1"])
        st = self.statuses(db)
        self.assertEqual([st[i] for i in ids[:3]], ["sent", "draft", "draft"])  # not failed, not counted

    def test_dry_run_sends_nothing(self):
        db, ids = self.point_contacts(["ok1", "ok2"])
        self.autosend(db, "--dry-run")
        self.assertEqual(Handler.sent, [])
        self.assertEqual(set(self.statuses(db)[i] for i in ids[:2]), {"draft"})

    def test_personalize_just_before_sending(self):
        class FakeClaude:
            name = "fake"
            prompts = []

            def generate(self, prompt):
                self.prompts.append(prompt)
                if "Reply with exactly NONE" in prompt:  # the check for invented facts
                    return "NONE"
                return "Hi %s,\n\nPersonal.\n\nSam" % re.search(r"Name: (\S+)", prompt).group(1)
        db, ids = self.point_contacts(["ok1", "ok2"])
        db.execute("UPDATE campaigns SET brief='Get feedback'")
        db.commit()
        ai = FakeClaude()
        args = outreach.build_parser().parse_args(
            ["--db", self.dbpath, "autosend", "c", "--headless", "--yes", "--min-delay", "0", "--max-delay", "0",
             "--personalize", "--profile-dir", str(Path(self.tmp.name) / "profile"),
             "--screenshots", str(Path(self.tmp.name) / "shots")])
        outreach.cmd_autosend(db, args, sleep=lambda s: None, ai=ai)
        self.assertEqual(sorted(s["text"] for s in Handler.sent),
                         sorted(f"Hi {m['first_name']},\n\nPersonal.\n\nSam" for m in outreach.campaign_messages(db, "c", "sent")))
        self.assertEqual(len(ai.prompts), 4)  # a rewrite and a check for invented facts per message
        self.assertIn("Ten years in", ai.prompts[0])  # the profile was read first
        self.assertIn("Something at Somewhere", ai.prompts[0])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 2)
        self.assertTrue(all(m["ai_at"] for m in outreach.campaign_messages(db, "c", "sent")))
        # Raj's conversation already had messages: the rewrite and the fact check both saw them
        raj = [p for p in ai.prompts if "Name: Raj" in p]
        self.assertEqual(len(raj), 2)
        for p in raj:
            self.assertIn("You: Great meeting you at the summit, Raj.", p)
            self.assertIn("You: Let's catch up when you're back.", p)
            self.assertIn("Raj: Likewise! Back on the 10th.", p)
        self.assertIn("continues that conversation", raj[0])
        self.assertIn("[Mar 2]\nYou: Great meeting you", raj[0])  # with the day it was sent
        for p in [p for p in ai.prompts if "Name: Raj" not in p]:  # the other person: no conversation yet
            self.assertNotIn("Messages already exchanged", p)
        threads = {m["first_name"]: m["thread"] for m in outreach.campaign_messages(db, "c", "sent")}
        self.assertIn("Raj: Likewise!", threads.pop("Raj"))
        self.assertEqual(list(threads.values()), [None])

    def test_holds_someone_waiting_on_you(self):
        class FakeClaude:
            name = "fake"

            def generate(self, prompt):
                if "Reply with exactly NONE" in prompt:
                    return "NONE"
                if "Name: Raj" in prompt:  # Raj's conversation is waiting on us
                    return "HOLD"
                return "Hi %s,\n\nPersonal.\n\nSam" % re.search(r"Name: (\S+)", prompt).group(1)
        db, ids = self.point_contacts(["ok1", "ok2"])
        db.execute("UPDATE campaigns SET brief='Get feedback'")
        db.commit()
        args = outreach.build_parser().parse_args(
            ["--db", self.dbpath, "autosend", "c", "--headless", "--yes", "--min-delay", "0", "--max-delay", "0",
             "--personalize", "--profile-dir", str(Path(self.tmp.name) / "profile"),
             "--screenshots", str(Path(self.tmp.name) / "shots")])
        outreach.cmd_autosend(db, args, sleep=lambda s: None, ai=FakeClaude())
        self.assertEqual([s["who"] for s in Handler.sent], ["ok1"])
        st = self.statuses(db)
        self.assertEqual([st[i] for i in ids[:2]], ["sent", "skipped"])
        err = db.execute("SELECT error FROM messages WHERE contact_id=?", (ids[1],)).fetchone()[0]
        self.assertTrue(err.startswith(outreach.HELD))

    def test_daily_cap(self):
        db, ids = self.point_contacts(["ok1", "ok2"])
        self.autosend(db, "--daily-cap", "1")
        self.assertEqual(len(Handler.sent), 1)
        self.autosend(db, "--daily-cap", "1")  # cap already used: sends nothing
        self.assertEqual(len(Handler.sent), 1)


if __name__ == "__main__":
    unittest.main()
