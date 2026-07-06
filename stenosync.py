#!/usr/bin/env python3
"""
StenoSync — a standalone editor that keeps a Plover JSON dictionary and a
CRE-RTF dictionary in sync.

Model: the two files ARE the truth. On open, both are read and diffed. When you
add a word it is written to both files atomically (both or neither). A side
panel flags entries that exist in one file but not the other, and entries that
exist in both but disagree — it flags, it never auto-fixes.

Plain word  -> one translation field, mirrored to both files.
Formatted   -> separate JSON and RTF fields, authored by you (no interpretation).

Run: python3 stenosync.py
"""
import bisect
import json
import os
import re
import sys
import tempfile

from PyQt6.QtCore import Qt, QSettings, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

# ───────────────────────── format core ─────────────────────────

def parse_plover_json(text):
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Plover JSON must be a top-level object")
    return {str(k): str(v) for k, v in data.items()}


def serialize_plover_json(entries):
    return json.dumps(dict(sorted(entries.items())), ensure_ascii=False, indent=0)


# CRE entries look like: {\*\cxs STKPW}word — the translation may contain
# RTF control words (\cxds ing) and braced metadata groups (entrydate etc.),
# all of which must survive a read-write cycle untouched.
_ENTRY_RE = re.compile(r"\{\\\*\\cxs ([^}]+)\}(.*?)(?=\{\\\*\\cxs |$)")
_META_GROUP_RE = re.compile(r"\{\\\*\\cxsvatdict[a-z]*[^}]*\}")
CRE_HEADER = (r"{\rtf1\ansi\ansicpg1252\deff0\deflang1033"
              r"{\fonttbl{\f0\fnil Courier New;}}"
              r"{\*\cxsystem StenoSync}\cxdict")


def parse_cre_rtf(text):
    """Parse a CRE RTF dictionary. Returns (entries, header).

    Translations are kept RAW — control words, spacing, and metadata groups
    intact — so serialize_cre_rtf(parse_cre_rtf(x)) is lossless per entry.
    header is the original file preamble (before the first entry), reused on
    write so the source system's header survives."""
    flat = text.replace("\r", "").replace("\n", "")
    first = flat.find(r"{\*\cxs ")
    header = flat[:first] if first >= 0 else None
    body = flat[:-1] if flat.endswith("}") else flat  # drop final closing brace
    entries = {}
    for m in _ENTRY_RE.finditer(body):
        steno = m.group(1).strip()
        if steno:
            entries[steno] = m.group(2)
    return entries, header


def serialize_cre_rtf(entries, header=None):
    lines = [header or CRE_HEADER]
    for steno, translation in sorted(entries.items()):
        lines.append(r"{\*\cxs %s}%s" % (steno, translation))
    lines.append("}")
    return "\r\n".join(lines)


def normalized(value):
    """Comparison form of a translation: CRE metadata groups (entry dates,
    flags) stripped and whitespace trimmed. Raw values are what get written;
    normalized values are what get compared and displayed."""
    return _META_GROUP_RE.sub("", value).strip()


def diff(json_entries, rtf_entries):
    jk, rk = set(json_entries), set(rtf_entries)
    missing_in_json = sorted(rk - jk)   # present in RTF only
    missing_in_rtf = sorted(jk - rk)    # present in JSON only
    conflicts = sorted(s for s in (jk & rk)
                       if normalized(json_entries[s]) != normalized(rtf_entries[s]))
    return missing_in_json, missing_in_rtf, conflicts


