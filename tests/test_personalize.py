"""enrich + personalize against a local mock of LinkedIn's profile pages and a fake Claude.

The browser tests run a real headless browser and are skipped when Playwright isn't
installed. Nothing here contacts LinkedIn or Claude.
"""

import re
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

# The profile page as LinkedIn renders it now: name as an h2, "· 1st" badge, About (truncated
# behind a "…more" button), Activity, and a sidebar of other people. No Experience here.
PROFILE = """<!doctype html><html><body><main>
<section><h2>Jane Doe</h2><div>· 1st</div><div>Partner at Acme Ventures</div><div>Lisbon, Portugal</div>
<div>·</div><a>Contact info</a><div>500+ connections</div><a href="/messaging/compose/?recipient=janedoe">Message</a></section>
<section><h2>About</h2><p>Early-stage investor. Before Acme I ran product at Bloom for six years<span id="rest" hidden>, mostly on payments.</span>
<button id="more">…more</button></p></section>
<section><h2>Activity</h2><p>1,200 followers</p><p>Jane Doe reposted this</p></section>
<aside><h3>More profiles for you</h3><p>Someone Else</p><p>· 2nd</p></aside>
</main><script>
document.getElementById('more').onclick = () => { document.getElementById('rest').hidden = false; document.getElementById('more').remove(); };
</script></body></html>"""

EXPERIENCE = """<!doctype html><html><body><main>
<h2>Experience</h2>
<ul><li><p>Partner</p><p>Acme Ventures · Full-time</p><p>Jan 2022 - Present · 4 yrs</p><p>Seed and Series A in fintech.</p></li>
<li><p>Head of Product</p><p>Bloom</p><p>2016 - 2022 · 6 yrs</p></li></ul>
<aside><h3>More profiles for you</h3><p>Someone Else</p></aside>
</main></body></html>"""

