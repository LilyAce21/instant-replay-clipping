"""ClipTrimmer - view and trim NVIDIA Instant Replay clips (smart cut, near-lossless)."""
import os, sys, re, json, locale, shutil, subprocess, tempfile, logging, logging.handlers, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", HERE))  # where ffmpeg/ffprobe/libmpv live
os.environ["PATH"] = os.pathsep.join([str(BUNDLE), str(BUNDLE / "ffmpeg"), os.environ.get("PATH", "")])
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(str(BUNDLE))
    if (BUNDLE / "ffmpeg").is_dir():
        os.add_dll_directory(str(BUNDLE / "ffmpeg"))

from PySide6.QtCore import Qt, QTimer, Signal, QRectF
from PySide6.QtGui import QPainter, QColor, QShortcut, QKeySequence, QPalette
from PySide6.QtWidgets import (QApplication, QWidget, QMainWindow, QHBoxLayout, QVBoxLayout, QListWidget,
                               QListWidgetItem, QComboBox, QPushButton, QLabel, QFileDialog, QMessageBox, QCheckBox, QInputDialog, QMenu, QStyledItemDelegate, QStyle,
                               QStyleOptionViewItem)

CONFIG_DIR = Path(os.environ.get("APPDATA", HERE)) / "ClipTrimmer"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG = CONFIG_DIR / "config.json"
LOGFILE = CONFIG_DIR / "cliptrimmer.log"
VERSION = "1.0.5"
logging.basicConfig(handlers=[logging.handlers.RotatingFileHandler(LOGFILE, maxBytes=1_000_000, backupCount=2,
                    encoding="utf-8")], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ct")


def _excepthook(t, v, tb):
    log.error("UNCAUGHT EXCEPTION\n%s", "".join(traceback.format_exception(t, v, tb)))
    sys.__excepthook__(t, v, tb)


sys.excepthook = _excepthook
EXTS = {".mp4", ".mkv", ".mov"}
EDIT_LIST_EXTS = {".mp4", ".mov", ".m4v"}  # containers that can hide the lead-in frames of a stream-copy cut
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


class ClipDelegate(QStyledItemDelegate):
    """Draws the clip name on the left and its file size right-aligned."""
    def sizeHint(self, opt, idx):
        sh = super().sizeHint(opt, idx)
        sh.setWidth(0)  # always use the list's width, so long names get elided instead of scrolling
        return sh

    def paint(self, p, opt, idx):
        o = QStyleOptionViewItem(opt)
        self.initStyleOption(o, idx)
        text, size = o.text, idx.data(Qt.UserRole + 1) or ""
        o.text = ""
        style = o.widget.style() if o.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, o, p, o.widget)
        selected = bool(o.state & QStyle.State_Selected)
        fm, pad = o.fontMetrics, 8
        sw = fm.horizontalAdvance(size)
        p.save()
        p.setPen(o.palette.color(QPalette.HighlightedText if selected else QPalette.Text))
        name_rect = o.rect.adjusted(pad, 0, -(sw + pad * 2), 0)
        p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(text, Qt.ElideMiddle, name_rect.width()))
        if not selected:
            p.setPen(o.palette.color(QPalette.PlaceholderText))
        p.drawText(o.rect.adjusted(0, 0, -pad, 0), Qt.AlignVCenter | Qt.AlignRight, size)
        p.restore()


def fmt(t):
    return f"{int(t // 60)}:{t % 60:06.3f}"


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NOWIN)
    log.info("RUN %s -> rc=%s", " ".join(f'"{c}"' if " " in c else c for c in cmd), r.returncode)
    if r.returncode != 0:
        log.error("stderr: %s", r.stderr[-2000:])
    return r


def probe(path):
    """Return dict with video size/codec/fps and audio track count."""
    r = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,start_time:format=duration",
             "-of", "json", path])
    try:
        d = json.loads(r.stdout)
        st = d.get("streams", [])
        v = next((x for x in st if x.get("codec_type") == "video"), {})
        return {"codec": v.get("codec_name"), "w": v.get("width"), "h": v.get("height"), "fps": v.get("r_frame_rate"),
                "audio": sum(1 for x in st if x.get("codec_type") == "audio"),
                "vstart": float(v.get("start_time") or 0),
                "astarts": [float(x.get("start_time") or 0) for x in st if x.get("codec_type") == "audio"],
                "dur": float(d.get("format", {}).get("duration", 0))}
    except Exception:
        log.exception("probe failed for %s", path)
        return {}