def _atomic_write(path, text):
    """Write text to a temp file in the same dir, fsync, then replace."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_both(json_path, json_entries, rtf_path, rtf_entries, rtf_header=None):
    """Write both files. Stage both temps first so a serialize error in one
    doesn't leave the other half-updated. The two os.replace calls are
    back-to-back; true cross-file atomicity isn't possible on a filesystem,
    but failure between them is rare and leaves both files individually valid."""
    json_text = serialize_plover_json(json_entries)
    rtf_text = serialize_cre_rtf(rtf_entries, rtf_header)
    _atomic_write(json_path, json_text)
    _atomic_write(rtf_path, rtf_text)


# ───────────────────────── GUI ─────────────────────────

APP_FONT_SIZE = 13
MAX_TABLE_ROWS = 2000  # cap visible rows — search narrows the rest

DARK_COLORS = {
    "conflict": QColor(180, 60, 60), "missing_j": QColor(160, 130, 40),
    "missing_r": QColor(50, 100, 150), "ignored": QColor(80, 80, 80),
    "later": QColor(100, 70, 120), "planning": QColor(40, 100, 70),
    "incomplete": QColor(90, 70, 50), "text": QColor(255, 255, 255),
    "problem": QColor(255, 100, 100),
    "hint": "#66ccff", "hint_err": "#ee6666",
}
LIGHT_COLORS = {
    "conflict": QColor(255, 175, 175), "missing_j": QColor(255, 224, 150),
    "missing_r": QColor(172, 214, 255), "ignored": QColor(214, 214, 214),
    "later": QColor(224, 200, 240), "planning": QColor(184, 228, 204),
    "incomplete": QColor(235, 214, 188), "text": QColor(20, 20, 20),
    "problem": QColor(178, 24, 24),
    "hint": "#0068a8", "hint_err": "#c22525",
}
COLORS = DARK_COLORS


def apply_theme(app, theme):
    """Apply light or dark palette to the whole app (ported from Steno Type,
    incl. the macOS fix: Qt6's color-scheme hint keeps the title bar and
    native widgets from overriding the palette)."""
    global COLORS
    COLORS = DARK_COLORS if theme == "Dark" else LIGHT_COLORS
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):
        hints.setColorScheme(Qt.ColorScheme.Dark if theme == "Dark"
                             else Qt.ColorScheme.Light)
    from PyQt6.QtGui import QPalette
    p = QPalette()
    R = QPalette.ColorRole
    if theme == "Dark":
        p.setColor(R.Window, QColor(43, 43, 43))
        p.setColor(R.WindowText, QColor(220, 220, 220))
        p.setColor(R.Base, QColor(30, 30, 30))
        p.setColor(R.AlternateBase, QColor(50, 50, 50))
        p.setColor(R.Text, QColor(220, 220, 220))
        p.setColor(R.Button, QColor(53, 53, 53))
        p.setColor(R.ButtonText, QColor(220, 220, 220))
        p.setColor(R.BrightText, QColor(255, 255, 255))
        p.setColor(R.Highlight, QColor(42, 130, 218))
        p.setColor(R.HighlightedText, QColor(255, 255, 255))
        p.setColor(R.ToolTipBase, QColor(50, 50, 50))
        p.setColor(R.ToolTipText, QColor(220, 220, 220))
        p.setColor(R.PlaceholderText, QColor(120, 120, 120))
    else:
        p.setColor(R.Window, QColor(240, 240, 240))
        p.setColor(R.WindowText, QColor(0, 0, 0))
        p.setColor(R.Base, QColor(255, 255, 255))
        p.setColor(R.AlternateBase, QColor(245, 245, 245))
        p.setColor(R.Text, QColor(0, 0, 0))
        p.setColor(R.Button, QColor(240, 240, 240))
        p.setColor(R.ButtonText, QColor(0, 0, 0))
        p.setColor(R.BrightText, QColor(255, 0, 0))
        p.setColor(R.Highlight, QColor(42, 130, 218))
        p.setColor(R.HighlightedText, QColor(255, 255, 255))
        p.setColor(R.ToolTipBase, QColor(255, 255, 220))
        p.setColor(R.ToolTipText, QColor(0, 0, 0))
        p.setColor(R.PlaceholderText, QColor(120, 120, 120))
    app.setPalette(p)
    # Force Qt to re-resolve palette() references in per-widget stylesheets
    for widget in app.allWidgets():
        ss = widget.styleSheet()
        if ss:
            widget.setStyleSheet(ss)
TAGS_FILENAME = "stenosync_tags.json"
PLANNING_FILENAME = "stenosync_planning.json"


def _sidecar_path(json_path, filename):
    return os.path.join(os.path.dirname(os.path.abspath(json_path)), filename)


def _tags_path(json_path):
    return _sidecar_path(json_path, TAGS_FILENAME)


def _planning_path(json_path):
    return _sidecar_path(json_path, PLANNING_FILENAME)


def load_tags(json_path):
    p = _tags_path(json_path)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.loads(f.read() or "{}")
    return {}


def save_tags(json_path, tags):
    _atomic_write(_tags_path(json_path), json.dumps(tags, indent=1))


def load_planning(json_path):
    p = _planning_path(json_path)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.loads(f.read() or "[]")
    return []


def save_planning(json_path, planning):
    _atomic_write(_planning_path(json_path), json.dumps(planning, indent=1))


# ───────────────────────── stroke suggestion ─────────────────────────
# Learns suffix rules from the dictionary itself (no theory hard-coded):
# for every root/derived pair like test=T-EFT / testing=T-EFGT, record the
# stroke transformation, then rank rules for a new translation by how often
# they applied to roots with the same final chord (and, via cmudict.dict if
# present, the same final sound).

_WORD_RE = re.compile(r"^[A-Za-z][a-z'-]*$")
_LEFT_ORDER = "STKPWHRAO*"
_RIGHT_ORDER = "EUFRPBLGTSDZ"
_SUFFIXES = ["ing", "ed", "s", "es", "er", "ers", "est", "ly", "tion",
             "tions", "ment", "ments", "ness", "able", "al", "ally", "ive",
             "ity", "ies", "ied", "ier", "iest", "ful", "less", "ous",
             "ance", "ence", "ism", "ist", "ists", "ized", "ize", "y",
             "en", "or", "age"]
CMUDICT_FILENAME = "cmudict.dict"


def _chord_keys(chord):
    """Chord -> (ok, frozenset of positional keys 'L:T' / 'R:G')."""
    left, _, right = chord.partition("-")
    keys = set()
    for ch in left:
        if ch not in _LEFT_ORDER:
            return False, frozenset()
        keys.add("L:" + ch)
    for ch in right:
        if ch not in _RIGHT_ORDER:
            return False, frozenset()
        keys.add("R:" + ch)
    return True, frozenset(keys)


def _keys_to_chord(keys):
    left = "".join(c for c in _LEFT_ORDER if "L:" + c in keys)
    right = "".join(c for c in _RIGHT_ORDER if "R:" + c in keys)
    return left + "-" + right


def _extract_rule(rstroke, dstroke):
    """What transformation turns the root stroke into the derived stroke?"""
    if dstroke.startswith(rstroke + "/"):
        return ("append", dstroke[len(rstroke) + 1:])
    rc, dc = rstroke.split("/"), dstroke.split("/")
    if len(rc) == len(dc) and rc[:-1] == dc[:-1] and rc[-1] != dc[-1]:
        ok1, k1 = _chord_keys(rc[-1])
        ok2, k2 = _chord_keys(dc[-1])
        if ok1 and ok2 and k1 < k2:
            return ("addkeys", tuple(sorted(k2 - k1)))
        return ("replace_last", rc[-1], dc[-1])
    return None


def _apply_rule(stroke, rule):
    if rule[0] == "append":
        return stroke + "/" + rule[1]
    if rule[0] == "addkeys":
        chords = stroke.split("/")
        ok, keys = _chord_keys(chords[-1])
        if not ok or set(rule[1]) & keys:
            return None  # key already occupied -> rule not applicable
        chords[-1] = _keys_to_chord(keys | set(rule[1]))
        return "/".join(chords)
    if rule[0] == "replace_last":
        chords = stroke.split("/")
        if chords[-1] != rule[1]:
            return None
        chords[-1] = rule[2]
        return "/".join(chords)
    return None


def _rule_desc(rule):
    if rule[0] == "append":
        return "add stroke /" + rule[1]
    if rule[0] == "addkeys":
        keys = "+".join(("-" + k[2:]) if k.startswith("R:") else k[2:]
                        for k in rule[1])
        return "tuck " + keys
    return f"replace {rule[1]} with {rule[2]}"


def _candidate_roots(word, suf):
    """Possible root spellings for word = root (+ spelling change) + suf."""
    stem = word[:-len(suf)]
    if not stem:
        return []
    cands = [stem]                        # test -> testing
    if suf[0] in "aeiouy":
        cands.append(stem + "e")          # make -> making
    if len(stem) >= 2 and stem[-1] == stem[-2]:
        cands.append(stem[:-1])           # run -> running
    if stem.endswith("i"):
        cands.append(stem[:-1] + "y")     # try -> tried
    return cands


def _load_cmudict():
    """word -> final phoneme, from cmudict.dict next to this script
    (optional — suggestions work without it, ranked slightly worse)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        CMUDICT_FILENAME)
    final = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2 and "(" not in parts[0]:
                    final[parts[0]] = parts[-1].rstrip("012")
    return final


