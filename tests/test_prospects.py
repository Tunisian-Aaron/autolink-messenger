"""People you aren't connected to yet: adding them, keeping them apart from connections, and
the character limit on the note. No browser needed (the invitation itself is in test_autosend)."""

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import outreach  # noqa: E402


class ProspectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        outreach.main(["--db", self.dbpath, "import", str(HERE / "sample_connections.csv")])
        self.db = outreach.connect(self.dbpath)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_cmd(self, *argv):
        outreach.main(["--db", self.dbpath, *argv])

    def test_parse_links_in_any_spelling(self):
        rows = outreach.parse_prospects(
            "https://www.linkedin.com/in/pat-lee-12/?trk=x\n"
            "linkedin.com/in/raj, Raj Patel, Acme, CTO\n"
            "Jo Roe\thttps://uk.linkedin.com/in/roe\n"
            "not a link\n"
            "https://www.linkedin.com/in/pat-lee-12\n")
        self.assertEqual([r["url"] for r in rows], ["https://www.linkedin.com/in/pat-lee-12",
                                                    "https://www.linkedin.com/in/raj", "https://www.linkedin.com/in/roe"])
        self.assertEqual((rows[1]["first_name"], rows[1]["last_name"], rows[1]["company"], rows[1]["position"]),
                         ("Raj", "Patel", "Acme", "CTO"))
        self.assertEqual((rows[2]["first_name"], rows[2]["last_name"]), ("Jo", "Roe"))

    def test_add_draft_and_keep_apart_from_connections(self):
        existing = self.db.execute("SELECT url FROM contacts LIMIT 1").fetchone()["url"]
        links = Path(self.tmp.name) / "links.txt"
        links.write_text(f"https://www.linkedin.com/in/pat-lee, Pat Lee, Acme, CTO\n{existing}, Some One\n")
        self.run_cmd("add-prospects", "--file", str(links), "https://www.linkedin.com/in/quin-roe, Quin Roe")
        flags = dict(self.db.execute("SELECT first_name, prospect FROM contacts").fetchall())
        self.assertEqual((flags["Pat"], flags["Quin"]), (1, 1))
        self.assertEqual(sum(flags.values()), 2)  # the existing connection stayed a connection

        # a normal campaign never picks them up; a --prospects one picks only them
        template = str(HERE / "templates/connect-note.txt")
        common = ["--sender-name", "Sam", "--var", "product=P"]
        self.run_cmd("draft", "notes", "--prospects", "--template", template, *common)
        self.run_cmd("draft", "msgs", "--template", template, *common)
        names = lambda camp: sorted(m["first_name"] for m in outreach.campaign_messages(self.db, camp))  # noqa: E731
        self.assertEqual(names("notes"), ["Pat", "Quin"])
        self.assertEqual(len(names("msgs")), 5)
        self.assertNotIn("Pat", names("msgs"))
        for m in outreach.campaign_messages(self.db, "notes"):
            self.assertLessEqual(len(m["body"]), outreach.NOTE_CHARS)

        # once they accept, the next connections import makes them a normal contact
        outreach.save_contacts(self.db, [dict(first_name="Pat", last_name="Lee", url="https://www.linkedin.com/in/pat-lee",
                                              email="", company="Acme", position="CTO", connected_on="2026-01-02")])
        self.assertEqual(self.db.execute("SELECT prospect FROM contacts WHERE first_name='Pat'").fetchone()[0], 0)

    def test_note_limit_is_checked_in_characters(self):
        jane = {"first_name": "Jane", "last_name": "Doe", "position": "", "company": ""}
        base = "Hi Jane,\n\nWould love to connect.\n\nSam"
        long = "Hi Jane,\n\n" + "word " * 50 + "\n\nSam"
        self.assertIn("characters; LinkedIn's note box takes 200",
                      " ".join(outreach.rewrite_problems(base, long, jane, max_chars=200)))
        self.assertEqual(outreach.rewrite_problems(base, "Hi Jane,\n\nYour CTO years: let's connect.\n\nSam", jane,
                                                   max_chars=200), [])
        prompt = outreach.personalization_prompt(base, jane, "goal", max_chars=200)
        self.assertIn("At most 200 characters", prompt)
        self.assertNotIn("existing connections", prompt)


if __name__ == "__main__":
    unittest.main()