# ---------------------------------------------------------------- trimming engine
def encoder_args(src):
    hevc = (probe(src).get("codec") == "hevc")
    nv = ["-c:v", "hevc_nvenc" if hevc else "h264_nvenc", "-preset", "p7", "-tune", "hq",
          "-rc", "vbr", "-cq", "10", "-b:v", "0"]
    cpu = ["-c:v", "libx265" if hevc else "libx264", "-crf", "10", "-preset", "slow"]
    return nv, cpu


def trim(src, s, e, dst, clean):
    """Lossless mode: video/audio are stream-copied (bit-exact); the cut start is exact on playback.
    Clean mode: video is re-encoded near-lossless (NVENC) so the file contains only the trimmed range."""
    # Clean mode seeks in two steps (fast jump, then frame-exact trim) so audio and video stay in sync.
    pre = max(0.0, s - 5.0) if clean else s
    fine = ["-ss", f"{s - pre}"] if clean and s - pre > 0 else []
    base = ["ffmpeg", "-y", "-v", "error", "-ss", f"{pre}", "-i", src] + fine + ["-t", f"{e - s}",
            "-map", "0:v:0", "-map", "0:a?", "-dn", "-sn"]
    mov = ["-movflags", "+faststart"] if Path(dst).suffix.lower() in EDIT_LIST_EXTS else []
    if clean:
        nv, cpu = encoder_args(src)
        attempts = [base + enc + ["-c:a", "copy"] + mov + [dst] for enc in (nv, cpu)]
    else:
        attempts = [base + ["-c", "copy"] + mov + [dst]]
    err = ""
    for cmd in attempts:
        r = run(cmd)
        if r.returncode == 0:
            return
        err = r.stderr
    raise RuntimeError(err[-600:] or "ffmpeg failed")


def verify(src, dst, s, e):
    """Return None if the output looks good, else a description of the problem."""
    a, b = probe(src), probe(dst)
    log.info("verify src=%s dst=%s want=%.3f", a, b, e - s)
    if not b or not b.get("w") or not b.get("h"):
        return "output has no valid video stream"
    if b.get("audio") != a.get("audio"):
        return f"audio tracks: expected {a.get('audio')}, got {b.get('audio')}"
    if abs(b.get("dur", 0) - (e - s)) > 0.75:
        return f"duration {b.get('dur', 0):.2f}s, expected about {e - s:.2f}s"
    for st in b.get("astarts", []):
        if abs(st - b.get("vstart", 0)) > 0.25:
            return f"audio and video start {abs(st - b.get('vstart', 0)):.2f}s apart (out of sync)"
    for args in (["-t", "2"], ["-sseof", "-2"]):  # decode start and end to catch broken frames
        r = run(["ffmpeg", "-v", "error"] + args + ["-i", dst, "-f", "null", "-"])
        if r.returncode != 0 or "error" in r.stderr.lower():
            return "decode errors: " + r.stderr[-300:]
    return None


def unique_path(p):
    out, n = p, 2
    while out.exists():
        out = p.with_name(f"{p.stem} ({n}){p.suffix}")
        n += 1
    return out


def edit_name(p):
    m = re.match(r"^(.*?) (\d{4}\.\d{2}\.\d{2} - .+)$", p.name)
    name = f"{m[1]} [EDIT] {m[2]}" if m else f"{p.stem} [EDIT]{p.suffix}"
    if "[EDIT]" in p.name:
        name = p.name  # already an edit: just number it
    return unique_path(p.with_name(name))