class StrokeSuggester:
    """Mined suffix rules + reverse index. build() is CPU-heavy (tens of
    seconds on a 600k dictionary) and runs off the GUI thread; suggest()
    is per-keystroke cheap."""

    def __init__(self):
        self.words = {}        # translation -> set of strokes
        self.rules = {}        # suffix -> {rule: count}
        self.rules_cond = {}   # (suffix, root's last chord) -> {rule: count}
        self.rules_phon = {}   # (suffix, root's final phoneme) -> {rule: count}
        self.phon = {}
        self.pair_count = 0
        self.ready = False

    def build(self, norm_maps):
        """norm_maps: stroke -> normalized-translation dicts (JSON and RTF
        sides). Phrases and formatted entries are filtered out, which also
        keeps merged-in phrasing systems (multi-word translations) out of
        the pattern mining."""
        words = {}
        for m in norm_maps:
            for stroke, t in m.items():
                if "\\" not in t and _WORD_RE.match(t):
                    words.setdefault(t.lower(), set()).add(stroke)
        self.words = words
        self.phon = _load_cmudict()
        from collections import Counter, defaultdict
        rules = defaultdict(Counter)
        rules_cond = defaultdict(Counter)
        rules_phon = defaultdict(Counter)
        npairs = 0
        for d in words:
            for suf in _SUFFIXES:
                if not (d.endswith(suf) and len(d) > len(suf)):
                    continue
                root = next((r for r in _candidate_roots(d, suf)
                             if r in words and r != d), None)
                if not root:
                    continue
                npairs += 1
                fp = self.phon.get(root)
                for rs in words[root]:
                    last = rs.split("/")[-1]
                    for ds in words[d]:
                        rule = _extract_rule(rs, ds)
                        if rule:
                            rules[suf][rule] += 1
                            rules_cond[(suf, last)][rule] += 1
                            if fp:
                                rules_phon[(suf, fp)][rule] += 1
        self.rules = dict(rules)
        self.rules_cond = dict(rules_cond)
        self.rules_phon = dict(rules_phon)
        self.pair_count = npairs
        self.ready = True
        return self

    def suggest(self, text, limit=3):
        """Top suggestions for a translation. Returns [(stroke, evidence)]."""
        t = text.strip().lower()
        if not self.ready or not t or not _WORD_RE.match(t):
            return []
        best = {}  # stroke -> (score, evidence)
        for suf in _SUFFIXES:
            if not (t.endswith(suf) and len(t) > len(suf)):
                continue
            global_rules = self.rules.get(suf)
            if not global_rules:
                continue
            for root in _candidate_roots(t, suf):
                if root not in self.words or root == t:
                    continue
                fp = self.phon.get(root)
                pcond = self.rules_phon.get((suf, fp), {}) if fp else {}
                for rs in sorted(self.words[root],
                                 key=lambda s: (s.count("/"), len(s)))[:4]:
                    cond = self.rules_cond.get((suf, rs.split("/")[-1]), {})
                    for rule, g in global_rules.items():
                        score = (1_000_000 * cond.get(rule, 0)
                                 + 1000 * pcond.get(rule, 0) + g)
                        pred = _apply_rule(rs, rule)
                        if not pred:
                            continue
                        if pred not in best or best[pred][0] < score:
                            n = cond.get(rule, 0) or g
                            best[pred] = (score, f"{root} = {rs}  ·  "
                                          f"{_rule_desc(rule)} (seen {n:,}×)")
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]
        return [(stroke, ev) for stroke, (_, ev) in ranked]


def _parse_data_bundle(data):
    """Validate an imported data file. Accepts the export bundle
    ({"tags": ..., "planning": ...}) as well as a bare tags file (dict of
    stroke -> tag) or bare planning file (list of entries), so the sidecar
    files themselves can be imported directly. Returns (tags, planning)."""
    if isinstance(data, list):
        data = {"planning": data}
    elif isinstance(data, dict) and "tags" not in data and "planning" not in data:
        data = {"tags": data}
    if not isinstance(data, dict):
        raise ValueError("Expected a StenoSync data file")
    tags_in = data.get("tags", {})
    planning_in = data.get("planning", [])
    if not isinstance(tags_in, dict) or not isinstance(planning_in, list):
        raise ValueError("Expected a StenoSync data file")
    tags = {}
    for k, v in tags_in.items():
        if v not in ("ignore", "later"):
            raise ValueError(f"Unknown tag {v!r} for stroke {k!r}")
        tags[str(k)] = v
    planning = []
    for p in planning_in:
        if not isinstance(p, dict):
            raise ValueError("Planning entries must be objects")
        planning.append({"stroke": str(p.get("stroke", "")),
                         "translation": str(p.get("translation", ""))})
    return tags, planning


class _SuggestBuilder(QThread):
    done = pyqtSignal(object)

    def __init__(self, norm_maps, parent=None):
        super().__init__(parent)
        self._norm_maps = norm_maps

    def run(self):
        self.done.emit(StrokeSuggester().build(self._norm_maps))


