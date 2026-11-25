import contextlib
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import outreach  # noqa: E402

SAMPLE = HERE / "sample_connections.csv"
TEMPLATE = HERE / "templates" / "product-beta.txt"


class OutreachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        self.run_cli("import", str(SAMPLE))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        outreach.main(["--db", self.dbpath, *argv])

    def db(self):
        return outreach.connect(self.dbpath)

    def draft_product(self, *extra):
        self.run_cli("draft", "beta", "--template", str(TEMPLATE), "--sender-name", "Sam",
                     "--var", "product=Acme", "--var", "audience=founders",
                     "--var", "problem=AI search visibility", *extra)

    def test_import_skips_preamble_and_is_idempotent(self):
        self.run_cli("import", str(SAMPLE))
        rows = self.db().execute("SELECT * FROM contacts ORDER BY id").fetchall()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["first_name"], "Jane")
        self.assertEqual(rows[0]["connected_on"], "2024-01-15")

    def test_one_contact_per_person_however_the_link_is_encoded(self):
        db = self.db()
        # an older database: the same person stored twice, the encoded twin already messaged
        db.execute("INSERT INTO contacts (key, first_name, last_name, url) VALUES (?,?,?,?)",
                   ("https://www.linkedin.com/in/%c3%a9lise-m", "Élise", "M", "https://www.linkedin.com/in/%C3%A9lise-m"))
        db.execute("INSERT INTO contacts (key, first_name, last_name, url) VALUES (?,?,?,?)",
                   ("https://www.linkedin.com/in/élise-m", "Élise", "M", "https://www.linkedin.com/in/élise-m"))
        db.commit()
        self.draft_product()
        db = self.db()
        twins = [r["id"] for r in db.execute("SELECT id FROM contacts WHERE first_name='Élise' ORDER BY id")]
        db.execute("UPDATE messages SET status='sent' WHERE contact_id=?", (twins[0],))
        db.execute("PRAGMA user_version = 0")  # as in a database made before links were decoded
        db.commit()
        db = self.db()  # opening an older database folds the twins together, once
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], outreach.SCHEMA_VERSION)
        rows = db.execute("SELECT id, key, do_not_contact FROM contacts WHERE first_name='Élise' ORDER BY id").fetchall()
        self.assertEqual([(r["key"], r["do_not_contact"]) for r in rows],
                         [("https://www.linkedin.com/in/élise-m", 0), (f"merged:{twins[1]}", 1)])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE contact_id=?", (twins[1],)).fetchone()[0], 0)
        # importing the person again, under either spelling, updates the one contact
        rows = [dict(first_name="Élise", last_name="M", url=u, email="", company="New Co", position="",
                     connected_on="") for u in ("https://www.linkedin.com/in/%C3%A9lise-m/", "https://www.linkedin.com/in/élise-m")]
        self.assertEqual(outreach.save_contacts(db, rows), (0, 2))
        self.assertEqual(db.execute("SELECT company FROM contacts WHERE id=?", (twins[0],)).fetchone()[0], "New Co")

    def test_filters(self):
        args = outreach.build_parser().parse_args(
            ["list", "--title", "partner", "--title", "principal", "--exclude-company", "northwind"])
        names = [r["first_name"] for r in outreach.select_contacts(self.db(), args)]
        self.assertEqual(names, ["Jane"])

    def test_tag_sorts_people_and_filters_and_orders_by_group(self):
        class Fake:
            name = "fake"
            prompts = []

            def generate(self, prompt):
                self.prompts.append(prompt)
                ids = [int(x.split(":")[0]) for x in prompt.split("company):\n")[1].split("\n\nReply")[0].splitlines()]
                groups = {1: ["investor"], 2: ["founder", "travel"], 3: ["engineer"], 4: ["Investor"], 5: ["bogus"]}
                return "Sure:\n```json\n" + str({str(i): groups.get(i, []) for i in ids}).replace("'", '"') + "\n```"

        self.assertEqual(outreach.parse_tag_spec("founder: started a company\ninvestor\nFounder: dup"),
                         [("founder", "started a company"), ("investor", "")])
        self.assertEqual(outreach.parse_tag_spec("Travel, Founder"), [("travel", ""), ("founder", "")])
        fake = Fake()
        args = outreach.build_parser().parse_args(["--db", self.dbpath, "tag", "--tags", "investor, founder, travel, engineer"])
        db = self.db()
        outreach.cmd_tag(db, args, ai=fake)
        self.assertEqual(len(fake.prompts), 1)
        self.assertIn("remote patient monitoring", fake.prompts[0])
        tags = {r["id"]: outreach.tags_of(r) for r in db.execute("SELECT * FROM contacts")}
        self.assertEqual(tags, {1: ["investor"], 2: ["founder", "travel"], 3: ["engineer"], 4: ["investor"], 5: []})
        # already tagged: nothing to do without --redo
        outreach.cmd_tag(db, args, ai=fake)
        self.assertEqual(len(fake.prompts), 1)
        # filters
        by_tag = outreach.build_parser().parse_args(["list", "--tag", "investor", "--tag", "travel"])
        self.assertEqual(sorted(r["id"] for r in outreach.select_contacts(db, by_tag)), [1, 2, 4])
        not_tag = outreach.build_parser().parse_args(["list", "--exclude-tag", "investor"])
        self.assertEqual(sorted(r["id"] for r in outreach.select_contacts(db, not_tag)), [2, 3, 5])
        # drafting queues the priority groups first, then everyone else in the usual order
        self.draft_product("--priority", "founder, investor")
        order = [r["contact_id"] for r in db.execute("SELECT contact_id FROM messages ORDER BY id")]
        self.assertEqual(order[:3], [2, 1, 4])  # founder, then investors by connection date
        self.assertEqual(sorted(order[3:]), [3, 5])

    def test_render_fallbacks(self):
        c = self.db().execute("SELECT * FROM contacts WHERE first_name='Tom'").fetchone()
        out = outreach.render("Hi {first_name} at {company|your team}, from {sender_name}", c, {"name": "Sam"})
        self.assertEqual(out, "Hi Tom at your team, from Sam")
        with self.assertRaises(KeyError):
            outreach.render("{sender_product}", c, {"name": "Sam"})

    def test_missing_var_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            self.run_cli("draft", "beta", "--template", str(TEMPLATE))

    def test_draft_dedupes_and_respects_dnc(self):
        self.run_cli("dnc", "3")
        self.draft_product("--title", "partner", "--title", "engineer")
        self.draft_product()  # second run only adds new people
        db = self.db()
        bodies = db.execute("SELECT contact_id, body FROM messages").fetchall()
        self.assertEqual(sorted(r["contact_id"] for r in bodies), [1, 2, 4, 5])
        self.assertIn("Hi Jane", bodies[0]["body"])
        self.assertIn("Acme", bodies[0]["body"])

    def test_send_loop_cap_and_actions(self):
        self.draft_product()
        answers = iter(["", "s", "x", "", "q"])
        args = outreach.build_parser().parse_args(["--db", self.dbpath, "send", "beta", "--daily-cap", "3"])
        db = self.db()
        opened = []
        outreach.cmd_send(db, args, prompt=lambda _: next(answers),
                          clipboard=lambda _: True, opener=opened.append)
        statuses = [r["status"] for r in db.execute("SELECT status FROM messages ORDER BY id")]
        self.assertEqual(statuses, ["sent", "skipped", "skipped", "sent", "draft"])
        self.assertEqual(outreach.sent_last_24h(db), 2)
        dnc = db.execute("SELECT first_name FROM contacts WHERE do_not_contact=1").fetchall()
        self.assertEqual(len(dnc), 1)
        self.assertEqual(len(opened), 5)

        # cap of 2 already reached: the loop must stop without prompting
        capped = outreach.build_parser().parse_args(["--db", self.dbpath, "send", "beta", "--daily-cap", "2"])
        def no_prompt(_):
            raise AssertionError("should not prompt once the cap is reached")
        outreach.cmd_send(db, capped, prompt=no_prompt, clipboard=lambda _: True, opener=lambda _: None)
        self.assertEqual(outreach.sent_last_24h(db), 2)

    def test_cooldown_skips_recently_messaged(self):
        self.draft_product()
        self.run_cli("mark", "beta", "1", "sent")
        self.run_cli("draft", "followup", "--template", str(TEMPLATE), "--sender-name", "Sam",
                     "--var", "product=X", "--var", "audience=Y", "--var", "problem=Z")
        ids = [r[0] for r in self.db().execute(
            "SELECT contact_id FROM messages m JOIN campaigns c ON c.id=m.campaign_id WHERE c.name='followup'")]
        self.assertNotIn(1, ids)
        self.assertEqual(len(ids), 4)

    def test_opening_an_up_to_date_database_writes_nothing(self):
        # The web page opens the database on every refresh while a sending run writes to it: an
        # open that wrote (as the duplicate check once did on every open) would fight over the lock.
        self.run_cli("import", str(SAMPLE))
        writer = self.db()
        writer.execute("UPDATE contacts SET company='Held' WHERE id=1")  # an open write transaction
        db = outreach.connect(self.dbpath)  # must not need the write lock
        self.assertEqual(db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 5)
        db.close()
        writer.rollback()

    def test_mark_takes_untidy_ids_and_counts_what_matched(self):
        self.draft_product()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_cli("mark", "beta", "1, 2,999,", "skipped")
        self.assertIn("Marked 2 message(s) as skipped. 1 of the ids have no message", out.getvalue())
        with self.assertRaises(SystemExit) as e:
            self.run_cli("mark", "beta", "one", "sent")
        self.assertIn("contact ids separated by commas", str(e.exception))

    def test_plain_errors_instead_of_tracebacks(self):
        self.run_cli("import", str(SAMPLE))
        for argv, expected in (
                (("review", "nope"), "No campaign named 'nope'. Create one with `draft` first."),
                (("import", str(Path(self.tmp.name) / "missing.csv")), "File not found:"),
                (("draft", "x", "--template", str(Path(self.tmp.name) / "missing.txt")), "File not found:")):
            with self.assertRaises(SystemExit) as e:
                self.run_cli(*argv)
            self.assertIn(expected, str(e.exception))
        self.draft_product()
        with self.assertRaises(SystemExit) as e:
            self.run_cli("autosend", "bta", "--yes")
        self.assertIn("Your campaigns: beta.", str(e.exception))
        self.assertIn("isn't downloaded yet", outreach.friendly_error(Exception(
            "BrowserType.launch_persistent_context: Executable doesn't exist at /x/chrome")))
        self.assertIsNone(outreach.friendly_error(ValueError("a real bug")))

    def test_check_reports_the_setup(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):  # exits 1 without Playwright
            self.run_cli("check", "--profile-dir", str(Path(self.tmp.name) / "no-login"))
        text = out.getvalue()
        self.assertIn("Not logged in to LinkedIn yet", text)
        self.assertIn("5 connection(s), 0 campaign(s)", text)

    def test_export_import_roundtrip(self):
        self.draft_product()
        out = str(Path(self.tmp.name) / "drafts.csv")
        self.run_cli("export", "beta", out)
        rows = list(csv.DictReader(open(out, encoding="utf-8")))
        rows[0]["message"] = "Custom note for Jane"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        self.run_cli("import-drafts", "beta", out)
        body = self.db().execute("SELECT body FROM messages WHERE contact_id=?",
                                 (int(rows[0]["contact_id"]),)).fetchone()[0]
        self.assertEqual(body, "Custom note for Jane")


if __name__ == "__main__":
    unittest.main()
