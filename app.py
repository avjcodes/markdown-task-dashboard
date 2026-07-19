"""Markdown task dashboard.

A local production dashboard for solo projects with a deadline. Your tasks
live in plain markdown notes; this page reads them, shows a countdown clock
of the estimated hours remaining, projects a finish date at your working
pace, and measures it against a deadline you set below.

Run:  python app.py        -> opens http://localhost:8787
      python app.py --dry  -> prints the computed state as JSON and exits
      (--no-browser skips the auto-open)

Data lives in ./tasks:
  tasks/production/*.md  - one note per ENTITY (a piece of content your
        project needs: a character, a level, a chapter, an illustration).
        Frontmatter carries `units: [a, b, ...]` (every asset the entity
        needs) and `done: [a, ...]` (which are finished). Checking a box on
        the page rewrites ONLY the `done:` line, so the notes stay yours and
        any editor (including Obsidian) can share the files.
  tasks/*.md             - feature/code tasks with status/estimate
        frontmatter. Status dropdowns rewrite ONLY the `status:` line.

Standard library only. No installs, no database, no build step.
"""
import json
import re
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS_DIR = ROOT / "tasks"
PROD_DIR = TASKS_DIR / "production"
SUMMARY_FILE = ROOT / "summary.md"
PORT = 8787

# ------------------------- edit this block to fit your project -------------

PROJECT_NAME = "My Project"
DEADLINE = "2026-12-01"          # the date the countdown measures against
DEADLINE_NAME = "Launch"         # what that date is called on the page
MILESTONE_LABEL = "milestone"    # chip shown on notes tagged `milestone: true`

# Effort model: minutes per unit type. Tune freely, the clock recomputes.
# Ground these in your real pacing, not your optimism.
UNIT_MINUTES = {
    "design": 20,
    "anim": 30,
    "floor": 90,
    "backdrop": 90,
    "build": 180,
    "polish": 45,
}
DEFAULT_UNIT = "anim"            # minutes used for unit names not listed above
FEATURE_MINUTES = {"S": 60, "M": 150, "L": 360}   # t-shirt task estimates

# Which entity `type:` lands in which section, and how sections are shown.
SECTION_OF_TYPE = {
    "environment": "levels", "monster": "levels", "boss": "levels",
    "companion": "characters", "hero": "characters",
    "interior": "interiors", "polish": "polish",
}
SECTION_ORDER = ["levels", "characters", "interiors", "polish", "features"]
SECTION_LABELS = {
    "levels": "Levels", "characters": "Characters", "interiors": "Interiors",
    "polish": "Polish", "features": "Code & feature tasks",
}

# ---------------------------------------------------------------------------

DIRECTIVE_FILE = TASKS_DIR / "directive.md"
LOG_FILE = TASKS_DIR / "production-log.md"
STATUSES = ("todo", "in-progress", "blocked", "done")


def feature_minutes(est):
    """S/M/L sizes, or explicit numeric hours like '22h' / '0.5h'."""
    if est in FEATURE_MINUTES:
        return FEATURE_MINUTES[est]
    m = re.match(r"^([\d.]+)\s*h$", str(est).strip(), re.I)
    return int(float(m.group(1)) * 60) if m else 150


def unit_minutes(entity_type, unit):
    if unit in UNIT_MINUTES:
        return UNIT_MINUTES[unit]
    if entity_type == "polish":
        return UNIT_MINUTES["polish"]
    return UNIT_MINUTES[DEFAULT_UNIT]


# ---------- frontmatter I/O ----------

def _parse_value(raw):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [v.strip().strip("'\"") for v in inner.split(",") if v.strip()] if inner else []
    return raw.strip("'\"")


