"""patra (पात्र, "vessel"): Docker containers for Vyom.

Every container, grouped by compose project, the ones that need you first
(unhealthy, restarting, failed), with live CPU and memory sparklines; the
strip above says how many run, need you and are stopped; the preview shows
the image, ports and the last log lines in level colours. Never wakes a
stopped Docker daemon (it's often socket-activated): it says Docker isn't
running instead.
"""
import collections
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime

from gi.repository import GLib, Gtk, Pango
from vyom import Hint, Row, Section, ui, util
from vyom import Plugin as Base

# keybind: Vyom/docker | Enter | Follow the selected container's logs in a terminal
# keybind: Vyom/docker | Ctrl+L | Follow the selected container's logs in a terminal (same as Enter)
# keybind: Vyom/docker | Ctrl+R | Restart the selected container
# keybind: Vyom/docker | Ctrl+S | Open a shell in the selected container
# keybind: Vyom/docker | Ctrl+X | Stop the selected container, or start a stopped one
# keybind: Vyom/docker | Ctrl+Backspace | Remove the selected stopped container (press twice: asks first)

HISTORY = 24          # stats samples kept per container while Vyom runs (~1 a refresh)
EVENTS_EVERY = 20     # seconds between reads of the last 10 minutes' restarts
ARM_SECONDS = 5       # a second Ctrl+Backspace within this removes

# What a container's state means: (word, colour, icon). Red needs you.
STATES = {"unhealthy": ("Unhealthy", "red", "\U000f0028"), "restarting": ("Restarting", "red", "\U000f0450"),
          "failed": ("Failed", "red", "\U000f0156"), "healthy": ("Healthy", "green", "\U000f05e0"),
          "running": ("Running", "green", "\U000f040a"), "paused": ("Paused", "yellow", "\U000f03e4"),
          "stopped": ("Stopped", "gray", "\U000f04db")}
TROUBLE = {"unhealthy", "restarting", "failed"}


def daemon_up():
    if shutil.which("systemctl"):
        return util.run(["systemctl", "is-active", "--quiet", "docker.service"])[0] == 0
    return util.run(["docker", "info", "--format", "x"], timeout=3)[0] == 0


def health(c):
    st, status = c.get("State", ""), c.get("Status", "")
    if st == "running":
        return "unhealthy" if "(unhealthy)" in status else "healthy" if "(healthy)" in status else "running"
    if st == "restarting":
        return "restarting"
    if st == "paused":
        return "paused"
    if st == "exited" and "Exited (0)" not in status:
        return "failed"
    return "stopped"


def to_bytes(text):
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B)", text or "")
    if not m:
        return 0.0
    mult = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KiB": 1024, "MiB": 1024 ** 2,
            "GiB": 1024 ** 3, "TiB": 1024 ** 4}.get(m.group(2), 1)
    return float(m.group(1)) * mult


