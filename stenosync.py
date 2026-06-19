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
import json
import os
import re
import sys
import tempfile

from PyQt6.QtCore import Qt, QSettings
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


# CRE entries look like: {\*\cxs STKPW}word
_ENTRY_RE = re.compile(r"\{\\\*\\cxs\s+([^}]*)\}([^{}\\]*)")
CRE_HEADER = (r"{\rtf1\ansi\ansicpg1252\deff0\deflang1033"
              r"{\fonttbl{\f0\fnil Courier New;}}"
              r"{\*\cxsystem StenoSync}\cxdict")


def parse_cre_rtf(text):
    entries = {}
    for m in _ENTRY_RE.finditer(text):
        steno = m.group(1).strip()
        translation = m.group(2).strip()
        if steno:
            entries[steno] = translation
    return entries


def serialize_cre_rtf(entries):
    lines = [CRE_HEADER]
    for steno, translation in sorted(entries.items()):
        lines.append(r"{\*\cxs %s}%s" % (steno, translation))
    lines.append("}")
    return "\r\n".join(lines)


def diff(json_entries, rtf_entries):
    jk, rk = set(json_entries), set(rtf_entries)
    missing_in_json = sorted(rk - jk)   # present in RTF only
    missing_in_rtf = sorted(jk - rk)    # present in JSON only
    conflicts = sorted(s for s in (jk & rk)
                       if json_entries[s] != rtf_entries[s])
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


def write_both(json_path, json_entries, rtf_path, rtf_entries):
    """Write both files. Stage both temps first so a serialize error in one
    doesn't leave the other half-updated. The two os.replace calls are
    back-to-back; true cross-file atomicity isn't possible on a filesystem,
    but failure between them is rare and leaves both files individually valid."""
    json_text = serialize_plover_json(json_entries)
    rtf_text = serialize_cre_rtf(rtf_entries)
    _atomic_write(json_path, json_text)
    _atomic_write(rtf_path, rtf_text)


# ───────────────────────── GUI ─────────────────────────

CONFLICT_BG = QColor(180, 60, 60)
MISSING_J_BG = QColor(160, 130, 40)
MISSING_R_BG = QColor(50, 100, 150)
IGNORED_BG = QColor(80, 80, 80)
LATER_BG = QColor(100, 70, 120)
TEXT_COLOR = QColor(255, 255, 255)
APP_FONT_SIZE = 13

