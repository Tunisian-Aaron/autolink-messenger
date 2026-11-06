"""`replies`, `delete-drafts` and `delete-campaign`, with a local mock of LinkedIn's inbox."""

import datetime as dt
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

INBOX = """<!doctype html><html><head><meta charset="utf-8"></head><body><main>
<ul class="msg-conversations-container__conversations-list">
<li><div>Jane Doe</div><time>4:46 PM</time><p>Jane: Thanks, will try it this weekend!</p></li>
<li><div>Raj Patel</div><time>Nov 3</time><p>You: Hi Raj, Hope all is well</p></li>
<li><div>Ana Silva</div><time>Jan 2, 2020</time><p>Ana: an old thread from years ago</p><span>1</span></li>
<li><div>Someone Else</div><time>Nov 1</time><p>Someone: unrelated</p></li>
</ul></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(INBOX.encode())


class WhenTest(unittest.TestCase):
    def test_parse_inbox_when(self):
        today = dt.date(2026, 11, 5)
        self.assertEqual(outreach.parse_inbox_when("4:46 PM", today), today)
        self.assertEqual(outreach.parse_inbox_when("Nov 3", today), dt.date(2026, 11, 3))
        self.assertEqual(outreach.parse_inbox_when("Dec 20", today), dt.date(2025, 12, 20))  # last year
        self.assertEqual(outreach.parse_inbox_when("Jan 2, 2020", today), dt.date(2020, 1, 2))
        self.assertIsNone(outreach.parse_inbox_when("Yesterday", today))


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        outreach.main(["--db", self.dbpath, "import", str(HERE / "sample_connections.csv")])
        outreach.main(["--db", self.dbpath, "draft", "c", "--template", str(HERE / "templates/product-beta.txt"),
                       "--sender-name", "Sam", "--var", "product=P", "--var", "audience=A", "--var", "problem=Q"])
        self.db = outreach.connect(self.dbpath)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def mark_sent(self, first, sent_at="2026-11-04T10:00:00"):
        self.db.execute("UPDATE messages SET status='sent', sent_at=? WHERE contact_id="
                        "(SELECT id FROM contacts WHERE first_name=?)", (sent_at, first))
        self.db.commit()

    def test_replies_from_inbox_data(self):
        self.mark_sent("Jane")
        self.mark_sent("Raj")
        self.mark_sent("Ana")
        inbox = [{"name": "Jane Doe", "when": "4:46 PM", "preview": "Jane: Thanks, will try it!", "from_me": False},
                 {"name": "Raj Patel", "when": "Nov 3", "preview": "You: Hi Raj", "from_me": True},
                 {"name": "Ana Silva", "when": "Jan 2, 2020", "preview": "Ana: old thread", "from_me": False}]
        n = outreach.cmd_replies(self.db, None, inbox=inbox)
        self.assertEqual(n, 1)
        st = {r["first_name"]: (r["status"], r["reply"]) for r in self.db.execute(
            "SELECT c.first_name, m.status, m.reply FROM messages m JOIN contacts c ON c.id=m.contact_id")}
        self.assertEqual(st["Jane"], ("replied", "Jane: Thanks, will try it!"))
        self.assertEqual(st["Raj"][0], "sent")  # last message is ours
        self.assertEqual(st["Ana"][0], "sent")  # older than our message

    def test_delete_drafts_keeps_sent(self):
        self.mark_sent("Jane")
        outreach.main(["--db", self.dbpath, "delete-drafts", "c"])
        rows = self.db.execute("SELECT status FROM messages").fetchall()
        self.assertEqual([r["status"] for r in rows], ["sent"])
        with self.assertRaises(SystemExit):  # a campaign with sent messages needs --force
            outreach.main(["--db", self.dbpath, "delete-campaign", "c"])
        outreach.main(["--db", self.dbpath, "delete-campaign", "c", "--force"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class InboxBrowserTest(DbTest):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_read_inbox_and_mark(self):
        self.mark_sent("Jane")
        self.mark_sent("Raj")
        args = outreach.build_parser().parse_args(
            ["--db", self.dbpath, "replies", "--headless", "--url", self.base + "/messaging/",
             "--profile-dir", str(Path(self.tmp.name) / "profile")])
        n = outreach.cmd_replies(self.db, args, page_factory=lambda: lb.browser(args.profile_dir, headless=True))
        self.assertEqual(n, 1)
        st = {r["first_name"]: r["status"] for r in self.db.execute(
            "SELECT c.first_name, m.status FROM messages m JOIN contacts c ON c.id=m.contact_id")}
        self.assertEqual((st["Jane"], st["Raj"]), ("replied", "sent"))


if __name__ == "__main__":
    unittest.main()
