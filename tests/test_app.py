"""Tests for the dashboard's parsing, effort math, and note writeback.

Run from the repo root:  python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


ENTITY = """---
id: monster-01
type: monster
name: "Cave Bat"
units: [design, idle, attack]
done: [design]
milestone: true
---
Body notes here.
"""

TASK = """---
id: save-system
title: "Save / load system"
status: in-progress
estimate: M
area: code
milestone: true
---
"""


class TempVault(unittest.TestCase):
    """Base: point the app at a throwaway tasks folder for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = (app.TASKS_DIR, app.PROD_DIR, app.DIRECTIVE_FILE,
                       app.LOG_FILE, app.SUMMARY_FILE)
        app.TASKS_DIR = root / "tasks"
        app.PROD_DIR = app.TASKS_DIR / "production"
        app.DIRECTIVE_FILE = app.TASKS_DIR / "directive.md"
        app.LOG_FILE = app.TASKS_DIR / "production-log.md"
        app.SUMMARY_FILE = root / "summary.md"
        app.PROD_DIR.mkdir(parents=True)

    def tearDown(self):
        (app.TASKS_DIR, app.PROD_DIR, app.DIRECTIVE_FILE,
         app.LOG_FILE, app.SUMMARY_FILE) = self._saved
        self._tmp.cleanup()


class TestEffortMath(unittest.TestCase):
    def test_tshirt_sizes(self):
        self.assertEqual(app.feature_minutes("S"), 60)
        self.assertEqual(app.feature_minutes("M"), 150)
        self.assertEqual(app.feature_minutes("L"), 360)

    def test_explicit_hours(self):
        self.assertEqual(app.feature_minutes("12h"), 720)
        self.assertEqual(app.feature_minutes("0.5h"), 30)
        self.assertEqual(app.feature_minutes(" 1.5 H "), 90)

    def test_unknown_estimate_falls_back_to_medium(self):
        self.assertEqual(app.feature_minutes("XXL"), 150)
        self.assertEqual(app.feature_minutes(None), 150)

    def test_unit_minutes_fallbacks(self):
        self.assertEqual(app.unit_minutes("monster", "design"), app.UNIT_MINUTES["design"])
        self.assertEqual(app.unit_minutes("polish", "mystery"), app.UNIT_MINUTES["polish"])
        self.assertEqual(app.unit_minutes("monster", "mystery"), app.UNIT_MINUTES[app.DEFAULT_UNIT])


class TestFrontmatter(unittest.TestCase):
    def test_parse_value_lists_and_scalars(self):
        self.assertEqual(app._parse_value("[a, b, c]"), ["a", "b", "c"])
        self.assertEqual(app._parse_value("[]"), [])
        self.assertEqual(app._parse_value('"Cave Bat"'), "Cave Bat")
        self.assertEqual(app._parse_value("plain"), "plain")

    def test_read_frontmatter_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "note.md"
            p.write_bytes(b"---\r\nid: x\r\nunits: [a, b]\r\n---\r\nbody")
            meta, text, fm_end = app._read_frontmatter(p)
            self.assertEqual(meta["id"], "x")
            self.assertEqual(meta["units"], ["a", "b"])
            self.assertEqual(text[fm_end:], "body")

    def test_no_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "note.md"
            p.write_text("just prose", encoding="utf-8")
            meta, text, fm_end = app._read_frontmatter(p)
            self.assertIsNone(meta)
            self.assertEqual(fm_end, 0)