def _read_frontmatter(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not m:
        return None, text, 0
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, val = line.partition(":")
            meta[key.strip()] = _parse_value(val)
    return meta, text, m.end()


def _rewrite_fm_line(path, key, new_line):
    """Replace exactly one `key:` line inside the frontmatter block."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        raise ValueError("no frontmatter in %s" % path.name)
    head, tail = text[:m.end()], text[m.end():]
    new_head, n = re.subn(r"(?m)^%s:\s*.*$" % re.escape(key), new_line, head, count=1)
    if n == 0:
        raise ValueError("no %s line in %s" % (key, path.name))
    path.write_text(new_head + tail, encoding="utf-8")


# ---------- production entities ----------

def load_production():
    entities = []
    if not PROD_DIR.is_dir():
        return entities
    for path in sorted(PROD_DIR.glob("*.md")):
        meta, text, fm_end = _read_frontmatter(path)
        if meta is None or "units" not in meta:
            continue
        units = meta["units"] if isinstance(meta["units"], list) else [meta["units"]]
        done = meta.get("done", [])
        if isinstance(done, str):
            done = [done] if done else []
        etype = meta.get("type", "polish")
        remaining = [u for u in units if u not in done]
        entities.append({
            "id": meta.get("id") or path.stem,
            "type": etype,
            "section": SECTION_OF_TYPE.get(etype, "polish"),
            "name": meta.get("name") or path.stem,
            "group": int(meta["group"]) if str(meta.get("group", "")).isdigit() else None,
            "units": units,
            "done": [u for u in done if u in units],
            "milestone": meta.get("milestone") == "true",
            "notes": text[fm_end:].strip(),
            "minutes_left": sum(unit_minutes(etype, u) for u in remaining),
        })
    return entities


def set_unit(entity_id, unit, is_done):
    for path in PROD_DIR.glob("*.md"):
        meta, _, _ = _read_frontmatter(path)
        if meta is None or (meta.get("id") or path.stem) != entity_id:
            continue
        units = meta["units"] if isinstance(meta.get("units"), list) else []
        if unit not in units:
            raise ValueError("no unit %s on %s" % (unit, entity_id))
        done = meta.get("done", [])
        if isinstance(done, str):
            done = [done] if done else []
        done = [u for u in done if u != unit] + ([unit] if is_done else [])
        ordered = [u for u in units if u in done]  # keep units order, drop strays
        _rewrite_fm_line(path, "done", "done: [%s]" % ", ".join(ordered))
        return
    raise KeyError("no entity with id %s" % entity_id)


# ---------- feature tasks ----------

def load_features():
    tasks = []
    if not TASKS_DIR.is_dir():
        return tasks
    for path in sorted(TASKS_DIR.glob("*.md")):
        meta, text, fm_end = _read_frontmatter(path)
        if meta is None or "units" in meta or "status" not in meta:
            continue  # not a task note (e.g. production-log.md)
        est = meta.get("estimate", "M")
        status = meta.get("status", "todo")
        tasks.append({
            "id": meta.get("id") or path.stem,
            "name": meta.get("title") or path.stem,
            "status": status,
            "estimate": est,
            "area": meta.get("area", "misc"),
            "milestone": meta.get("milestone") == "true",
            "notes": text[fm_end:].strip(),
            "minutes_left": 0 if status == "done" else feature_minutes(est),
        })
    return tasks


# ---------- directive + velocity ----------

def load_directive():
    if not DIRECTIVE_FILE.exists():
        return None
    meta, text, fm_end = _read_frontmatter(DIRECTIVE_FILE)
    if meta is None:
        return None
    return {
        "directive": meta.get("directive", ""),
        "why": meta.get("why", ""),
        "updated": meta.get("updated", ""),
        "body": text[fm_end:].strip(),
    }


def load_velocity(days=14):
    """Sum actual hours from production-log.md table rows within the window.

    Rows look like: | 2026-07-10 | item | 2.5 | 3 | notes |
    (date | what | estimated hours | actual hours | notes)
    Returns None until the log has at least one numeric 'actual' entry.
    """
    if not LOG_FILE.exists():
        return None
    total, n, newest, oldest = 0.0, 0, None, None
    today = date.today()
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not re.match(r"^\d{4}-\d{2}-\d{2}$", cells[0]):
            continue
        try:
            d = date.fromisoformat(cells[0])
            actual = float(cells[3])
        except ValueError:
            continue
        if (today - d).days > days:
            continue
        total += actual
        n += 1
        newest = max(newest, d) if newest else d
        oldest = min(oldest, d) if oldest else d
    if n == 0:
        return None
    span_days = max((today - oldest).days + 1, 1)
    return {
        "window_days": days,
        "entries": n,
        "actual_hours": round(total, 1),
        "hours_per_day": round(total / span_days, 2),
    }


def set_status(task_id, new_status):
    if new_status not in STATUSES:
        raise ValueError("bad status: %s" % new_status)
    for path in TASKS_DIR.glob("*.md"):
        meta, _, _ = _read_frontmatter(path)
        if meta is None or (meta.get("id") or path.stem) != task_id:
            continue
        _rewrite_fm_line(path, "status", "status: " + new_status)
        return
    raise KeyError("no task with id %s" % task_id)


# ---------- state ----------

def build_state():
    entities = load_production()
    features = load_features()

    sections = {}
    for e in entities:
        s = sections.setdefault(e["section"], {"minutes_left": 0, "units_done": 0, "units_total": 0})
        s["minutes_left"] += e["minutes_left"]
        s["units_done"] += len(e["done"])
        s["units_total"] += len(e["units"])
    fs = sections.setdefault("features", {"minutes_left": 0, "units_done": 0, "units_total": 0})
    for t in features:
        fs["minutes_left"] += t["minutes_left"]
        fs["units_total"] += 1
        if t["status"] == "done":
            fs["units_done"] += 1

    ms_minutes = (sum(e["minutes_left"] for e in entities if e["milestone"]) +
                  sum(t["minutes_left"] for t in features if t["milestone"]))
    ms_done = (sum(len(e["done"]) for e in entities if e["milestone"]) +
               sum(1 for t in features if t["milestone"] and t["status"] == "done"))
    ms_total = (sum(len(e["units"]) for e in entities if e["milestone"]) +
                sum(1 for t in features if t["milestone"]))

    return {
        "date": date.today().isoformat(),
        "project_name": PROJECT_NAME,
        "deadline": DEADLINE,
        "deadline_name": DEADLINE_NAME,
        "milestone_label": MILESTONE_LABEL,
        "section_labels": SECTION_LABELS,
        "summary": SUMMARY_FILE.read_text(encoding="utf-8") if SUMMARY_FILE.exists() else "",
        "total_minutes_left": sum(s["minutes_left"] for s in sections.values()),
        "milestone_minutes_left": ms_minutes,
        "milestone_units_done": ms_done,
        "milestone_units_total": ms_total,
        "directive": load_directive(),
        "velocity": load_velocity(),
        "sections": {k: sections[k] for k in SECTION_ORDER if k in sections},
        "production": entities,
        "features": features,
    }


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html")
        elif self.path == "/api/state":
            self._send(200, build_state())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            if self.path == "/api/unit":
                set_unit(body["id"], body["unit"], bool(body["done"]))
            elif self.path == "/api/status":
                set_status(body["id"], body["status"])
            else:
                return self._send(404, {"error": "not found"})
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        self._send(200, build_state())


def main():
    if "--dry" in sys.argv:
        print(json.dumps(build_state(), indent=2))
        return
    if not TASKS_DIR.is_dir():
        print("No tasks folder at %s" % TASKS_DIR)
        return
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://localhost:%d" % PORT
    print("%s dashboard -> %s   (Ctrl+C to stop)" % (PROJECT_NAME, url))
    if "--no-browser" not in sys.argv:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