class StenoSync(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("StenoSync")
        self.resize(1100, 700)
        self._settings = QSettings("StenoSync", "StenoSync")
        self._font_size = int(self._settings.value("font_size", APP_FONT_SIZE))
        self._apply_font_size()

        self.json_path = None
        self.rtf_path = None
        self.json_entries = {}
        self.rtf_entries = {}
        self.rtf_header = None
        self.json_norm = {}
        self.rtf_norm = {}
        self._sorted_keys = []
        self._search_lc = {}
        self.tags = {}  # stroke -> "ignore" | "later"
        self.planning = []  # list of {"stroke": ..., "translation": ...}
        self._suggester = None
        self._suggest_thread = None
        self._suggest_gen = 0  # invalidates in-flight builds on reload

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        # --- file row ---
        files = QHBoxLayout()
        self.json_label = QLabel("JSON: (none)")
        self.rtf_label = QLabel("RTF: (none)")
        open_btn = QPushButton("Open dictionary pair…")
        open_btn.clicked.connect(self.open_pair)
        reload_btn = QPushButton("Reload && re-diff")
        reload_btn.clicked.connect(self.reload)
        export_btn = QPushButton("Export data…")
        export_btn.setToolTip("Save tags (ignored / consider later) and the "
                              "planning list to a single file")
        export_btn.clicked.connect(self.export_data)
        import_btn = QPushButton("Import data…")
        import_btn.setToolTip("Load tags and planning from an exported file")
        import_btn.clicked.connect(self.import_data)
        files.addWidget(open_btn)
        files.addWidget(reload_btn)
        files.addWidget(export_btn)
        files.addWidget(import_btn)
        files.addStretch()
        files.addWidget(self.json_label)
        files.addWidget(QLabel("  |  "))
        files.addWidget(self.rtf_label)
        files.addWidget(QLabel("  "))
        self.font_size_label = QLabel(f"Font: {self._font_size}pt")
        files.addWidget(self.font_size_label)
        font_minus = QPushButton("A-")
        font_minus.setFixedWidth(36)
        font_minus.clicked.connect(lambda: self._change_font_size(-1))
        files.addWidget(font_minus)
        font_plus = QPushButton("A+")
        font_plus.setFixedWidth(36)
        font_plus.clicked.connect(lambda: self._change_font_size(1))
        files.addWidget(font_plus)
        self._theme = self._settings.value("theme", "Dark")
        self.theme_btn = QPushButton()
        self.theme_btn.clicked.connect(self._toggle_theme)
        self._update_theme_btn()
        files.addWidget(self.theme_btn)
        outer.addLayout(files)

        # --- add-entry form ---
        form = QGroupBox("Add / update entry")
        fl = QVBoxLayout(form)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Stroke:"))
        self.stroke_in = QLineEdit()
        self.stroke_in.setPlaceholderText("e.g. STKPW")
        row1.addWidget(self.stroke_in)
        self.stroke_hint = QLabel("")
        self.stroke_hint.setStyleSheet(f"color: {COLORS['hint']}; font-size: 15pt; font-weight: bold;")
        row1.addWidget(self.stroke_hint)
        self.stroke_in.textChanged.connect(self._update_stroke_hint)
        self.formatted_cb = QCheckBox("Formatted")
        self.formatted_cb.stateChanged.connect(self._toggle_formatted)
        row1.addWidget(self.formatted_cb)
        fl.addLayout(row1)

        row2 = QHBoxLayout()
        self.plain_label = QLabel("Translation:")
        row2.addWidget(self.plain_label)
        self.plain_in = QLineEdit()
        self.plain_in.setPlaceholderText("written to both files identically")
        row2.addWidget(self.plain_in)
        fl.addLayout(row2)

        self.fmt_row = QHBoxLayout()
        self.fmt_row.addWidget(QLabel("JSON form:"))
        self.json_in = QLineEdit()
        self.json_in.setPlaceholderText(r"e.g. {^}ing")
        self.fmt_row.addWidget(self.json_in)
        self.fmt_row.addWidget(QLabel("RTF form:"))
        self.rtf_in = QLineEdit()
        self.rtf_in.setPlaceholderText(r"e.g. \cxds ing")
        self.fmt_row.addWidget(self.rtf_in)
        fl.addLayout(self.fmt_row)
        self._toggle_formatted()

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Write to both files")
        add_btn.clicked.connect(self.add_entry)
        btn_row.addWidget(add_btn)
        sync_btn = QPushButton("Sync selected")
        sync_btn.clicked.connect(self.sync_selected)
        btn_row.addWidget(sync_btn)
        del_btn = QPushButton("Delete selected")
        del_btn.clicked.connect(self.delete_selected)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        ignore_btn = QPushButton("Mark ignored")
        ignore_btn.clicked.connect(lambda: self._tag_selected("ignore"))
        btn_row.addWidget(ignore_btn)
        later_btn = QPushButton("Consider later")
        later_btn.clicked.connect(lambda: self._tag_selected("later"))
        btn_row.addWidget(later_btn)
        untag_btn = QPushButton("Clear tag")
        untag_btn.clicked.connect(lambda: self._tag_selected(None))
        btn_row.addWidget(untag_btn)
        fl.addLayout(btn_row)
        outer.addWidget(form)

        # --- search ---
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.search_in = QLineEdit()
        self.search_in.setPlaceholderText("filter by stroke or translation")
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self.refresh_table)
        self.search_in.textChanged.connect(lambda: self._search_timer.start(250))
        search_row.addWidget(self.search_in)
        self.table_count_label = QLabel("")
        search_row.addWidget(self.table_count_label)
        outer.addLayout(search_row)

        # --- split: entries table | mismatch panel ---
        split = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Stroke", "JSON", "RTF"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setFont(self.font())
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.cellClicked.connect(self._load_from_table)
        split.addWidget(self.table)

        right_split = QSplitter(Qt.Orientation.Vertical)

        # -- sync panel (top right) --
        sync_panel = QWidget()
        pl = QVBoxLayout(sync_panel)
        sync_header = QHBoxLayout()
        sync_header.addWidget(QLabel("Out of sync"))
        sync_header.addStretch()
        self.show_ignored_cb = QCheckBox("Show ignored")
        self.show_ignored_cb.stateChanged.connect(self.refresh_sync)
        sync_header.addWidget(self.show_ignored_cb)
        self.show_later_cb = QCheckBox("Show later")
        self.show_later_cb.stateChanged.connect(self.refresh_sync)
        sync_header.addWidget(self.show_later_cb)
        pl.addLayout(sync_header)
        self.sync_table = QTableWidget(0, 3)
        self.sync_table.setHorizontalHeaderLabels(["Stroke", "Issue", "Tag"])
        self.sync_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.sync_table.verticalHeader().setDefaultSectionSize(32)
        self.sync_table.setFont(self.font())
        self.sync_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sync_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sync_table.cellClicked.connect(self._load_from_sync)
        pl.addWidget(self.sync_table)
        legend = QLabel(
            "red = conflict · blue = JSON only · amber = RTF only\n"
            "grey = ignored · purple = consider later")
        legend.setFont(QFont("", 11))
        pl.addWidget(legend)
        right_split.addWidget(sync_panel)

        # -- planning panel (bottom right) --
        plan_panel = QWidget()
        ppl = QVBoxLayout(plan_panel)
        plan_header = QHBoxLayout()
        plan_header.addWidget(QLabel("Planning"))
        plan_header.addStretch()
        self.suggest_cb = QCheckBox("Suggest strokes")
        self.suggest_cb.setToolTip(
            "Suggest strokes for the translation you type, based on "
            "suffix patterns mined from your own dictionary")
        self.suggest_cb.setChecked(
            self._settings.value("suggest_strokes", "false") == "true")
        self.suggest_cb.stateChanged.connect(self._toggle_suggest)
        plan_header.addWidget(self.suggest_cb)
        ppl.addLayout(plan_header)

        plan_input = QHBoxLayout()
        self.plan_stroke_in = QLineEdit()
        self.plan_stroke_in.setPlaceholderText("Stroke (optional)")
        plan_input.addWidget(self.plan_stroke_in)
        self.plan_hint = QLabel("")
        self.plan_hint.setStyleSheet(f"color: {COLORS['hint']}; font-size: 15pt; font-weight: bold;")
        plan_input.addWidget(self.plan_hint)
        self.plan_stroke_in.textChanged.connect(self._update_plan_hint)
        self.plan_trans_in = QLineEdit()
        self.plan_trans_in.setPlaceholderText("Translation (optional)")
        plan_input.addWidget(self.plan_trans_in)
        plan_add_btn = QPushButton("Add to plan")
        plan_add_btn.clicked.connect(self.add_to_plan)
        plan_input.addWidget(plan_add_btn)
        ppl.addLayout(plan_input)

        suggest_row = QHBoxLayout()
        self.suggest_label = QLabel("")
        suggest_row.addWidget(self.suggest_label)
        self.suggest_btns = []
        for _ in range(3):
            b = QPushButton("")
            b.setVisible(False)
            b.clicked.connect(
                lambda checked=False, btn=b:
                self.plan_stroke_in.setText(btn.text()))
            self.suggest_btns.append(b)
            suggest_row.addWidget(b)
        suggest_row.addStretch()
        ppl.addLayout(suggest_row)
        self.plan_trans_in.textChanged.connect(self._update_suggestions)

        self.plan_table = QTableWidget(0, 3)
        self.plan_table.setHorizontalHeaderLabels(["Stroke", "Translation", "Status"])
        self.plan_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.plan_table.verticalHeader().setDefaultSectionSize(32)
        self.plan_table.setFont(self.font())
        self.plan_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.plan_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.plan_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.plan_table.cellClicked.connect(self._load_from_plan)
        self.plan_table.cellChanged.connect(self._plan_cell_edited)
        ppl.addWidget(self.plan_table)

        plan_btns = QHBoxLayout()
        plan_commit_btn = QPushButton("Add to dictionary")
        plan_commit_btn.clicked.connect(self.commit_planned)
        plan_btns.addWidget(plan_commit_btn)
        plan_del_btn = QPushButton("Remove from plan")
        plan_del_btn.clicked.connect(self.remove_from_plan)
        plan_btns.addWidget(plan_del_btn)
        plan_btns.addStretch()
        ppl.addLayout(plan_btns)

        right_split.addWidget(plan_panel)
        right_split.setSizes([300, 300])

        split.addWidget(right_split)
        split.setSizes([620, 380])
        outer.addWidget(split, 1)

        self.status = QLabel("Open a dictionary pair to begin.")
        outer.addWidget(self.status)

        # --- restore last session ---
        last_json = self._settings.value("last_json_path", "")
        last_rtf = self._settings.value("last_rtf_path", "")
        if last_json and last_rtf and os.path.isfile(last_json) and os.path.isfile(last_rtf):
            self.json_path = last_json
            self.rtf_path = last_rtf
            self.reload()

    def _big_msg(self, icon, title, text, buttons=None):
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStyleSheet("QLabel { font-size: 14pt; } QPushButton { font-size: 13pt; padding: 6px 18px; }")
        if buttons:
            box.setStandardButtons(buttons)
        else:
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
        return box.exec()

    # ----- font -----
    def _apply_font_size(self):
        font = QFont()
        font.setPointSize(self._font_size)
        self.setFont(font)

    def _change_font_size(self, delta):
        new_size = max(8, min(30, self._font_size + delta))
        if new_size == self._font_size:
            return
        self._font_size = new_size
        self._settings.setValue("font_size", new_size)
        self._apply_font_size()
        self.table.setFont(self.font())
        self.sync_table.setFont(self.font())
        self.plan_table.setFont(self.font())
        row_h = max(28, new_size * 2 + 6)
        self.table.verticalHeader().setDefaultSectionSize(row_h)
        self.sync_table.verticalHeader().setDefaultSectionSize(row_h)
        self.plan_table.verticalHeader().setDefaultSectionSize(row_h)
        self.font_size_label.setText(f"Font: {new_size}pt")

    # ----- theme -----
    def _update_theme_btn(self):
        nxt = "Light" if self._theme == "Dark" else "Dark"
        self.theme_btn.setText(("☀ " if nxt == "Light" else "🌙 ") + nxt)
        self.theme_btn.setToolTip(f"Switch to {nxt.lower()} mode")

    def _toggle_theme(self):
        self._theme = "Light" if self._theme == "Dark" else "Dark"
        self._settings.setValue("theme", self._theme)
        apply_theme(QApplication.instance(), self._theme)
        self._update_theme_btn()
        # repaint everything that carries explicit colors
        self.refresh_table()
        self.refresh_sync()
        self.refresh_planning()
        self._update_stroke_hint()
        self._update_plan_hint()

    # ----- form behavior -----
    def _toggle_formatted(self):
        formatted = self.formatted_cb.isChecked()
        self.plain_label.setVisible(not formatted)
        self.plain_in.setVisible(not formatted)
        for i in range(self.fmt_row.count()):
            w = self.fmt_row.itemAt(i).widget()
            if w:
                w.setVisible(formatted)

    def _update_stroke_hint(self):
        stroke = self.stroke_in.text().strip()
        if not stroke:
            self.stroke_hint.setText("")
            return
        jv = self.json_norm.get(stroke)
        rv = self.rtf_norm.get(stroke)
        if jv is not None and rv is not None:
            if jv == rv:
                self.stroke_hint.setText(f"exists: \"{jv}\"")
                self.stroke_hint.setStyleSheet(f"color: {COLORS['hint']};")
            else:
                self.stroke_hint.setText(f"CONFLICT — JSON: \"{jv}\" / RTF: \"{rv}\"")
                self.stroke_hint.setStyleSheet(f"color: {COLORS['hint_err']}; font-size: 15pt; font-weight: bold;")
        elif jv is not None:
            self.stroke_hint.setText(f"in JSON only: \"{jv}\"")
            self.stroke_hint.setStyleSheet(f"color: {COLORS['hint']};")
        elif rv is not None:
            self.stroke_hint.setText(f"in RTF only: \"{rv}\"")
            self.stroke_hint.setStyleSheet(f"color: {COLORS['hint']};")
        else:
            self.stroke_hint.setText("")

    def _rebuild_norm(self):
        """Precompute normalized translations, the sorted key list, and a
        lowercase search blob once — doing any of this per refresh is too
        slow at 600k entries."""
        self.json_norm = {k: normalized(v) for k, v in self.json_entries.items()}
        self.rtf_norm = {k: normalized(v) for k, v in self.rtf_entries.items()}
        self._sorted_keys = sorted(set(self.json_norm) | set(self.rtf_norm))
        self._search_lc = {
            k: f"{k}\n{self.json_norm.get(k, '')}\n{self.rtf_norm.get(k, '')}".lower()
            for k in self._sorted_keys}

    def _entry_changed(self, stroke):
        """Update the derived indexes for one added/updated stroke."""
        if stroke not in self._search_lc:
            bisect.insort(self._sorted_keys, stroke)
        jv = self.json_entries.get(stroke)
        rv = self.rtf_entries.get(stroke)
        self.json_norm[stroke] = normalized(jv) if jv is not None else ""
        self.rtf_norm[stroke] = normalized(rv) if rv is not None else ""
        if jv is None:
            self.json_norm.pop(stroke, None)
        if rv is None:
            self.rtf_norm.pop(stroke, None)
        self._search_lc[stroke] = (f"{stroke}\n{self.json_norm.get(stroke, '')}"
                                   f"\n{self.rtf_norm.get(stroke, '')}").lower()

    def _entry_removed(self, stroke):
        self.json_norm.pop(stroke, None)
        self.rtf_norm.pop(stroke, None)
        if stroke in self._search_lc:
            del self._search_lc[stroke]
            i = bisect.bisect_left(self._sorted_keys, stroke)
            if i < len(self._sorted_keys) and self._sorted_keys[i] == stroke:
                self._sorted_keys.pop(i)

    # ----- file ops -----
    def open_pair(self):
        jp, _ = QFileDialog.getOpenFileName(
            self, "Open Plover JSON dictionary", "", "JSON (*.json);;All (*)")
        if not jp:
            return
        rp, _ = QFileDialog.getOpenFileName(
            self, "Open CRE-RTF dictionary", "", "RTF (*.rtf);;All (*)")
        if not rp:
            return
        self.json_path, self.rtf_path = jp, rp
        self._settings.setValue("last_json_path", jp)
        self._settings.setValue("last_rtf_path", rp)
        self.reload()

    def reload(self):
        if not (self.json_path and self.rtf_path):
            return
        try:
            with open(self.json_path, encoding="utf-8") as f:
                self.json_entries = parse_plover_json(f.read() or "{}")
            with open(self.rtf_path, encoding="utf-8") as f:
                self.rtf_entries, self.rtf_header = parse_cre_rtf(f.read())
        except Exception as e:
            QMessageBox.critical(self, "Load error", str(e))
            return
        self._rebuild_norm()
        self.tags = load_tags(self.json_path)
        self.planning = load_planning(self.json_path)
        self.json_label.setText("JSON: " + os.path.basename(self.json_path))
        self.rtf_label.setText("RTF: " + os.path.basename(self.rtf_path))
        self.refresh_table()
        self.refresh_sync()
        self.refresh_planning()
        self._suggester = None  # entries changed — patterns must be re-mined
        self._suggest_gen += 1  # discard any in-flight build of the old data
        if self.suggest_cb.isChecked():
            self._start_suggest_build()
        else:
            self._update_suggestions()

    def add_entry(self):
        if not (self.json_path and self.rtf_path):
            QMessageBox.warning(self, "No files", "Open a dictionary pair first.")
            return
        stroke = self.stroke_in.text().strip()
        if not stroke:
            QMessageBox.warning(self, "No stroke", "Enter a stroke.")
            return
        if self.formatted_cb.isChecked():
            jv, rv = self.json_in.text(), self.rtf_in.text()
            # if only one side filled, use it for both
            if jv and not rv:
                rv = jv
            elif rv and not jv:
                jv = rv
        else:
            jv = rv = self.plain_in.text()
        if not jv and not rv:
            QMessageBox.warning(self, "Empty", "Enter a translation.")
            return

        # check for existing conflict
        existing_j = self.json_entries.get(stroke)
        existing_r = self.rtf_entries.get(stroke)
        if existing_j is not None and existing_r is not None and \
                normalized(existing_j) != normalized(existing_r):
            self._big_msg(
                QMessageBox.Icon.Warning, "Conflict",
                f"'{stroke}' has a conflict between JSON and RTF.\n\n"
                f"JSON: {existing_j}\nRTF: {existing_r}\n\n"
                "Resolve the conflict first (use Sync or Delete), "
                "or pick a different stroke.")
            return

        # warn if overwriting an existing entry
        if existing_j is not None or existing_r is not None:
            existing_val = normalized(existing_j if existing_j is not None else existing_r)
            reply = self._big_msg(
                QMessageBox.Icon.Question, "Overwrite?",
                f"'{stroke}' already exists with translation \"{existing_val}\".\n\n"
                "Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return

        new_json = dict(self.json_entries)
        new_rtf = dict(self.rtf_entries)
        new_json[stroke] = jv
        new_rtf[stroke] = rv
        try:
            write_both(self.json_path, new_json, self.rtf_path, new_rtf, self.rtf_header)
        except Exception as e:
            QMessageBox.critical(self, "Write failed",
                                 f"Neither file changed.\n\n{e}")
            return
        self.json_entries, self.rtf_entries = new_json, new_rtf
        self._entry_changed(stroke)
        self.stroke_in.clear()
        self.plain_in.clear()
        self.json_in.clear()
        self.rtf_in.clear()
        self.status.setText(f"Wrote '{stroke}' to both files.")
        self.refresh_table()
        self.refresh_sync()

    def _selected_strokes(self):
        """Get strokes from selected rows in both tables, plus the stroke input."""
        strokes = []
        seen = set()
        # from main table
        for idx in self.table.selectionModel().selectedRows():
            s = self.table.item(idx.row(), 0).text()
            if s not in seen:
                strokes.append(s)
                seen.add(s)
        # from sync table
        for idx in self.sync_table.selectionModel().selectedRows():
            s = self.sync_table.item(idx.row(), 0).text()
            if s not in seen:
                strokes.append(s)
                seen.add(s)
        # fallback to text input
        if not strokes:
            s = self.stroke_in.text().strip()
            if s:
                strokes.append(s)
        return strokes

    def sync_selected(self):
        if not (self.json_path and self.rtf_path):
            QMessageBox.warning(self, "No files", "Open a dictionary pair first.")
            return
        strokes = self._selected_strokes()
        if not strokes:
            QMessageBox.warning(self, "Nothing selected", "Select entries to sync.")
            return
        new_json = dict(self.json_entries)
        new_rtf = dict(self.rtf_entries)
        synced = []
        for stroke in strokes:
            jv = self.json_entries.get(stroke)
            rv = self.rtf_entries.get(stroke)
            if jv is not None and rv is not None and normalized(jv) == normalized(rv):
                continue  # already in sync
            # pick whichever side has it; for conflicts, prefer JSON
            if jv is not None:
                new_json[stroke] = jv
                new_rtf[stroke] = jv
            elif rv is not None:
                # RTF -> JSON: metadata groups stay in the RTF, not the JSON
                new_json[stroke] = normalized(rv)
                new_rtf[stroke] = rv
            else:
                continue
            synced.append(stroke)
        if not synced:
            self.status.setText("Selected entries are already in sync.")
            return
        try:
            write_both(self.json_path, new_json, self.rtf_path, new_rtf, self.rtf_header)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", str(e))
            return
        self.json_entries, self.rtf_entries = new_json, new_rtf
        for s in synced:
            self._entry_changed(s)
            self.tags.pop(s, None)
        save_tags(self.json_path, self.tags)
        self.status.setText(f"Synced {len(synced)} entry(s) to both files.")
        self.refresh_table()
        self.refresh_sync()

    def delete_selected(self):
        if not (self.json_path and self.rtf_path):
            QMessageBox.warning(self, "No files", "Open a dictionary pair first.")
            return
        strokes = self._selected_strokes()
        if not strokes:
            QMessageBox.warning(self, "Nothing selected",
                                "Select entries to delete.")
            return
        # filter to strokes that actually exist
        strokes = [s for s in strokes
                   if s in self.json_entries or s in self.rtf_entries]
        if not strokes:
            QMessageBox.warning(self, "Not found",
                                "None of the selected strokes exist.")
            return
        preview = "\n".join(strokes[:20])
        if len(strokes) > 20:
            preview += f"\n… and {len(strokes) - 20} more"
        reply = self._big_msg(
            QMessageBox.Icon.Question, "Delete entries",
            f"Delete {len(strokes)} entry(s) from both files?\n\n{preview}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        new_json = dict(self.json_entries)
        new_rtf = dict(self.rtf_entries)
        for s in strokes:
            new_json.pop(s, None)
            new_rtf.pop(s, None)
        try:
            write_both(self.json_path, new_json, self.rtf_path, new_rtf, self.rtf_header)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", str(e))
            return
        self.json_entries, self.rtf_entries = new_json, new_rtf
        for s in strokes:
            self._entry_removed(s)
            self.tags.pop(s, None)
        save_tags(self.json_path, self.tags)
        self.stroke_in.clear()
        self.plain_in.clear()
        self.json_in.clear()
        self.rtf_in.clear()
        self.status.setText(f"Deleted {len(strokes)} entry(s) from both files.")
        self.refresh_table()
        self.refresh_sync()

    def _tag_selected(self, tag):
        if not self.json_path:
            return
        strokes = self._selected_strokes()
        if not strokes:
            QMessageBox.warning(self, "Nothing selected",
                                "Select entries to tag.")
            return
        for s in strokes:
            if tag is None:
                self.tags.pop(s, None)
            else:
                self.tags[s] = tag
        save_tags(self.json_path, self.tags)
        label = "cleared" if tag is None else tag
        self.status.setText(f"Tagged {len(strokes)} entry(s) as {label}.")
        self.refresh_sync()

    # ----- import / export of sidecar data -----
    def export_data(self):
        if not self.json_path:
            QMessageBox.warning(self, "No files", "Open a dictionary pair first.")
            return
        if not self.tags and not self.planning:
            QMessageBox.information(self, "Nothing to export",
                                    "There are no tags or planning entries yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export StenoSync data", "stenosync_data.json",
            "JSON (*.json);;All (*)")
        if not path:
            return
        bundle = {"app": "StenoSync", "version": 1,
                  "tags": self.tags, "planning": self.planning}
        try:
            _atomic_write(path, json.dumps(bundle, ensure_ascii=False, indent=1))
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        self.status.setText(
            f"Exported {len(self.tags):,} tag(s) and "
            f"{len(self.planning):,} planning entry(s) to "
            f"{os.path.basename(path)}.")

    def import_data(self):
        if not self.json_path:
            QMessageBox.warning(self, "No files", "Open a dictionary pair first.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import StenoSync data", "", "JSON (*.json);;All (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                bundle = json.loads(f.read() or "{}")
            tags, planning = _parse_data_bundle(bundle)
        except Exception as e:
            QMessageBox.critical(self, "Import failed",
                                 f"Could not read that file.\n\n{e}")
            return
        if not tags and not planning:
            QMessageBox.information(self, "Nothing to import",
                                    "That file contains no tags or planning entries.")
            return
        reply = self._big_msg(
            QMessageBox.Icon.Question, "Import data",
            f"Import {len(tags):,} tag(s) and {len(planning):,} planning "
            "entry(s)?\n\nYes — merge into current data "
            "(imported tags win on overlap)\n"
            "No — replace current data entirely",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Cancel:
            return
        if reply == QMessageBox.StandardButton.Yes:
            self.tags.update(tags)
            existing = {(p.get("stroke", ""), p.get("translation", ""))
                        for p in self.planning}
            self.planning.extend(
                p for p in planning
                if (p["stroke"], p["translation"]) not in existing)
        else:
            self.tags = tags
            self.planning = planning
        save_tags(self.json_path, self.tags)
        save_planning(self.json_path, self.planning)
        self.refresh_sync()
        self.refresh_planning()
        self.status.setText(
            f"Imported data — now {len(self.tags):,} tag(s) and "
            f"{len(self.planning):,} planning entry(s).")

    # ----- stroke suggestions -----
    def _toggle_suggest(self):
        on = self.suggest_cb.isChecked()
        self._settings.setValue("suggest_strokes", "true" if on else "false")
        if on and self._suggester is None:
            self._start_suggest_build()
        self._update_suggestions()

    def _start_suggest_build(self):
        if not (self.json_path and self.rtf_path):
            return
        self._suggest_gen += 1
        gen = self._suggest_gen
        self.suggest_label.setText("mining patterns…")
        # a build already in flight keeps running; its result is discarded
        # in _suggest_ready because its generation is stale
        thread = _SuggestBuilder(
            [dict(self.json_norm), dict(self.rtf_norm)], self)
        thread.done.connect(lambda s, g=gen: self._suggest_ready(s, g))
        self._suggest_thread = thread
        thread.start()

    def _suggest_ready(self, suggester, gen):
        if gen != self._suggest_gen:
            return  # entries changed while mining — a newer build is running
        self._suggester = suggester
        self.status.setText(
            f"Stroke suggestions ready — learned from "
            f"{suggester.pair_count:,} root/derived pairs.")
        self._update_suggestions()

    def _update_suggestions(self):
        text = self.plan_trans_in.text().strip()
        sugs = []
        if self.suggest_cb.isChecked():
            if self._suggester is None:
                self.suggest_label.setText(
                    "mining patterns…" if self._suggest_thread
                    and self._suggest_thread.isRunning() else "")
            elif text:
                sugs = self._suggester.suggest(text)
                self.suggest_label.setText(
                    "Suggest:" if sugs else "no suggestion")
            else:
                self.suggest_label.setText("")
        else:
            self.suggest_label.setText("")
        for btn, sug in zip(self.suggest_btns, sugs + [None] * 3):
            if sug:
                btn.setText(sug[0])
                btn.setToolTip(sug[1])
                btn.setVisible(True)
            else:
                btn.setVisible(False)

    # ----- planning -----
    def _update_plan_hint(self):
        stroke = self.plan_stroke_in.text().strip()
        if not stroke:
            self.plan_hint.setText("")
            return
        jv = self.json_norm.get(stroke)
        rv = self.rtf_norm.get(stroke)
        if jv is not None and rv is not None:
            if jv == rv:
                self.plan_hint.setText(f"exists: \"{jv}\"")
                self.plan_hint.setStyleSheet(f"color: {COLORS['hint']}; font-size: 15pt; font-weight: bold;")
            else:
                self.plan_hint.setText(f"CONFLICT — JSON: \"{jv}\" / RTF: \"{rv}\"")
                self.plan_hint.setStyleSheet(f"color: {COLORS['hint_err']}; font-size: 15pt; font-weight: bold;")
        elif jv is not None:
            self.plan_hint.setText(f"in JSON only: \"{jv}\"")
            self.plan_hint.setStyleSheet(f"color: {COLORS['hint']}; font-size: 15pt; font-weight: bold;")
        elif rv is not None:
            self.plan_hint.setText(f"in RTF only: \"{rv}\"")
            self.plan_hint.setStyleSheet(f"color: {COLORS['hint']}; font-size: 15pt; font-weight: bold;")
        else:
            self.plan_hint.setText("")

    def add_to_plan(self):
        stroke = self.plan_stroke_in.text().strip()
        trans = self.plan_trans_in.text().strip()
        if not stroke and not trans:
            QMessageBox.warning(self, "Empty", "Enter a stroke, translation, or both.")
            return
        self.planning.append({"stroke": stroke, "translation": trans})
        if self.json_path:
            save_planning(self.json_path, self.planning)
        self.plan_stroke_in.clear()
        self.plan_trans_in.clear()
        self.refresh_planning()
        self.status.setText(f"Added to plan: {stroke or '?'} → {trans or '?'}")

    def remove_from_plan(self):
        rows = sorted(set(idx.row() for idx in self.plan_table.selectionModel().selectedRows()), reverse=True)
        if not rows:
            QMessageBox.warning(self, "Nothing selected", "Select plan entries to remove.")
            return
        for r in rows:
            if 0 <= r < len(self.planning):
                self.planning.pop(r)
        if self.json_path:
            save_planning(self.json_path, self.planning)
        self.refresh_planning()
        self.status.setText(f"Removed {len(rows)} entry(s) from plan.")

    def commit_planned(self):
        if not (self.json_path and self.rtf_path):
            QMessageBox.warning(self, "No files", "Open a dictionary pair first.")
            return
        rows = sorted(set(idx.row() for idx in self.plan_table.selectionModel().selectedRows()))
        if not rows:
            QMessageBox.warning(self, "Nothing selected", "Select plan entries to add to dictionary.")
            return
        ready = []
        incomplete = []
        for r in rows:
            entry = self.planning[r]
            s, t = entry.get("stroke", ""), entry.get("translation", "")
            if s and t:
                ready.append((r, s, t))
            else:
                incomplete.append(r)
        if incomplete:
            QMessageBox.warning(
                self, "Incomplete entries",
                f"{len(incomplete)} selected entry(s) need both a stroke and translation.\n"
                "Fill them in before adding to dictionary.")
            return
        if not ready:
            return
        # same stroke twice in the selection with different translations —
        # committing would silently let the later row win
        by_stroke = {}
        dups = set()
        for _, s, t in ready:
            if s in by_stroke and by_stroke[s] != t:
                dups.add(s)
            by_stroke[s] = t
        if dups:
            self._big_msg(
                QMessageBox.Icon.Warning, "Duplicate strokes in plan",
                "These strokes appear more than once in the selection with "
                "different translations:\n\n" + "\n".join(sorted(dups)[:15]) +
                "\n\nRemove or edit the extras before adding to dictionary.")
            return
        new_json = dict(self.json_entries)
        new_rtf = dict(self.rtf_entries)
        conflicts = []
        for _, s, t in ready:
            ej = self.json_entries.get(s)
            er = self.rtf_entries.get(s)
            if ej is not None or er is not None:
                conflicts.append(s)
            else:
                new_json[s] = t
                new_rtf[s] = t
        if conflicts:
            reply = self._big_msg(
                QMessageBox.Icon.Question, "Some strokes exist",
                f"{len(conflicts)} stroke(s) already in dictionary:\n"
                + "\n".join(conflicts[:15]) +
                "\n\nSkip those and add the rest?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        added = [s for _, s, t in ready if s not in conflicts]
        if not added:
            self.status.setText("No new entries to add.")
            return
        try:
            write_both(self.json_path, new_json, self.rtf_path, new_rtf, self.rtf_header)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", str(e))
            return
        self.json_entries, self.rtf_entries = new_json, new_rtf
        for _, s, t in ready:
            if s not in conflicts:
                self._entry_changed(s)
        # remove committed entries from plan (reverse order to keep indices valid)
        committed_rows = sorted([r for r, s, t in ready if s not in conflicts], reverse=True)
        for r in committed_rows:
            self.planning.pop(r)
        save_planning(self.json_path, self.planning)
        self.status.setText(f"Added {len(added)} entry(s) to dictionary from plan.")
        self.refresh_table()
        self.refresh_sync()
        self.refresh_planning()

    def refresh_planning(self):
        self.plan_table.blockSignals(True)
        self.plan_table.setRowCount(len(self.planning))
        # strokes that appear in more than one plan row with differing translations
        seen = {}
        dup_strokes = set()
        for entry in self.planning:
            s, t = entry.get("stroke", ""), entry.get("translation", "")
            if s:
                if s in seen and seen[s] != t:
                    dup_strokes.add(s)
                seen[s] = t
        for i, entry in enumerate(self.planning):
            s = entry.get("stroke", "")
            t = entry.get("translation", "")
            if not s and not t:
                status, bg = "empty", COLORS["incomplete"]
            elif not s or not t:
                status, bg = "incomplete", COLORS["incomplete"]
            elif s in dup_strokes:
                status, bg = "dup in plan", COLORS["conflict"]
            elif s in self.json_entries or s in self.rtf_entries:
                ej = self.json_entries.get(s)
                er = self.rtf_entries.get(s)
                if ej is not None and er is not None and \
                        normalized(ej) != normalized(er):
                    status, bg = "conflict", COLORS["conflict"]
                else:
                    status, bg = "exists", COLORS["missing_j"]
            else:
                status, bg = "ready", COLORS["planning"]
            is_problem = status in ("conflict", "exists", "dup in plan")
            for c, val in enumerate((s, t, status)):
                item = QTableWidgetItem(val)
                item.setBackground(bg)
                if is_problem:
                    item.setForeground(COLORS["problem"])
                else:
                    item.setForeground(COLORS["text"])
                # status column is read-only
                if c == 2:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.plan_table.setItem(i, c, item)
        self.plan_table.blockSignals(False)

    def _plan_cell_edited(self, row, col):
        if row < 0 or row >= len(self.planning) or col > 1:
            return
        text = self.plan_table.item(row, col).text()
        if col == 0:
            self.planning[row]["stroke"] = text
        else:
            self.planning[row]["translation"] = text
        if self.json_path:
            save_planning(self.json_path, self.planning)
        self.refresh_planning()

    def _load_from_plan(self, row, _col):
        if len(self.plan_table.selectionModel().selectedRows()) <= 1:
            entry = self.planning[row]
            self.plan_stroke_in.setText(entry.get("stroke", ""))
            self.plan_trans_in.setText(entry.get("translation", ""))

    # ----- views -----
    def refresh_table(self):
        q = self.search_in.text().strip().lower()
        rows = []
        matches = 0
        blob = self._search_lc
        for k in self._sorted_keys:
            if q and q not in blob[k]:
                continue
            matches += 1
            if len(rows) <= MAX_TABLE_ROWS:
                rows.append((k, self.json_norm.get(k, ""), self.rtf_norm.get(k, "")))
        rows = rows[:MAX_TABLE_ROWS]
        if matches > len(rows):
            self.table_count_label.setText(
                f"showing {len(rows):,} of {matches:,} — search to narrow")
        else:
            self.table_count_label.setText(f"{matches:,} entries")
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for i, (k, jv, rv) in enumerate(rows):
            for c, val in enumerate((k, jv, rv)):
                item = QTableWidgetItem(val)
                if jv != rv:
                    item.setBackground(COLORS["conflict"])
                    item.setForeground(COLORS["text"])
                self.table.setItem(i, c, item)
        self.table.setUpdatesEnabled(True)

    def refresh_sync(self):
        mij, mir, conf = diff(self.json_norm, self.rtf_norm)
        show_ignored = self.show_ignored_cb.isChecked()
        show_later = self.show_later_cb.isChecked()
        all_rows = (
            [(s, "conflict: translations differ", COLORS["conflict"]) for s in conf]
            + [(s, "in JSON only — missing from RTF", COLORS["missing_r"]) for s in mir]
            + [(s, "in RTF only — missing from JSON", COLORS["missing_j"]) for s in mij]
        )
        rows = []
        hidden = 0
        for s, msg, bg in all_rows:
            tag = self.tags.get(s, "")
            if (tag == "ignore" and not show_ignored) or \
               (tag == "later" and not show_later):
                hidden += 1
                continue
            if tag == "ignore":
                bg = COLORS["ignored"]
            elif tag == "later":
                bg = COLORS["later"]
            rows.append((s, msg, tag, bg))
        shown = rows[:MAX_TABLE_ROWS]
        self.sync_table.setUpdatesEnabled(False)
        self.sync_table.setRowCount(len(shown))
        for i, (s, msg, tag, bg) in enumerate(shown):
            for c, val in enumerate((s, msg, tag)):
                item = QTableWidgetItem(val)
                item.setBackground(bg)
                item.setForeground(COLORS["text"])
                self.sync_table.setItem(i, c, item)
        self.sync_table.setUpdatesEnabled(True)
        total = len(all_rows)
        active = total - hidden
        if total == 0:
            self.status.setText("In sync.")
        elif hidden:
            self.status.setText(f"{active:,} out of sync ({hidden:,} hidden).")
        else:
            self.status.setText(f"{active:,} entries out of sync.")
        if len(rows) > MAX_TABLE_ROWS:
            self.status.setText(self.status.text()
                                + f" Showing first {MAX_TABLE_ROWS:,}.")

    def _load_from_table(self, row, _col):
        if len(self.table.selectionModel().selectedRows()) <= 1:
            self._load_stroke(self.table.item(row, 0).text())

    def _load_from_sync(self, row, _col):
        if len(self.sync_table.selectionModel().selectedRows()) <= 1:
            self._load_stroke(self.sync_table.item(row, 0).text())

    def _load_stroke(self, stroke):
        self.stroke_in.setText(stroke)
        jv = self.json_entries.get(stroke, "")
        rv = self.rtf_entries.get(stroke, "")
        if normalized(jv) == normalized(rv):
            self.formatted_cb.setChecked(False)
            self.plain_in.setText(normalized(jv))
        else:
            self.formatted_cb.setChecked(True)
            self.json_in.setText(jv)
            self.rtf_in.setText(rv)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    _theme = QSettings("StenoSync", "StenoSync").value("theme", "Dark")
    apply_theme(app, _theme)
    w = StenoSync()
    w.show()
    sys.exit(app.exec())