def custom_name(src, text):
    """Turn what the user typed into a safe file path next to src, or None to use the default scheme."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()
    if text.lower().endswith(src.suffix.lower()):
        text = text[: -len(src.suffix)]
    text = text.rstrip(". ")
    if not text or text == src.stem:
        return None
    return unique_path(src.with_name(text + src.suffix))


# ---------------------------------------------------------------- timeline widget
class Timeline(QWidget):
    seek = Signal(float, bool)  # time, exact
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(56)
        self.dur, self.a, self.b, self.pos, self.drag = 1.0, 0.0, 1.0, 0.0, None

    def px(self, t):
        return 10 + (self.width() - 20) * t / max(self.dur, 0.001)

    def tm(self, x):
        return min(max((x - 10) / max(self.width() - 20, 1) * self.dur, 0), self.dur)

    def paintEvent(self, _):
        p, h = QPainter(self), self.height()
        p.fillRect(self.rect(), QColor(30, 30, 34))
        xa, xb = self.px(self.a), self.px(self.b)
        p.fillRect(QRectF(xa, 8, xb - xa, h - 16), QColor(60, 120, 220, 110))
        for x in (xa, xb):
            p.fillRect(QRectF(x - 4, 4, 8, h - 8), QColor(90, 160, 255))
        p.fillRect(QRectF(self.px(self.pos) - 1, 0, 2, h), QColor(255, 255, 255))

    def mousePressEvent(self, e):
        x = e.position().x()
        self.drag = "a" if abs(x - self.px(self.a)) < 9 else "b" if abs(x - self.px(self.b)) < 9 else "p"
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e):
        if not self.drag:
            return
        t = self.tm(e.position().x())
        if self.drag == "a":
            t = self.a = min(t, self.b - 0.05)
        elif self.drag == "b":
            t = self.b = max(t, self.a + 0.05)
        self.pos = t
        self.seek.emit(t, False)
        self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, _):
        if self.drag:
            self.seek.emit(self.pos, True)
        self.drag = None


# ---------------------------------------------------------------- main window
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"ClipTrimmer {VERSION}")
        self.resize(1400, 820)
        self.cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        self.clips, self.cur, self.init_pending = [], None, False

        self.video = QWidget()
        self.video.setStyleSheet("background:black")
        self.video.setAttribute(Qt.WA_NativeWindow)
        self.tl = Timeline()
        self.info = QLabel("")
        self.status = QLabel("")
        self.game = QComboBox()
        self.list = QListWidget()
        btn = lambda text, fn: (lambda b: (b.clicked.connect(fn), b)[1])(QPushButton(text))
        row = QHBoxLayout()
        for text, fn in [("Play/Pause (Space)", self.toggle), ("◀ Frame", lambda: self.step(-1)),
                         ("Frame ▶", lambda: self.step(1)), ("Set start (I)", self.set_a),
                         ("Set end (O)", self.set_b), ("Audio track", self.cycle_audio)]:
            row.addWidget(btn(text, fn))
        row.addStretch()
        self.clean = QCheckBox("Clean cut (re-encode)")
        self.clean.setToolTip("Off = lossless copy. On = near-lossless re-encode so the file holds only the trimmed range.")
        row.addWidget(self.clean)
        row.addWidget(btn("Open log folder", self.open_log))
        row.addWidget(btn("Save as new", lambda: self.export(False)))
        row.addWidget(btn("Replace original", lambda: self.export(True)))
        left = QVBoxLayout()
        left.addWidget(btn("Change clips folder…", self.pick_folder))
        left.addWidget(self.game)
        left.addWidget(self.list)
        right = QVBoxLayout()
        right.addWidget(self.video, 1)
        right.addWidget(self.tl)
        right.addWidget(self.info)
        right.addLayout(row)
        right.addWidget(self.status)
        lw = QWidget()
        lw.setFixedWidth(340)
        lw.setLayout(left)
        root = QWidget()
        h = QHBoxLayout(root)
        h.addWidget(lw)
        h.addLayout(right, 1)
        self.setCentralWidget(root)

        import mpv
        self.player = mpv.MPV(wid=str(int(self.video.winId())), vo="gpu", hwdec="auto-safe", keep_open="yes",
                              osc=False, input_default_bindings=False, input_vo_keyboard=False,
                              hr_seek="yes", pause=True,
                              log_handler=lambda l, c, t: log.info("mpv[%s] %s", c, t.strip()), loglevel="warn")
        for key, fn in [("Space", self.toggle), ("Left", lambda: self.step(-1)), ("Right", lambda: self.step(1)),
                        ("Shift+Left", lambda: self.jump(-5)), ("Shift+Right", lambda: self.jump(5)),
                        ("I", self.set_a), ("O", self.set_b)]:
            QShortcut(QKeySequence(key), self, activated=fn)
        self.tl.seek.connect(lambda t, ex: self.player.seek(t, "absolute", "exact" if ex else "keyframes"))
        self.tl.changed.connect(self.refresh_info)
        self.game.currentIndexChanged.connect(self.fill_list)
        self.list.currentRowChanged.connect(self.open_row)
        self.list.setItemDelegate(ClipDelegate(self.list))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.clip_menu)
        self.timer = QTimer(self, interval=30, timeout=self.tick)
        self.timer.start()
        QApplication.instance().applicationStateChanged.connect(
            lambda s: self.scan() if s == Qt.ApplicationActive else None)
        if not self.cfg.get("folder"):
            self.cfg["folder"] = str(Path.home() / "Videos")
            self.pick_folder()
        else:
            self.scan()

    # --- library
    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Select your NVIDIA clips folder", self.cfg["folder"])
        if d:
            self.cfg["folder"] = d
            CONFIG.write_text(json.dumps(self.cfg))
        self.scan()

    def scan(self):
        root, found = Path(self.cfg["folder"]), []
        for dp, _, files in os.walk(root):
            for f in files:
                if Path(f).suffix.lower() in EXTS:
                    p = Path(dp) / f
                    found.append((Path(dp).name if Path(dp) != root else "(Other)", p, p.stat().st_mtime, p.stat().st_size))
        self.clips = sorted(found, key=lambda c: -c[2])
        games, prev = sorted({c[0] for c in self.clips}), self.game.currentText()
        self.game.blockSignals(True)
        self.game.clear()
        self.game.addItems(["All games"] + games)
        if prev in games:
            self.game.setCurrentText(prev)
        self.game.blockSignals(False)
        self.fill_list()

    def fill_list(self):
        g, cur = self.game.currentText(), self.cur
        self.list.blockSignals(True)
        self.list.clear()
        self.shown = [c for c in self.clips if g in ("All games", c[0])]
        for _, p, _, size in self.shown:
            item = QListWidgetItem(p.name)
            item.setData(Qt.UserRole + 1, fmt_size(size))
            item.setToolTip(str(p))
            self.list.addItem(item)
            if p == cur:
                self.list.setCurrentRow(self.list.count() - 1)
        self.list.blockSignals(False)

    def clip_menu(self, pos):
        item = self.list.itemAt(pos)
        if not item:
            return
        row = self.list.row(item)
        menu = QMenu(self)
        act = menu.addAction("Delete…")
        if menu.exec(self.list.viewport().mapToGlobal(pos)) is act:
            self.delete_clip(self.shown[row][1])

    def delete_clip(self, p):
        size = fmt_size(p.stat().st_size) if p.exists() else "?"
        box = QMessageBox(QMessageBox.Warning, "Delete clip",
                          f"Are you sure you want to delete this clip?\n\n{p.name}  ({size})\n\n"
                          "It will be moved to the Recycle Bin.", QMessageBox.Yes | QMessageBox.No, self)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        try:
            from send2trash import send2trash
            if self.cur == p:
                self.player.command("stop")  # release the file lock on Windows
                self.cur = None
                self.tl.dur, self.tl.a, self.tl.b, self.tl.pos = 1.0, 0.0, 1.0, 0.0
                self.tl.update()
                self.info.setText("")
            send2trash(str(p))
            log.info("DELETE %s (moved to Recycle Bin)", p)
            self.status.setText(f"Deleted: {p.name}")
        except Exception as ex:
            log.exception("DELETE failed")
            QMessageBox.critical(self, "Delete failed", f"{ex}\n\nDetails are in the log: {LOGFILE}")
        self.scan()

    def open_row(self, i):
        if i >= 0:
            self.load(self.shown[i][1])

    def load(self, p):
        log.info("LOAD %s %s", p, probe(str(p)))
        self.cur, self.init_pending = p, True
        self.player.loadfile(str(p))
        self.player.pause = True
        self.status.setText(str(p))

    # --- playback
    def tick(self):
        d = self.player.duration
        if self.init_pending and d:
            self.tl.dur, self.tl.a, self.tl.b, self.init_pending = d, 0.0, d, False
            self.refresh_info()
        t = self.player.time_pos
        if t is not None and not self.tl.drag:
            self.tl.pos = t
            self.tl.update()

    def toggle(self):
        self.player.pause = not self.player.pause

    def step(self, d):
        self.player.command("frame-step" if d > 0 else "frame-back-step")

    def jump(self, s):
        self.player.seek(s, "relative", "exact")

    def cycle_audio(self):
        self.player.command("cycle", "audio")

    def set_a(self):
        self.tl.a = min(self.tl.pos, self.tl.b - 0.05)
        self.tl.update()
        self.refresh_info()

    def set_b(self):
        self.tl.b = max(self.tl.pos, self.tl.a + 0.05)
        self.tl.update()
        self.refresh_info()

    def refresh_info(self):
        self.info.setText(f"Start {fmt(self.tl.a)}   End {fmt(self.tl.b)}   Length {fmt(self.tl.b - self.tl.a)}")

    # --- export
    def export(self, replace):
        if not self.cur:
            return
        src, s, e, clean = self.cur, self.tl.a, self.tl.b, self.clean.isChecked()
        if not clean and src.suffix.lower() not in EDIT_LIST_EXTS:
            clean = True  # e.g. MKV cannot hide lead-in frames, so a copy would start too early
            log.info("Non-MP4 container (%s): using clean cut for an exact start", src.suffix)
        log.info("EXPORT replace=%s clean=%s src=%s start=%.3f end=%.3f", replace, clean, src, s, e)
        if replace and QMessageBox.question(self, "Replace original",
                "Replace the original with the trimmed version?\nThe original goes to the Recycle Bin.") != QMessageBox.Yes:
            return
        out = edit_name(src)
        if not replace:
            text, ok = QInputDialog.getText(self, "Save as new", "File name (leave unchanged to use the default [EDIT] name):",
                                            text=src.stem)
            if not ok:
                return
            out = custom_name(src, text) or out
            log.info("EXPORT name chosen: %s", out.name)
        tmp = src.with_name(src.stem + ".trimming" + src.suffix)
        target = tmp if replace else out
        self.status.setText("Exporting…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        final = src
        try:
            trim(str(src), s, e, str(target), clean)
            problem = verify(str(src), str(target), s, e)
            if problem:
                raise RuntimeError("Verification failed, original untouched: " + problem)
            if replace:
                from send2trash import send2trash
                st = src.stat()
                self.player.command("stop")  # release the file lock on Windows
                send2trash(str(src))
                os.replace(tmp, src)
                os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
            else:
                final = out
            log.info("EXPORT ok -> %s", final)
            self.status.setText(f"Saved: {final.name}")
        except Exception as ex:
            log.exception("EXPORT failed")
            if target.exists() and (replace or not src.samefile(target)):
                try:
                    target.unlink()
                except OSError:
                    pass
            QMessageBox.critical(self, "Export failed", f"{ex}\n\nDetails are in the log: {LOGFILE}")
            self.status.setText("Export failed (see log)")
        finally:
            QApplication.restoreOverrideCursor()
        self.scan()
        self.load(final if final.exists() else src)

    def open_log(self):
        os.startfile(str(CONFIG_DIR))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    if "--selftest" in sys.argv:  # used by the build to make sure nothing needed was stripped out
        ok = run(["ffmpeg", "-version"]).returncode == 0 and run(["ffprobe", "-version"]).returncode == 0
        import mpv  # raises if libmpv cannot be loaded
        sys.exit(0 if ok else 1)
    locale.setlocale(locale.LC_NUMERIC, "C")  # mpv requires this
    log.info("ClipTrimmer %s starting; log=%s", VERSION, LOGFILE)
    log.info("%s", run(["ffmpeg", "-version"]).stdout.splitlines()[:1])
    w = Main()
    w.show()
    sys.exit(app.exec())