class TestWriteback(TempVault):
    def test_set_unit_rewrites_only_the_done_line(self):
        note = app.PROD_DIR / "monster-01.md"
        note.write_text(ENTITY, encoding="utf-8")
        app.set_unit("monster-01", "idle", True)
        after = note.read_text(encoding="utf-8")
        self.assertIn("done: [design, idle]", after)
        # everything except the done: line is untouched
        strip = lambda s: [l for l in s.splitlines() if not l.startswith("done:")]
        self.assertEqual(strip(ENTITY), strip(after))

    def test_set_unit_keeps_units_order(self):
        note = app.PROD_DIR / "monster-01.md"
        note.write_text(ENTITY.replace("done: [design]", "done: [attack]"), encoding="utf-8")
        app.set_unit("monster-01", "design", True)
        self.assertIn("done: [design, attack]",
                      note.read_text(encoding="utf-8"))

    def test_set_unit_unchecks(self):
        note = app.PROD_DIR / "monster-01.md"
        note.write_text(ENTITY, encoding="utf-8")
        app.set_unit("monster-01", "design", False)
        self.assertIn("done: []", note.read_text(encoding="utf-8"))

    def test_set_unit_errors(self):
        (app.PROD_DIR / "monster-01.md").write_text(ENTITY, encoding="utf-8")
        with self.assertRaises(ValueError):
            app.set_unit("monster-01", "not-a-unit", True)
        with self.assertRaises(KeyError):
            app.set_unit("ghost", "design", True)

    def test_set_status_validates(self):
        (app.TASKS_DIR / "save-system.md").write_text(TASK, encoding="utf-8")
        app.set_status("save-system", "done")
        self.assertIn("status: done",
                      (app.TASKS_DIR / "save-system.md").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            app.set_status("save-system", "napping")


class TestLoadAndState(TempVault):
    def test_entity_minutes_and_stray_done(self):
        note = ENTITY.replace("done: [design]", "done: [design, ghost-unit]")
        (app.PROD_DIR / "monster-01.md").write_text(note, encoding="utf-8")
        (e,) = app.load_production()
        self.assertEqual(e["done"], ["design"])  # stray dropped
        expect = app.unit_minutes("monster", "idle") + app.unit_minutes("monster", "attack")
        self.assertEqual(e["minutes_left"], expect)

    def test_features_skip_non_task_notes(self):
        (app.TASKS_DIR / "save-system.md").write_text(TASK, encoding="utf-8")
        (app.TASKS_DIR / "production-log.md").write_text("| a | b |", encoding="utf-8")
        (app.TASKS_DIR / "freeform.md").write_text("no frontmatter", encoding="utf-8")
        tasks = app.load_features()
        self.assertEqual([t["id"] for t in tasks], ["save-system"])
        self.assertEqual(tasks[0]["minutes_left"], 150)

    def test_build_state_totals(self):
        (app.PROD_DIR / "monster-01.md").write_text(ENTITY, encoding="utf-8")
        (app.TASKS_DIR / "save-system.md").write_text(TASK, encoding="utf-8")
        state = app.build_state()
        self.assertEqual(state["total_minutes_left"],
                         sum(s["minutes_left"] for s in state["sections"].values()))
        # both fixtures are milestone-tagged, so milestone == total
        self.assertEqual(state["milestone_minutes_left"], state["total_minutes_left"])
        self.assertEqual(state["milestone_units_total"], 3 + 1)

    def test_velocity_window(self):
        today = date.today()
        rows = ["| date | what | est | actual | notes |",
                "| %s | thing | 2 | 1.5 | |" % (today - timedelta(days=2)),
                "| %s | thing | 2 | 2.5 | |" % (today - timedelta(days=4)),
                "| %s | old | 2 | 99 | |" % (today - timedelta(days=40))]
        app.LOG_FILE.write_text("\n".join(rows), encoding="utf-8")
        v = app.load_velocity(days=14)
        self.assertEqual(v["entries"], 2)
        self.assertEqual(v["actual_hours"], 4.0)
        self.assertEqual(v["hours_per_day"], round(4.0 / 5, 2))

    def test_velocity_none_without_rows(self):
        self.assertIsNone(app.load_velocity())
        app.LOG_FILE.write_text("| header | only |", encoding="utf-8")
        self.assertIsNone(app.load_velocity())


if __name__ == "__main__":
    unittest.main()