PLANNING_BG = QColor(40, 100, 70)
INCOMPLETE_BG = QColor(90, 70, 50)
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
        self.tags = {}  # stroke -> "ignore" | "later"
        self.planning = []  # list of {"stroke": ..., "translation": ...}

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
        files.addWidget(open_btn)
        files.addWidget(reload_btn)
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
        self.stroke_hint.setStyleSheet("color: #66ccff; font-size: 15pt; font-weight: bold;")
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
        self.search_in.textChanged.connect(self.refresh_table)
        search_row.addWidget(self.search_in)
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
        ppl.addLayout(plan_header)

        plan_input = QHBoxLayout()
        self.plan_stroke_in = QLineEdit()
        self.plan_stroke_in.setPlaceholderText("Stroke (optional)")
        plan_input.addWidget(self.plan_stroke_in)
        self.plan_hint = QLabel("")
        self.plan_hint.setStyleSheet("color: #66ccff; font-size: 15pt; font-weight: bold;")
        plan_input.addWidget(self.plan_hint)
        self.plan_stroke_in.textChanged.connect(self._update_plan_hint)
        self.plan_trans_in = QLineEdit()
        self.plan_trans_in.setPlaceholderText("Translation (optional)")
        plan_input.addWidget(self.plan_trans_in)
        plan_add_btn = QPushButton("Add to plan")
        plan_add_btn.clicked.connect(self.add_to_plan)
        plan_input.addWidget(plan_add_btn)
        ppl.addLayout(plan_input)

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
        jv = self.json_entries.get(stroke)
        rv = self.rtf_entries.get(stroke)
        if jv is not None and rv is not None:
            if jv == rv:
                self.stroke_hint.setText(f"exists: \"{jv}\"")
                self.stroke_hint.setStyleSheet("color: #66ccff;")
            else:
                self.stroke_hint.setText(f"CONFLICT — JSON: \"{jv}\" / RTF: \"{rv}\"")
                self.stroke_hint.setStyleSheet("color: #ee6666; font-size: 15pt; font-weight: bold;")
        elif jv is not None:
            self.stroke_hint.setText(f"in JSON only: \"{jv}\"")
            self.stroke_hint.setStyleSheet("color: #66ccff;")
        elif rv is not None:
            self.stroke_hint.setText(f"in RTF only: \"{rv}\"")
            self.stroke_hint.setStyleSheet("color: #66ccff;")
        else:
            self.stroke_hint.setText("")

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
                self.rtf_entries = parse_cre_rtf(f.read())
        except Exception as e:
            QMessageBox.critical(self, "Load error", str(e))
            return
        self.tags = load_tags(self.json_path)
        self.planning = load_planning(self.json_path)
        self.json_label.setText("JSON: " + os.path.basename(self.json_path))
        self.rtf_label.setText("RTF: " + os.path.basename(self.rtf_path))
        self.refresh_table()
        self.refresh_sync()
        self.refresh_planning()

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
        if existing_j is not None and existing_r is not None and existing_j != existing_r:
            self._big_msg(
                QMessageBox.Icon.Warning, "Conflict",
                f"'{stroke}' has a conflict between JSON and RTF.\n\n"
                f"JSON: {existing_j}\nRTF: {existing_r}\n\n"
                "Resolve the conflict first (use Sync or Delete), "
                "or pick a different stroke.")
            return

        # warn if overwriting an existing entry
        if existing_j is not None or existing_r is not None:
            existing_val = existing_j or existing_r
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
            write_both(self.json_path, new_json, self.rtf_path, new_rtf)
        except Exception as e:
            QMessageBox.critical(self, "Write failed",
                                 f"Neither file changed.\n\n{e}")
            return
        self.json_entries, self.rtf_entries = new_json, new_rtf
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
            if jv is not None and rv is not None and jv == rv:
                continue  # already in sync
            # pick whichever side has it; for conflicts, prefer JSON
            val = jv if jv is not None else rv
            if val is None:
                continue
            new_json[stroke] = val
            new_rtf[stroke] = val
            synced.append(stroke)
        if not synced:
            self.status.setText("Selected entries are already in sync.")
            return
        try:
            write_both(self.json_path, new_json, self.rtf_path, new_rtf)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", str(e))
            return
        self.json_entries, self.rtf_entries = new_json, new_rtf
        for s in synced:
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
            write_both(self.json_path, new_json, self.rtf_path, new_rtf)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", str(e))
            return
        self.json_entries, self.rtf_entries = new_json, new_rtf
        for s in strokes:
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

    # ----- planning -----
    def _update_plan_hint(self):
        stroke = self.plan_stroke_in.text().strip()
        if not stroke:
            self.plan_hint.setText("")
            return
        jv = self.json_entries.get(stroke)
        rv = self.rtf_entries.get(stroke)
        if jv is not None and rv is not None:
            if jv == rv:
                self.plan_hint.setText(f"exists: \"{jv}\"")
                self.plan_hint.setStyleSheet("color: #66ccff; font-size: 15pt; font-weight: bold;")
            else:
                self.plan_hint.setText(f"CONFLICT — JSON: \"{jv}\" / RTF: \"{rv}\"")
                self.plan_hint.setStyleSheet("color: #ee6666; font-size: 15pt; font-weight: bold;")
        elif jv is not None:
            self.plan_hint.setText(f"in JSON only: \"{jv}\"")
            self.plan_hint.setStyleSheet("color: #66ccff; font-size: 15pt; font-weight: bold;")
        elif rv is not None:
            self.plan_hint.setText(f"in RTF only: \"{rv}\"")
            self.plan_hint.setStyleSheet("color: #66ccff; font-size: 15pt; font-weight: bold;")
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
            write_both(self.json_path, new_json, self.rtf_path, new_rtf)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", str(e))
            return
        self.json_entries, self.rtf_entries = new_json, new_rtf
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
        for i, entry in enumerate(self.planning):
            s = entry.get("stroke", "")
            t = entry.get("translation", "")
            if not s and not t:
                status, bg = "empty", INCOMPLETE_BG
            elif not s or not t:
                status, bg = "incomplete", INCOMPLETE_BG
            elif s in self.json_entries or s in self.rtf_entries:
                ej = self.json_entries.get(s)
                er = self.rtf_entries.get(s)
                if ej is not None and er is not None and ej != er:
                    status, bg = "conflict", CONFLICT_BG
                else:
                    status, bg = "exists", MISSING_J_BG
            else:
                status, bg = "ready", PLANNING_BG
            is_problem = status in ("conflict", "exists")
            for c, val in enumerate((s, t, status)):
                item = QTableWidgetItem(val)
                item.setBackground(bg)
                if is_problem:
                    item.setForeground(QColor(255, 100, 100))
                else:
                    item.setForeground(TEXT_COLOR)
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
        keys = sorted(set(self.json_entries) | set(self.rtf_entries))
        rows = []
        for k in keys:
            jv = self.json_entries.get(k, "")
            rv = self.rtf_entries.get(k, "")
            if q and q not in k.lower() and q not in jv.lower() and q not in rv.lower():
                continue
            rows.append((k, jv, rv))
        self.table.setRowCount(len(rows))
        for i, (k, jv, rv) in enumerate(rows):
            for c, val in enumerate((k, jv, rv)):
                item = QTableWidgetItem(val)
                if jv != rv:
                    item.setBackground(CONFLICT_BG)
                    item.setForeground(TEXT_COLOR)
                self.table.setItem(i, c, item)

    def refresh_sync(self):
        mij, mir, conf = diff(self.json_entries, self.rtf_entries)
        show_ignored = self.show_ignored_cb.isChecked()
        show_later = self.show_later_cb.isChecked()
        all_rows = (
            [(s, "conflict: translations differ", CONFLICT_BG) for s in conf]
            + [(s, "in JSON only — missing from RTF", MISSING_R_BG) for s in mir]
            + [(s, "in RTF only — missing from JSON", MISSING_J_BG) for s in mij]
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
                bg = IGNORED_BG
            elif tag == "later":
                bg = LATER_BG
            rows.append((s, msg, tag, bg))
        self.sync_table.setRowCount(len(rows))
        for i, (s, msg, tag, bg) in enumerate(rows):
            for c, val in enumerate((s, msg, tag)):
                item = QTableWidgetItem(val)
                item.setBackground(bg)
                item.setForeground(TEXT_COLOR)
                self.sync_table.setItem(i, c, item)
        total = len(all_rows)
        active = total - hidden
        if total == 0:
            self.status.setText("In sync.")
        elif hidden:
            self.status.setText(f"{active} out of sync ({hidden} hidden).")
        else:
            self.status.setText(f"{active} entries out of sync.")

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
        if jv == rv:
            self.formatted_cb.setChecked(False)
            self.plain_in.setText(jv)
        else:
            self.formatted_cb.setChecked(True)
            self.json_in.setText(jv)
            self.rtf_in.setText(rv)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = StenoSync()
    w.show()
    sys.exit(app.exec())