EDUCATION = """<!doctype html><html><body><main>
<h2>Education</h2>
<ul><li><p>University of Lisbon</p><p>MSc, Computer Science</p><p>2010 – 2012</p></li></ul>
<aside><h3>More profiles for you</h3><p>Someone Else</p></aside>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if "/details/experience" in self.path:
            html = EXPERIENCE
        elif "/details/education" in self.path:
            html = EDUCATION
        elif self.path.startswith("/in/"):
            html = PROFILE
        else:
            html = "<html><body><main>feed</main></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")  # LinkedIn's "…more" is UTF-8
        self.end_headers()
        self.wfile.write(html.encode())


class FakeClaude:
    name = "a fake Claude"

    def __init__(self, reply=None, fail=False, replies=None):
        self.prompts = []
        self.reply = reply
        self.fail = fail
        self.replies = list(replies or [])

    def generate(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("quota")
        if "Reply with exactly NONE" in prompt and not self.replies:  # the grounding check
            return "NONE"
        reply = self.replies.pop(0) if self.replies else self.reply
        first = re.search(r"Name: (\S+)", prompt)  # "{first}" greets whoever the prompt is about
        return reply.replace("{first}", first.group(1)) if reply and first else reply


class PromptTest(unittest.TestCase):
    def test_prompt_includes_profile_and_rules(self):
        contact = {"first_name": "Jane", "last_name": "Doe", "position": "Partner", "company": "Acme"}
        profile = {"headline": "Partner at Acme Ventures", "location": "Lisbon", "about": "Early-stage investor.",
                   "experience": "Partner\nAcme Ventures", "education": "University of Lisbon"}
        p = outreach.personalization_prompt("Hi Jane,\n\nDraft.", contact, "Get feedback", profile)
        for needle in ("Early-stage investor.", "University of Lisbon", "Get feedback", "Hi Jane,", "Never invent"):
            self.assertIn(needle, p)
        self.assertIn("unknown", outreach.personalization_prompt("x", {**contact, "company": ""}, "g", None))

    def test_prompt_lists_earlier_openers_so_they_are_not_reused(self):
        contact = {"first_name": "Jane", "last_name": "Doe", "position": "", "company": ""}
        self.assertEqual(outreach.opener("Hi Jane,\n\nYou run Acme, so...\n\nSam"), "You run Acme, so...")
        self.assertEqual(outreach.opener("One line only"), "One line only")
        p = outreach.personalization_prompt("Hi Jane,\n\nDraft.", contact, "g", None,
                                            ["You've spent years on X.", "From A to B, you..."])
        self.assertIn("Openers already used", p)
        self.assertIn("- From A to B, you...", p)
        self.assertNotIn("Openers already used", outreach.personalization_prompt("x", contact, "g", None))

    def test_thread_text_names_sides_and_reaches_the_prompt(self):
        jane = {"first_name": "Jane", "last_name": "Doe", "position": "", "company": ""}
        thread = [{"who": "Sam Sender", "text": "Hi Jane,\n\nlong  spaced   text"},
                  {"who": "Jane Doe", "text": "Thanks Sam!"},
                  {"who": "Jane", "text": "x" * 500}, {"who": "", "text": ""}]
        text = outreach.thread_text(thread, jane)
        self.assertEqual(text.splitlines()[:2], ["You: Hi Jane, long spaced text", "Jane: Thanks Sam!"])
        self.assertEqual(len(text.splitlines()[2]), len("Jane: ") + outreach.THREAD_CHARS)
        self.assertEqual(outreach.thread_text([], jane), "")
        dated = [{"who": "Sam Sender", "text": "Dinner tomorrow?", "day": "OCT 6"},
                 {"who": "Jane Doe", "text": "Sure", "day": "OCT 6"},
                 {"who": "Jane Doe", "text": "Which place?", "day": "TODAY"}]
        self.assertEqual(outreach.thread_text(dated, jane).splitlines(),
                         ["[Oct 6]", "You: Dinner tomorrow?", "Jane: Sure", "[Today]", "Jane: Which place?"])
        # the whole reply since your last message, for the reply scan
        self.assertEqual(outreach.their_last_words(dated, jane), "Sure\nWhich place?")
        self.assertEqual(outreach.their_last_words(dated[:1], jane), "")
        p = outreach.personalization_prompt("Hi Jane,\n\nDraft.", jane, "g", None, thread=text)
        self.assertIn("Jane: Thanks Sam!", p)
        self.assertIn("continues that conversation", p)
        self.assertIn("never answer for the sender", p)
        self.assertIn("today is", p)  # so an old plan isn't mistaken for a current one  # no accepting plans or promising in your name
        g = outreach.grounding_prompt("Hi Jane", jane, None, text)
        self.assertIn("Jane: Thanks Sam!", g)
        self.assertIn("confirms a plan", g)

    def test_conversation_waiting_on_you_is_held(self):
        jane = {"first_name": "Jane", "last_name": "Doe", "position": "", "company": ""}
        base = "Hi Jane,\n\nTry https://x.io when you can, and tell me.\n\nSam"
        thread = "Jane: Which day works for you next week?"
        # Claude sees she's waiting on you and says HOLD: nothing is written for her
        text, note = outreach.personalize(FakeClaude(replies=["HOLD"]), base, jane, "g", thread=thread)
        self.assertEqual((text, note.startswith(outreach.HELD)), (base, True))
        # a rewrite that fails its checks twice isn't swapped for the template when you've talked before
        ai = FakeClaude(replies=["Hi Jane,\n\nTry https://x.io.\n\nSam", "we're meeting Friday"] * 2)
        text, note = outreach.personalize(ai, base, jane, "g", thread=thread)
        self.assertTrue(note.startswith(outreach.HELD))
        self.assertIn("we're meeting Friday", note)
        # without a conversation, HOLD is just a bad rewrite and the template goes out as before
        text, note = outreach.personalize(FakeClaude(replies=["HOLD", "HOLD"]), base, jane, "g")
        self.assertEqual(text, base)
        self.assertFalse(note.startswith(outreach.HELD))

    def test_cheap_checks_catch_what_a_rewrite_must_keep(self):
        base = "Hi Jane,\n\nTry it at demo.example.com or https://cal.com/sam/15 today.\n\nSam"
        jane = {"first_name": "Jane", "last_name": "Doe", "position": "", "company": ""}
        self.assertEqual(outreach.rewrite_problems(base, "Hi Jane,\n\nYou at Acme: demo.example.com, "
                                                         "https://cal.com/sam/15.\n\nSam", jane), [])
        problems = outreach.rewrite_problems(
            base, "Here's the message:\n\nHello there,\n\nSee demo.example.org, https://cal.com/sam/15 {company}.\n\nSamuel", jane)
        for needle in ("link demo.example.com", "sign-off must stay exactly 'Sam'", "greet Jane", "placeholder", "preamble"):
            self.assertTrue(any(needle in p for p in problems), (needle, problems))
        self.assertIn("words", outreach.rewrite_problems("Hi Jane, " + "w " * 20, "Hi Jane, " + "w " * 40, jane)[0])
        # names with symbols are greeted by their letters; no sign-off line means no sign-off check
        self.assertEqual(outreach.rewrite_problems("Hi Mine,\n\nText that is long enough here.", "Hi Mine,\n\nOther text.",
                                                   {"first_name": "\u2728Mine"}), [])

    def test_grounding_check_parses_claims(self):
        jane = {"first_name": "Jane", "last_name": "Doe", "position": "Partner", "company": "Acme"}
        self.assertEqual(outreach.unsupported_claims(FakeClaude(replies=["NONE"]), "msg", jane), [])
        self.assertEqual(outreach.unsupported_claims(FakeClaude(replies=["None."]), "msg", jane), [])
        claims = outreach.unsupported_claims(FakeClaude(replies=["- she lived in Lisbon\n* we met at Web Summit"]), "msg", jane)
        self.assertEqual(claims, ["she lived in Lisbon", "we met at Web Summit"])

    def test_personalize_retries_once_then_falls_back_to_the_template(self):
        jane = {"first_name": "Jane", "last_name": "Doe", "position": "", "company": ""}
        base = "Hi Jane,\n\nTry https://x.io when you can, and tell me.\n\nSam"
        good = "Hi Jane,\n\nAs a partner, try https://x.io and tell me.\n\nSam"

        # passes first time: rewrite + grounding check = 2 calls
        ai = FakeClaude(replies=[good, "NONE"])
        self.assertEqual(outreach.personalize(ai, base, jane, "g"), (good, ""))
        self.assertEqual(len(ai.prompts), 2)
        self.assertIn("Known facts:", ai.prompts[1])

        # link dropped: retry with the problem named, retry passes
        ai = FakeClaude(replies=[good.replace("https://x.io", "x.io"), good, "NONE"])
        self.assertEqual(outreach.personalize(ai, base, jane, "g"), (good, ""))
        self.assertIn("link https://x.io is missing", ai.prompts[1])
        self.assertEqual(len(ai.prompts), 3)

        # invented fact: retry still invents -> template text, with the reason
        ai = FakeClaude(replies=[good, "she lived in Lisbon", good, "she lived in Lisbon"])
        text, note = outreach.personalize(ai, base, jane, "g")
        self.assertEqual(text, base)
        self.assertIn("doesn't support, or commits you: she lived in Lisbon", note)
        self.assertIn("Fix every problem", ai.prompts[2])

        # over-long then cut: the old length rule still holds
        long = "Hi Jane,\n\n" + "word " * 40 + "https://x.io\n\nSam"
        ai = FakeClaude(replies=[long, good, "NONE"])
        self.assertEqual(outreach.personalize(ai, base, jane, "g")[0], good)
        self.assertIn("the limit is", ai.prompts[1])

        # Claude declines
        self.assertEqual(outreach.personalize(FakeClaude(reply=None), base, jane, "g")[0], base)


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class EnrichPersonalizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        outreach.main(["--db", self.dbpath, "import", str(HERE / "sample_connections.csv")])
        outreach.main(["--db", self.dbpath, "draft", "c", "--template", str(HERE / "templates/product-beta.txt"),
                       "--sender-name", "Sam", "--var", "product=P", "--var", "audience=A", "--var", "problem=Q",
                       "--limit", "2"])
        self.db = outreach.connect(self.dbpath)
        for r in self.db.execute("SELECT id, url FROM contacts"):  # point everyone at the mock
            self.db.execute("UPDATE contacts SET url=? WHERE id=?", (self.base + "/in/" + r["url"].rsplit("/", 1)[-1], r["id"]))
        self.db.commit()
        self.page_factory = lambda: lb.browser(str(Path(self.tmp.name) / "profile"), headless=True)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def args(self, cmd, *extra):
        return outreach.build_parser().parse_args(["--db", self.dbpath, cmd, *extra, "--min-delay", "0", "--max-delay", "0",
                                                   "--profile-dir", str(Path(self.tmp.name) / "profile")])

    def test_read_profile_expands_and_splits_sections(self):
        with self.page_factory() as page:
            p = lb.read_profile(page, self.base + "/in/janedoe", sleep=lambda s: None, pause=(0, 0))
        self.assertEqual(p["headline"], "Partner at Acme Ventures")
        self.assertEqual(p["location"], "Lisbon, Portugal")
        self.assertIn("mostly on payments", p["about"])  # the "…more" text was expanded
        self.assertNotIn("…more", p["about"])
        self.assertNotIn("followers", p["about"])  # stops at the next heading
        self.assertIn("Head of Product", p["experience"])
        self.assertNotIn("Someone Else", p["experience"])  # stops at the sidebar
        self.assertIn("University of Lisbon", p["education"])

    def test_enrich_saves_and_skips_recent(self):
        n = outreach.cmd_enrich(self.db, self.args("enrich", "--campaign", "c"), page_factory=self.page_factory,
                                sleep=lambda s: None)
        self.assertEqual(n, 2)
        rows = self.db.execute("SELECT * FROM profiles").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn("Acme Ventures", rows[0]["experience"])
        n = outreach.cmd_enrich(self.db, self.args("enrich", "--campaign", "c"), page_factory=self.page_factory,
                                sleep=lambda s: None)
        self.assertEqual(n, 0)  # already read

    def test_personalize_rewrites_waiting_drafts_and_keeps_template_on_failure(self):
        ai = FakeClaude(reply="Hi {first},\n\nPersonal version.\n\nSam")
        outreach.cmd_personalize(self.db, self.args("personalize", "c", "--brief", "Get feedback", "--enrich"),
                                 ai=ai, page_factory=self.page_factory, sleep=lambda s: None)
        msgs = outreach.campaign_messages(self.db, "c", "draft")
        self.assertEqual([m["body"] for m in msgs], [f"Hi {m['first_name']},\n\nPersonal version.\n\nSam" for m in msgs])
        self.assertTrue(all(m["ai_at"] for m in msgs))
        self.assertEqual(len(ai.prompts), 4)  # a rewrite and a grounding check per message
        self.assertIn("Early-stage investor", ai.prompts[0])  # the enriched profile reached Claude
        self.assertIn("Get feedback", ai.prompts[0])
        self.assertEqual(self.db.execute("SELECT brief FROM campaigns").fetchone()[0], "Get feedback")
        self.assertNotIn("Openers already used", ai.prompts[0])  # nothing written yet
        self.assertIn("- Personal version.", ai.prompts[2])  # the second rewrite sees the first one's opener

        # Already personalized: nothing happens without --redo
        ai2 = FakeClaude(reply="again")
        outreach.cmd_personalize(self.db, self.args("personalize", "c"), ai=ai2)
        self.assertEqual(ai2.prompts, [])

        # --redo starts from the template text again, not from the last rewrite
        ai3 = FakeClaude(reply="Hi {first},\n\nAgain.\n\nSam")
        outreach.cmd_personalize(self.db, self.args("personalize", "c", "--redo"), ai=ai3)
        self.assertIn("Hope all is well at", ai3.prompts[0])  # the template text
        draft, _, earlier = ai3.prompts[0].partition("Openers already used")
        self.assertNotIn("Personal version", draft)
        self.assertEqual(earlier.count("Personal version."), 1)  # the other message's opener, not its own

        # A failing Claude keeps the existing text
        before = [m["body"] for m in outreach.campaign_messages(self.db, "c", "draft")]
        outreach.cmd_personalize(self.db, self.args("personalize", "c", "--redo"), ai=FakeClaude(fail=True))
        self.assertEqual([m["body"] for m in outreach.campaign_messages(self.db, "c", "draft")], before)

    def test_personalize_needs_a_brief(self):
        with self.assertRaises(SystemExit):
            outreach.cmd_personalize(self.db, self.args("personalize", "c"), ai=FakeClaude(reply="x"))


if __name__ == "__main__":
    unittest.main()