def short_bytes(n):
    for unit, size in (("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if n >= size:
            v = n / size
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return f"{n:.0f}B"


def pct(text):
    try:
        return float((text or "").strip().rstrip("%"))
    except ValueError:
        return 0.0


def theme_hex(name, fallback):
    """A theme colour (@red...) as #rrggbb for Pango markup (main thread)."""
    try:
        w = Gtk.Label()
        w.add_css_class(f"t-{name}")
        c = w.get_color()
        return "#%02x%02x%02x" % (round(c.red * 255), round(c.green * 255), round(c.blue * 255))
    except (AttributeError, TypeError):
        return fallback


class Spark(Gtk.DrawingArea):
    """A small bar sparkline of the last values, in its CSS colour
    (t-green / t-red), so a theme applies."""

    def __init__(self, values, color, top=None, w=84, h=12):
        super().__init__(content_width=w, content_height=h, valign=Gtk.Align.CENTER)
        self.values, self.top = list(values), top
        self.add_css_class("drawn")
        self.add_css_class(f"t-{color}")
        self.set_draw_func(self.draw)

    def draw(self, _a, cr, w, h):
        c = self.get_color()
        cr.set_source_rgba(c.red, c.green, c.blue, 0.14)
        cr.rectangle(0, h - 1, w, 1)
        cr.fill()
        if not self.values:
            return
        top = self.top or max(max(self.values), 1e-9)
        bw = w / HISTORY
        x0 = w - bw * len(self.values)
        for i, v in enumerate(self.values):
            bh = max(1.0, min(1.0, v / top) * h) if top else 1.0
            cr.set_source_rgba(c.red, c.green, c.blue, 1 if i == len(self.values) - 1 else 0.7)
            cr.rectangle(x0 + i * bw + 0.5, h - bh, max(1.0, bw - 1.2), bh)
            cr.fill()


class Plugin(Base):
    name, title, icon, color = "patra", "Docker", "", "blue"
    placeholder = "Filter containers…"
    preview = True
    preview_width = 340
    live = 3
    hints = [Hint("Enter", "logs"), Hint("Ctrl+R", "restart"), Hint("Ctrl+S", "shell"), Hint("Ctrl+X", "stop/start")]
    empty_text = "No containers"

    def __init__(self, vyom, conf):
        super().__init__(vyom, conf)
        self.stats = {}
        self.stats_busy = False
        self.hist = {}            # id12 -> deque of (cpu %, memory bytes)
        self.restarts = {}        # id12 -> deaths in the last 10 minutes
        self.events_at = 0.0
        self.counts = None        # (running, trouble, stopped) for the strip
        self.armed = None         # (id, time): Ctrl+Backspace once, a second time removes
        self.colors = {"red": "#f38ba8", "yellow": "#f9e2af", "green": "#a6e3a1", "dim": "#6c7086"}
        self.terminal = self.conf.get("terminal") or os.environ.get("TERMINAL") or \
            next((t for t in ("kitty", "foot", "alacritty", "wezterm") if shutil.which(t)), "xterm")

    def opened(self, args):
        super().opened(args)
        self.armed = None
        self.colors = {"red": theme_hex("red", "#f38ba8"), "yellow": theme_hex("yellow", "#f9e2af"),
                       "green": theme_hex("green", "#a6e3a1"), "dim": theme_hex("gray", "#7f849c")}

    # ------------------------------------------------------------ reading
    def update_stats(self):
        """docker stats takes ~1 s: run it on the side, show the last result
        (and keep a short history of it, for the sparklines)."""
        if self.stats_busy:
            return
        self.stats_busy = True

        def go():
            try:
                code, out = util.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=10)
                st = {}
                for line in out.splitlines() if code == 0 else []:
                    try:
                        j = json.loads(line)
                        st[j["ID"][:12]] = j
                    except (ValueError, KeyError):
                        pass
                for cid, j in st.items():
                    h = self.hist.setdefault(cid, collections.deque(maxlen=HISTORY))
                    h.append((pct(j.get("CPUPerc")), to_bytes(j.get("MemUsage", "").split(" / ")[0])))
                for cid in list(self.hist):
                    if cid not in st:
                        del self.hist[cid]
                self.stats = st
                if time.time() - self.events_at > EVENTS_EVERY:
                    self.events_at = time.time()
                    self.restarts = self.read_restarts()
            finally:
                self.stats_busy = False
        threading.Thread(target=go, daemon=True).start()

    def read_restarts(self):
        """How often each container died in the last 10 minutes (a
        restart loop), from docker events."""
        code, out = util.run(["docker", "events", "--since", "10m", "--until", str(int(time.time())),
                              "--filter", "type=container", "--filter", "event=die", "--format", "{{json .}}"],
                             timeout=6)
        n = {}
        for line in out.splitlines() if code == 0 else []:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            cid = (j.get("id") or j.get("Actor", {}).get("ID") or "")[:12]
            if cid:
                n[cid] = n.get(cid, 0) + 1
        return n

    def search(self, q):
        if not daemon_up():
            self.counts = None
            return [Row("Docker isn't running", "Enter starts it (docker.service)", icon="", color="gray",
                        data={"kind": "start"})]
        self.update_stats()
        code, out = util.run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=8)
        conts = []
        for line in out.splitlines() if code == 0 else []:
            try:
                c = json.loads(line)
            except ValueError:
                continue
            c["health"] = health(c)
            conts.append(c)
        self.counts = (sum(c["State"] == "running" and c["health"] not in TROUBLE for c in conts),
                       sum(c["health"] in TROUBLE for c in conts),
                       sum(c["health"] in ("stopped", "paused") for c in conts))
        groups = {}
        for c in conts:
            if q and util.fuzzy(q, c["Names"] + " " + c["Image"]) is None:
                continue
            labels = dict(kv.split("=", 1) for kv in c.get("Labels", "").split(",") if "=" in kv)
            groups.setdefault(labels.get("com.docker.compose.project", ""), []).append(c)

        def order(c):   # the ones that need you, then running, then stopped
            return (0 if c["health"] in TROUBLE else 1 if c["State"] == "running" else 2, c["Names"])

        def trouble(p):
            return any(c["health"] in TROUBLE for c in groups[p])
        rows = []
        for proj in sorted(groups, key=lambda p: (not trouble(p), p == "", p)):
            items = sorted(groups[proj], key=order)
            up = sum(c["State"] == "running" for c in items)
            note = "needs you" if trouble(proj) else "all running" if up == len(items) else \
                "stopped" if up == 0 else f"{up} of {len(items)} running"
            rows.append(Section(f"{proj} · compose" if proj else "Containers", len(items), note))
            rows += [self.row(c) for c in items]
        return rows

    def row(self, c):
        h = c["health"]
        word, color, icon = STATES[h]
        status = c.get("Status", "")
        when = re.sub(r"\s*\((un)?healthy\)|\s*\(health: starting\)", "", status)
        when = when[0].lower() + when[1:] if when else ""
        sub = f"{word} · {when}" if when else word
        dead = self.restarts.get(c["ID"][:12], 0)
        if dead and h in TROUBLE:
            sub = f"{word} · restarted {dead}× in 10 min"
        ports = c.get("Ports", "").replace("0.0.0.0:", ":").replace("127.0.0.1:", ":").replace("[::]:", ":")
        port = ports.split(",")[0].replace("->", " → ").strip() if ports else ""
        if port and h not in TROUBLE:
            sub += f" · {port}"
        sub = GLib.markup_escape_text(sub)
        if h in TROUBLE:
            sub = f"<span foreground='{self.colors['red']}'>{sub}</span>"
        return Row(GLib.markup_escape_text(c["Names"]), sub, icon=icon, color=color, markup=True,
                   dim=h == "stopped", data={"kind": "c", **c})

    # ------------------------------------------------------------ the strip
    def build_strip(self):
        """How many run, need you and are stopped; and the sparklines on the
        rows (drawn here: the panel calls this right after it built them)."""
        self.add_sparks()
        if not self.counts:
            return None
        run, bad, stop = self.counts
        specs = [(f"{run} running", "green", "\U000f040a")]
        if bad:
            specs.append((f"{bad} unhealthy", "red", "\U000f0028"))
        specs.append((f"{stop} stopped", "gray", "\U000f04db"))
        row = ui.hbox(*[ui.chip(*s) for s in specs], spacing=8)
        if bad:
            row.append(ui.label("they need you: first in the list", "patra-strip-note",
                                hexpand=True, xalign=1, valign=Gtk.Align.CENTER))
        row.add_css_class("patra-strip")
        return row

    def add_sparks(self):
        panel = self.panel
        for r, w in getattr(panel, "items", []):
            c = r.data or {}
            if c.get("kind") != "c" or c.get("State") != "running":
                continue
            box = w.get_child() if hasattr(w, "get_child") else None
            text = box.get_first_child().get_next_sibling() if box is not None else None
            if text is None or getattr(box, "_patra_spark", False):
                continue
            box._patra_spark = True
            box.insert_child_after(self.spark(c), text)

    def spark(self, c):
        cid = c["ID"][:12]
        hist = list(self.hist.get(cid, ()))
        color = "red" if c["health"] in TROUBLE else "green"
        cpu = [h[0] for h in hist]
        mem = [h[1] for h in hist]
        cpu_text = f"{cpu[-1]:.0f}%" if cpu else "…"
        mem_text = short_bytes(mem[-1]) if mem else "…"

        def line(name, vals, top, text):
            return ui.hbox(ui.label(name, "patra-spark-name", xalign=0), Spark(vals, color, top),
                           ui.label(text, "patra-spark-val", xalign=1), spacing=6)
        box = ui.vbox(line("CPU", cpu, max([100.0] + cpu), cpu_text),
                      line("MEM", mem, max(mem) * 1.15 if mem else None, mem_text), spacing=3)
        box.set_valign(Gtk.Align.CENTER)
        box.set_tooltip_text(f"CPU {cpu_text} · memory {mem_text} (the last {len(hist)} readings)")
        return box

    # ------------------------------------------------------------ the preview
    def color_logs(self, text):
        """docker logs --timestamps → Pango: times dim, ERROR red, WARN
        yellow, INFO green."""
        out = []
        for line in text.splitlines()[-14:]:
            ts, _, msg = line.partition(" ")
            stamp = ""
            if re.match(r"\d{4}-\d\d-\d\dT", ts):
                try:
                    stamp = datetime.fromisoformat(ts[:19] + "+00:00").astimezone().strftime("%H:%M:%S")
                except ValueError:
                    stamp = ts[11:19]
            else:
                msg = line
            msg = GLib.markup_escape_text(msg[:220])
            for pat, col in ((r"\b(ERROR|ERR|FATAL|CRITICAL|PANIC|error|fatal)\b", "red"),
                             (r"\b(WARN|WARNING|warn|warning)\b", "yellow"), (r"\b(INFO|info)\b", "green")):
                msg, n = re.subn(pat, lambda m, c=col: f"<span foreground='{self.colors[c]}'>{m.group(0)}</span>",
                                 msg, count=1)
                if n:
                    break
            out.append((f"<span foreground='{self.colors['dim']}'>{stamp}</span> " if stamp else "") + msg)
        return "\n".join(out)

    def build_preview(self, row):
        c = row.data or {}
        if c.get("kind") != "c":
            return ui.vbox(ui.title("Docker"), ui.body("The daemon is stopped. Enter starts it."))
        word, color, _ = STATES[c["health"]]
        head = ui.hbox(ui.title(c["Names"]), ui.chip(word, color), spacing=8)
        ports = c.get("Ports", "").replace("0.0.0.0:", ":").replace("127.0.0.1:", ":").replace("->", " → ")
        parts = [head, ui.meta(" · ".join(x for x in (c["Image"], ports.split(", ")[0] if ports else "") if x))]
        st = self.stats.get(c["ID"][:12], {})
        facts = [("Status", c.get("Status", ""))]
        if st:
            facts += [("CPU", st.get("CPUPerc", "")), ("Memory", st.get("MemUsage", ""))]
        if ports and "," in ports:
            facts.append(("Ports", ports.replace(", ", "\n")))
        parts.append(ui.kv(facts))
        logs = ui.label("loading logs…", "p-mono", xalign=0, wrap=True)
        logs.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        parts.append(logs)

        def got(r):
            if isinstance(r, tuple):
                body = self.color_logs(r[1][-6000:])
                if body:
                    logs.set_markup(body)
                else:
                    logs.set_label("(no logs)")
        self.background(lambda: util.run(f"docker logs --timestamps --tail 14 {c['ID']} 2>&1", timeout=6), got)
        running = c["State"] == "running"
        if self.armed and self.armed[0] == c["ID"] and time.time() - self.armed[1] < ARM_SECONDS:
            parts.append(ui.notice(f"Remove {c['Names']}?", "Ctrl+Backspace again removes it. This can't be undone.",
                                   "red"))
        if running:
            btns = [("Restart", lambda: self.do(c, "restart"), "primary", "", "Ctrl+R"),
                    ("Shell", lambda: self.shell(c), "", "", "Ctrl+S"),
                    ("Logs", lambda: self.logs(c), "", "", "Ctrl+L"),
                    ("Stop", lambda: self.do(c, "stop"), "danger", "", "Ctrl+X")]
        else:
            btns = [("Start", lambda: self.do(c, "start"), "ok", "", "Ctrl+X"),
                    ("Logs", lambda: self.logs(c), "", "", "Ctrl+L"),
                    ("Remove", lambda: self.remove(c), "danger", "", "Ctrl+⌫")]
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8,
                           max_children_per_line=4, homogeneous=False)
        flow.add_css_class("chips")
        for b in btns:
            flow.append(ui.button(*b))
        parts.append(flow)
        return ui.vbox(*parts, spacing=10)

    # ------------------------------------------------------------ actions
    def do(self, c, verb):
        self.set_status(f"{verb} {c['Names']}…")
        self.background(lambda: util.run(["docker", verb, c["ID"]], timeout=60),
                        lambda r: (self.set_status("" if isinstance(r, tuple) and r[0] == 0
                                                   else f"{verb} failed"), self.refresh()))

    def remove(self, c):
        """Asks first: the first press arms it (a red note in the preview),
        a second within a few seconds removes."""
        if c["State"] == "running":
            self.set_status("stop it first (Ctrl+X)")
            return
        if self.armed and self.armed[0] == c["ID"] and time.time() - self.armed[1] < ARM_SECONDS:
            self.armed = None
            self.do(c, "rm")
            return
        self.armed = (c["ID"], time.time())
        self.set_status(f"Ctrl+Backspace again removes {c['Names']}")
        if self.panel:
            self.panel.update_preview_soon()

    def term(self, cmd, title):
        if os.path.basename(self.terminal) == "kitty":
            util.spawn(["kitty", "--title", title, "-e", *cmd])
        else:
            util.spawn([self.terminal, "-e", *cmd])

    def shell(self, c):
        if c["State"] != "running":
            self.set_status("start it first (Ctrl+X)")
            return
        self.term(["docker", "exec", "-it", c["ID"], "sh", "-c", "command -v bash >/dev/null && exec bash || exec sh"],
                  f"{c['Names']} shell")
        self.close()

    def logs(self, c):
        self.term(["docker", "logs", "-f", "--tail", "200", c["ID"]], f"{c['Names']} logs")
        self.close()

    def activate(self, row, mods):
        c = row.data or {}
        if c.get("kind") == "start":
            util.spawn(["docker", "info"])  # socket activation starts the daemon
            self.set_status("starting Docker…")
            return False
        if c.get("kind") == "c":
            self.logs(c)
        return False

    def key(self, name, row, mods):
        if mods != {"ctrl"} or row is None or (row.data or {}).get("kind") != "c":
            return False
        c = row.data
        if name == "r":
            self.do(c, "restart")
        elif name == "s":
            self.shell(c)
        elif name == "l":
            self.logs(c)
        elif name == "x":
            self.do(c, "stop" if c["State"] == "running" else "start")
        elif name == "BackSpace":
            self.remove(c)
        else:
            return False
        return True
