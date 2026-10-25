"""ClipTrimmer - view and trim NVIDIA Instant Replay clips (smart cut, near-lossless)."""
import os, sys, re, json, locale, shutil, subprocess, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", HERE))  # where ffmpeg/ffprobe/libmpv live
os.environ["PATH"] = str(BUNDLE) + os.pathsep + os.environ.get("PATH", "")
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(str(BUNDLE))

from PySide6.QtCore import Qt, QTimer, Signal, QRectF
from PySide6.QtGui import QPainter, QColor, QShortcut, QKeySequence
from PySide6.QtWidgets import (QApplication, QWidget, QMainWindow, QHBoxLayout, QVBoxLayout, QListWidget,
                               QListWidgetItem, QComboBox, QPushButton, QLabel, QFileDialog, QMessageBox)

CONFIG_DIR = Path(os.environ.get("APPDATA", HERE)) / "ClipTrimmer"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG = CONFIG_DIR / "config.json"
EXTS = {".mp4", ".mkv", ".mov"}
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def fmt(t):
    return f"{int(t // 60)}:{t % 60:06.3f}"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NOWIN)


# ---------------------------------------------------------------- trimming engine
def next_keyframe(src, s):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{s}%+12",
             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", src])
    for line in r.stdout.splitlines():
        p = line.split(",")
        if len(p) >= 2 and "K" in p[1]:
            try:
                t = float(p[0])
            except ValueError:
                continue
            if t >= s - 0.001:
                return t
    return None


def encoder_args(src):
    codec = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                 "-of", "csv=p=0", src]).stdout.strip()
    hevc = codec == "hevc"
    nv = ["-c:v", "hevc_nvenc" if hevc else "h264_nvenc", "-preset", "p7", "-tune", "hq",
          "-rc", "vbr", "-cq", "12", "-b:v", "0"]
    cpu = ["-c:v", "libx265" if hevc else "libx264", "-crf", "12", "-preset", "slow"]
    return nv, cpu


def trim(src, s, e, dst):
    """Frame-accurate cut: copy everything, re-encode only the few frames before the first keyframe."""
    maps = ["-map", "0:v:0", "-map", "0:a?", "-dn", "-sn"]
    base = ["ffmpeg", "-y", "-v", "error", "-avoid_negative_ts", "make_zero"]
    k = next_keyframe(src, s)
    nv, cpu = encoder_args(src)
    tmp = Path(tempfile.mkdtemp(dir=Path(dst).parent))

    def go(cmds):
        for c in cmds:
            r = run(c)
            if r.returncode == 0:
                return
            err = r.stderr
        raise RuntimeError(err[-600:])

    try:
        if k is not None and k - s < 0.04:  # already on a keyframe: pure stream copy
            go([base + ["-ss", f"{k + 0.001}", "-i", src, "-t", f"{e - k}"] + maps +
                ["-c", "copy", "-movflags", "+faststart", dst]])
        elif k is None or k >= e:  # whole cut sits inside one GOP: re-encode it
            go([base + ["-ss", f"{s}", "-i", src, "-t", f"{e - s}"] + maps + enc + ["-c:a", "copy", dst]
                for enc in (nv, cpu)])
        else:
            a, b, lst = tmp / "a.ts", tmp / "b.ts", tmp / "l.txt"
            go([base + ["-ss", f"{s}", "-i", src, "-t", f"{k - s}"] + maps + enc + ["-c:a", "copy", str(a)]
                for enc in (nv, cpu)])
            go([base + ["-ss", f"{k + 0.001}", "-i", src, "-t", f"{e - k}"] + maps + ["-c", "copy", str(b)]])
            lst.write_text(f"file '{a.as_posix()}'\nfile '{b.as_posix()}'\n")
            go([["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0",
                 "-c", "copy", "-movflags", "+faststart", dst]])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def edit_name(p):
    m = re.match(r"^(.*?) (\d{4}\.\d{2}\.\d{2} - .+)$", p.name)
    name = f"{m[1]} [EDIT] {m[2]}" if m else f"{p.stem} [EDIT]{p.suffix}"
    out, n = p.with_name(name), 2
    while out.exists():
        out = p.with_name(Path(name).stem + f" ({n})" + p.suffix)
        n += 1
    return out


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
        self.setWindowTitle("ClipTrimmer")
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
                              hr_seek="yes", pause=True)
        for key, fn in [("Space", self.toggle), ("Left", lambda: self.step(-1)), ("Right", lambda: self.step(1)),
                        ("Shift+Left", lambda: self.jump(-5)), ("Shift+Right", lambda: self.jump(5)),
                        ("I", self.set_a), ("O", self.set_b)]:
            QShortcut(QKeySequence(key), self, activated=fn)
        self.tl.seek.connect(lambda t, ex: self.player.seek(t, "absolute", "exact" if ex else "keyframes"))
        self.tl.changed.connect(self.refresh_info)
        self.game.currentIndexChanged.connect(self.fill_list)
        self.list.currentRowChanged.connect(self.open_row)
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
                    found.append((Path(dp).name if Path(dp) != root else "(Other)", p, p.stat().st_mtime))
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
        for _, p, _ in self.shown:
            self.list.addItem(QListWidgetItem(p.name))
            if p == cur:
                self.list.setCurrentRow(self.list.count() - 1)
        self.list.blockSignals(False)

    def open_row(self, i):
        if i >= 0:
            self.load(self.shown[i][1])

    def load(self, p):
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
        src, s, e = self.cur, self.tl.a, self.tl.b
        if replace and QMessageBox.question(self, "Replace original",
                                            "Overwrite the original clip with the trimmed version?") != QMessageBox.Yes:
            return
        dst = src.with_name(src.stem + ".trimming" + src.suffix) if replace else edit_name(src)
        self.status.setText("Exporting…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            trim(str(src), s, e, str(dst))
            if replace:
                st = src.stat()
                self.player.command("stop")  # release the file lock on Windows
                os.replace(dst, src)
                os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
                dst = src
            self.status.setText(f"Saved: {dst.name}")
        except Exception as ex:
            if replace and dst.exists():
                dst.unlink()
            QMessageBox.critical(self, "Export failed", str(ex))
            self.status.setText("Export failed")
        finally:
            QApplication.restoreOverrideCursor()
        self.scan()
        self.load(dst if dst.exists() else src)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    locale.setlocale(locale.LC_NUMERIC, "C")  # mpv requires this
    w = Main()
    w.show()
    sys.exit(app.exec())
