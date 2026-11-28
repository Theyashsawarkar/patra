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
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime

from gi.repository import GLib, Gtk, Pango
from vyom import Hint, Row, Section, ui, util
from vyom import Plugin as Base

from . import engine, vim

# KEYBINDS (generated from patra/vim.py by scripts/keybinds.py; don't edit by hand)
# keybind: Vyom/docker:normal | Enter | Follow the selected container's logs in a terminal
# keybind: Vyom/docker:normal | o | Open a shell in the selected container
# keybind: Vyom/docker:normal | x | Stop the selected container, or start a stopped one
# keybind: Vyom/docker:normal | R | Restart the selected container
# keybind: Vyom/docker:normal | d d | Remove the selected stopped container (y confirms)
# keybind: Vyom/docker:normal | y y | Copy the container's name
# keybind: Vyom/docker:normal | r | Reload the list now
# keybind: Vyom/docker leader:normal | Space c r | Restart the selected container
# keybind: Vyom/docker leader:normal | Space c s | Stop the selected container, or start a stopped one
# keybind: Vyom/docker leader:normal | Space c l | Follow the selected container's logs in a terminal
# keybind: Vyom/docker leader:normal | Space c h | Open a shell in the selected container
# keybind: Vyom/docker leader:normal | Space o t | Open a shell in the selected container, in a terminal
# keybind: Vyom/docker leader:normal | Space y y | Copy the container's name
# keybind: Vyom/docker leader:normal | Space y i | Copy the container's ID
# keybind: Vyom/docker leader:normal | Space y p | Copy the container's ports
# keybind: Vyom/docker leader:normal | Space f r | Show only running containers (again: show all)
# keybind: Vyom/docker leader:normal | Space f s | Show only stopped containers (again: show all)
# keybind: Vyom/docker leader:normal | Space f t | Show only the ones that need you (again: show all)
# keybind: Vyom/docker leader:normal | Space f c | Clear the filter: show every container
# keybind: Vyom/docker leader:normal | Space s c | Sort the containers by CPU (busiest first; again: the usual order)
# keybind: Vyom/docker leader:normal | Space s m | Sort the containers by memory (biggest first; again: the usual order)
# keybind: Vyom/docker leader:normal | Space s n | Sort the containers by name (A to Z; again: the usual order)
# keybind: Vyom/docker leader:normal | Space d d | Remove the selected stopped container (y confirms)
# END KEYBINDS

HISTORY = 24          # stats samples kept per container while Vyom runs (~1 a refresh)
TICK = 3              # seconds between readings while the panel shows
SOON = 1              # a container read for the first time is read again this soon (CPU needs two readings)
EVENTS_EVERY = 20     # seconds between reads of the last 10 minutes' restarts
FRESH = 1.5           # seconds a reading is new enough for search() to filter it instead of asking again
CLI_LOGS_EVERY = 3    # without the socket, the preview's logs every this many readings (a `docker logs` each)
AWAY_EVERY = 4        # rows scrolled out of sight: their stats every this many readings (Docker has no
                      # call for many containers' stats: each is a request, so 40 running cost 40 a reading)

# What a container's state means: (word, colour, icon). Red needs you.
STATES = {"unhealthy": ("Unhealthy", "red", "\U000f0028"), "restarting": ("Restarting", "red", "\U000f0450"),
          "failed": ("Failed", "red", "\U000f0156"), "healthy": ("Healthy", "green", "\U000f05e0"),
          "running": ("Running", "green", "\U000f040a"), "paused": ("Paused", "yellow", "\U000f03e4"),
          "stopped": ("Stopped", "gray", "\U000f04db")}
TROUBLE = {"unhealthy", "restarting", "failed"}


def daemon_up():
    """Without starting a command: systemd over D-Bus (engine.up())."""
    try:
        return engine.up()
    except Exception:   # noqa: BLE001  (then the old way)
        pass
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


def mem_text(n):
    """Bytes as `docker stats` writes them: "12.3MiB", "512B"."""
    return f"{short_bytes(n)}iB" if n >= 1024 else f"{n:.0f}B"


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


def set_text(label, text, markup=False):
    """A label's text, only when it changed (an unchanged one isn't laid out or drawn again)."""
    if getattr(label, "_patra_text", None) != (text, markup):
        label._patra_text = (text, markup)
        if markup:
            label.set_markup(text)
        else:
            label.set_label(text)


def recolor(widget, prefix, color, colors=("red", "green", "yellow", "gray")):
    for c in colors:
        if c != color:
            widget.remove_css_class(f"{prefix}{c}")
    widget.add_css_class(f"{prefix}{color}")


# ------------------------------------------------------------------ reading (worker threads, no widgets)
def cli_ps():
    code, out = util.run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=8)
    conts = []
    for line in out.splitlines() if code == 0 else []:
        try:
            conts.append(json.loads(line))
        except ValueError:
            continue
    return conts


def cli_stats():
    """`docker stats --no-stream` (a second or two) as {id12: {cpu, mem, limit}}."""
    code, out = util.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=10)
    st = {}
    for line in out.splitlines() if code == 0 else []:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        use, _, limit = j.get("MemUsage", "").partition(" / ")
        if j.get("ID"):
            st[j["ID"][:12]] = {"cpu": pct(j.get("CPUPerc")), "mem": to_bytes(use), "limit": to_bytes(limit)}
    return st


def cli_restarts():
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


def cli_logs(cid):
    code, out = util.run(["docker", "logs", "--timestamps", "--tail", "14", cid], timeout=6)
    return out if code == 0 else None


class Spark(Gtk.DrawingArea):
    """A small bar sparkline of the last values, in its CSS colour
    (t-green / t-red), so a theme applies. set() redraws only this
    area, and only when the values changed."""

    def __init__(self, color, w=84, h=12):
        super().__init__(content_width=w, content_height=h, valign=Gtk.Align.CENTER)
        self.values, self.top = [], None
        self.add_css_class("drawn")
        self.add_css_class(f"t-{color}")
        self.set_draw_func(self.draw)

    def set(self, values, top):
        values = list(values)
        if values != self.values or top != self.top:
            self.values, self.top = values, top
            self.queue_draw()

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


class Sparks(Gtk.Box):
    """A running container's CPU and memory on its row: two sparklines and
    the last figures (fixed-width, monospaced: a new figure never moves
    anything), updated in place on every reading."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3, valign=Gtk.Align.CENTER)
        self.color = "green"
        self.lines = []
        for name in ("CPU", "MEM"):
            spark, val = Spark(self.color), ui.label("…", "patra-spark-val", xalign=1)
            val.set_width_chars(5)
            self.lines.append((spark, val))
            self.append(ui.hbox(ui.label(name, "patra-spark-name", xalign=0), spark, val, spacing=6))
        self.tip = None

    def update(self, hist, last, color):
        if color != self.color:
            self.color = color
            for spark, _ in self.lines:
                recolor(spark, "t-", color)
        cpu = [h[0] for h in hist]
        mem = [h[1] for h in hist]
        last = last or {}
        cpu_text = f"{last['cpu']:.0f}%" if last.get("cpu") is not None else f"{cpu[-1]:.0f}%" if cpu else "…"
        mem_now = last.get("mem", mem[-1] if mem else None)
        mem_s = short_bytes(mem_now) if mem_now is not None else "…"
        (cs, cv), (ms, mv) = self.lines
        cs.set(cpu, max([100.0] + cpu))
        ms.set(mem, max(mem) * 1.15 if mem else None)
        set_text(cv, cpu_text)
        set_text(mv, mem_s)
        tip = f"CPU {cpu_text} · memory {mem_s} (the last {len(hist)} readings)"
        if tip != self.tip:
            self.tip = tip
            self.set_tooltip_text(tip)


class Detail:
    """The preview of one container, built once and then updated in place:
    the same widgets every refresh (no "loading logs…" flash, nothing laid
    out again unless a text changed)."""

    def __init__(self, plugin, c):
        self.p, self.c = plugin, c
        self.key = (c["State"], c["health"])
        word, color, _ = STATES[c["health"]]
        self.name = ui.title(c["Names"])
        self.chip = ui.chip(word, color)
        head = ui.hbox(self.name, self.chip, spacing=8)
        self.meta = ui.meta("")
        running = c["State"] == "running"
        facts = [("Status", "")] + ([("CPU", "…"), ("Memory", "…")] if running else [])
        self.multi_ports = "," in c.get("Ports", "")
        if self.multi_ports:
            facts.append(("Ports", ""))
        self.kv = ui.kv(facts)
        self.vals = {k: self.kv.get_child_at(1, i) for i, (k, _) in enumerate(facts)}
        for k in ("CPU", "Memory"):
            if k in self.vals:
                self.vals[k].add_css_class("patra-num")
        self.logs = ui.label("loading logs…", "p-mono", xalign=0, wrap=True)
        self.logs.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.logs_set = False
        if running:
            btns = [("Restart", lambda: plugin.do(self.c, "restart"), "primary", "", "R"),
                    ("Shell", lambda: plugin.shell(self.c), "", "", "o"),
                    ("Logs", lambda: plugin.logs(self.c), "", "", "Enter"),
                    ("Stop", lambda: plugin.do(self.c, "stop"), "danger", "", "x")]
        else:
            btns = [("Start", lambda: plugin.do(self.c, "start"), "ok", "", "x"),
                    ("Logs", lambda: plugin.logs(self.c), "", "", "Enter"),
                    ("Remove", lambda: plugin.remove(self.c), "danger", "", "d d")]
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=8,
                           max_children_per_line=4, homogeneous=False)
        flow.add_css_class("chips")
        for b in btns:
            flow.append(ui.button(*b))
        self.root = ui.vbox(head, self.meta, self.kv, self.logs, flow, spacing=10)
        self.update(c)

    def update(self, c):
        self.c = c
        p = self.p
        set_text(self.name, c["Names"])
        ports = c.get("Ports", "").replace("0.0.0.0:", ":").replace("127.0.0.1:", ":").replace("->", " → ")
        set_text(self.meta, " · ".join(x for x in (c["Image"], ports.split(", ")[0] if ports else "") if x))
        set_text(self.vals["Status"], c.get("Status", ""))
        st = p.stats.get(c["ID"][:12])
        if "CPU" in self.vals:
            cpu = st.get("cpu") if st else None
            set_text(self.vals["CPU"], f"{cpu:.2f}%" if cpu is not None else "…")
            set_text(self.vals["Memory"], f"{mem_text(st['mem'])} / {mem_text(st['limit'])}" if st else "…")
        if "Ports" in self.vals:
            set_text(self.vals["Ports"], ports.replace(", ", "\n"))
        text = p.logs_text.get(c["ID"])
        if text is not None:
            self.logs_set = True
            if text:
                set_text(self.logs, p.color_logs(text), markup=True)
            else:
                set_text(self.logs, "(no logs)")


class Plugin(Base):
    name, title, icon, color = "patra", "Docker", "", "blue"
    placeholder = "Filter containers…"
    preview = True
    preview_width = 340
    # Vyom's own live refresh (`live = 3`) runs search() again and rebuilds
    # the list's rows and the whole preview every time: the right side
    # flickered every 3 s. patra reads on its own timer and changes on
    # screen only what changed (see tick()).
    live = 0
    hints = [Hint("Enter", "logs"), Hint("x", "stop/start"), Hint("R", "restart"), Hint("o", "shell"),
             Hint("d d", "remove")]
    modal = True                # vim keys (Vyom 0.7): NORMAL first, / filters
    start_mode = "normal"
    key_groups = vim.GROUPS
    empty_text = "No containers"

    def __init__(self, vyom, conf):
        super().__init__(vyom, conf)
        self.stats = {}           # id12 -> {"cpu": % or None, "mem": bytes, "limit": bytes}
        self.snap = None          # the last reading: {"up", "api", "conts", "at"}
        self.api = True           # Docker's socket answers (else the docker command)
        self.engine_stats = engine.Stats()
        self.hist = {}            # id12 -> deque of (cpu %, memory bytes)
        self.restarts = {}        # id12 -> deaths in the last 10 minutes
        self.events_at = 0.0
        self.counts = None        # (running, trouble, stopped) for the strip
        self.only = ""            # Space f: "", "running", "stopped" or "trouble"
        self.sort_by = ""         # Space s: "", "cpu", "mem" or "name"
        self.timer = 0            # the next reading's GLib timer
        self.gen = 0              # bumped on close / park: a reading that lands after it is dropped
        self.busy = False         # a reading runs
        self.again = False        # read again as soon as it lands (an action happened)
        self.reads = 0
        self.want = None          # the container to select again after a rebuild of the list
        self.details = collections.OrderedDict()   # ID -> Detail (the last few previews)
        self.logs_text = {}       # ID -> its last log lines (text)
        self.logs_busy = set()
        self.colors = {"red": "#f38ba8", "yellow": "#f9e2af", "green": "#a6e3a1", "dim": "#6c7086"}
        self.terminal = self.conf.get("terminal") or os.environ.get("TERMINAL") or \
            next((t for t in ("kitty", "foot", "alacritty", "wezterm") if shutil.which(t)), "xterm")

    # ------------------------------------------------------------ lifecycle
    def opened(self, args):
        super().opened(args)
        self.colors = {"red": theme_hex("red", "#f38ba8"), "yellow": theme_hex("yellow", "#f9e2af"),
                       "green": theme_hex("green", "#a6e3a1"), "dim": theme_hex("gray", "#7f849c")}
        self.details.clear()
        self.start_ticking(0)

    def closed(self):
        self.stop_ticking()
        self.details.clear()      # the widgets go with the panel's rows; the readings stay
        self.logs_text.clear()

    def parked(self):
        """Out of sight (another workspace): no readings until it shows again."""
        self.stop_ticking()

    def unparked(self):
        self.start_ticking(0)

    def showing(self):
        p = self.panel
        return p is not None and p.get_visible() and not getattr(p, "parked", False)

    def start_ticking(self, delay):
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(int(delay * 1000), self.tick)

    def stop_ticking(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.gen += 1             # what's being read now is dropped when it lands
        self.busy = False
        self.again = False

    def kick(self):
        """After an action: the list again at once (one request, or `docker
        ps`), without waiting for the stats; then a full reading."""
        if not self.showing():
            return
        gen = self.gen
        self.background(lambda: self.read(), lambda snap: self.landed_list(gen, snap))
        if self.busy:
            self.again = True
        else:
            self.start_ticking(TICK)

    def landed_list(self, gen, snap):
        if gen != self.gen or not isinstance(snap, dict):
            return
        self.take(snap)
        self.sync()

    # ------------------------------------------------------------ reading
    def tick(self):
        """One reading in a worker thread: whether Docker runs, the list,
        the running containers' stats, the preview's logs, now and then the
        restarts; all over one kept-alive connection to the socket. The
        next one is timed from when this one lands, so two never overlap."""
        self.timer = 0
        if not self.showing() or self.busy:
            return False
        self.busy = True
        self.reads += 1
        gen, logs_for, n = self.gen, self.preview_id(), self.reads
        seen = None if self.sort_by in ("cpu", "mem") or n % AWAY_EVERY == 1 else self.on_screen()

        def work():
            snap = None
            try:
                snap = self.read(stats=True, logs_for=logs_for, n=n, seen=seen)
            except Exception:   # a bug in a reading mustn't stop the next one)
                logging.getLogger("vyom.patra").exception("reading failed")
            GLib.idle_add(self.landed, gen, snap, priority=GLib.PRIORITY_DEFAULT)
        threading.Thread(target=work, daemon=True).start()
        return False

    def on_screen(self):
        """The IDs of the rows in sight (and the selected one), or None when
        that can't be told: those are read every time, the others now and
        then (AWAY_EVERY)."""
        p = self.panel
        try:
            adj = p.scroll.get_vadjustment()
            top, bottom = adj.get_value(), adj.get_value() + adj.get_page_size()
            seen = set()
            for r, w in p.items:
                res = w.translate_coordinates(p.list, 0, 0)
                if not res or (len(res) == 3 and not res[0]):
                    continue
                y = res[-1]
                if y + w.get_height() >= top and y <= bottom:
                    seen.add((r.data or {}).get("ID"))
            sel = self.preview_id()
            if sel:
                seen.add(sel)
            return seen
        except (AttributeError, TypeError, IndexError):
            return None

    def read(self, stats=False, logs_for=None, n=0, seen=None):
        """A reading (worker thread): {"up", "api", "conts", "stats", "restarts", "logs", "t0", "at"}."""
        now = time.monotonic()
        if not daemon_up():
            return {"up": False, "api": self.api, "conts": [], "t0": now, "at": now}
        with engine.Session(5) as s:
            conts = engine.containers(s)
            api = conts is not None
            if not api and engine.refused(s):
                return {"up": False, "api": self.api, "conts": [], "t0": now, "at": time.monotonic()}
            if not api:           # no access to Docker's socket: the commands
                conts = cli_ps()
            for c in conts:
                c["health"] = health(c)
            snap = {"up": True, "api": api, "conts": conts, "t0": now, "at": now}
            if stats:
                running = [c["ID"][:12] for c in conts if c.get("State") == "running"]
                ids = running if seen is None else [cid for cid in running if cid in seen]
                snap["stats"] = self.engine_stats.read(ids, s, running) if api else cli_stats()
                snap["running"] = running
                snap["soon"] = api and self.engine_stats.needs_second(ids)
                if time.time() - self.events_at > EVENTS_EVERY:
                    self.events_at = time.time()
                    snap["restarts"] = (engine.restarts(10, s) if api else None) or ({} if api else cli_restarts())
                if logs_for and (api or n % CLI_LOGS_EVERY == 1):
                    snap["logs"] = (logs_for, engine.logs(logs_for, 14, s) if api else cli_logs(logs_for))
        snap["at"] = time.monotonic()    # when it was read to the end (the commands take a second or two)
        return snap

    def landed(self, gen, snap):
        if gen != self.gen:       # closed or parked meanwhile
            return False
        self.busy = False
        if snap is not None:
            self.take(snap)
            self.sync()
        if self.showing() and not self.timer:
            self.start_ticking(0 if self.again else SOON if snap and snap.get("soon") else TICK)
        self.again = False
        return False

    def take(self, snap):
        """A reading's numbers into the history (main thread). A list read
        before the one shown (a reading that started before an action) is
        older news: its stats count, its list doesn't."""
        if self.snap is None or snap["t0"] >= self.snap.get("t0", 0):
            self.snap = snap
            self.api = snap.get("api", self.api)
        if "restarts" in snap:
            self.restarts = snap["restarts"]
        if "stats" in snap:
            st = snap["stats"]
            for cid, v in st.items():
                if v.get("cpu") is not None:
                    self.hist.setdefault(cid, collections.deque(maxlen=HISTORY)).append((v["cpu"], v["mem"]))
            running = set(snap.get("running", st))
            for cid in [c for c in self.hist if c not in running]:
                del self.hist[cid]
            self.stats = {cid: st.get(cid, self.stats.get(cid)) for cid in running
                          if st.get(cid, self.stats.get(cid)) is not None}
        if snap.get("logs") and snap["logs"][1] is not None:
            self.logs_text[snap["logs"][0]] = snap["logs"][1]

    def search(self, q):
        snap = self.snap
        if snap is None or time.monotonic() - snap.get("at", 0) > FRESH:
            snap = self.read()    # the list only (a few ms); the stats come with the next reading
            self.snap = snap
        rows, self.counts = self.rows(snap, q)
        return rows

    # ------------------------------------------------------------ the rows
    def rows(self, snap, q):
        """(the rows for this reading and filter, the strip's counts). No widgets: also runs in search()'s thread."""
        if not snap["up"]:
            return [Row("Docker isn't running", "Enter starts it (docker.service)", icon=STATES["stopped"][2], color="gray",
                        data={"kind": "start"})], None
        conts = snap["conts"]
        counts = (sum(c["State"] == "running" and c["health"] not in TROUBLE for c in conts),
                  sum(c["health"] in TROUBLE for c in conts),
                  sum(c["health"] in ("stopped", "paused") for c in conts))
        groups = {}
        for c in conts:
            if q and util.fuzzy(q, c["Names"] + " " + c["Image"]) is None:
                continue
            if self.only and not self.shown(c):
                continue
            labels = dict(kv.split("=", 1) for kv in c.get("Labels", "").split(",") if "=" in kv)
            groups.setdefault(labels.get("com.docker.compose.project", ""), []).append(c)
        stats = self.stats

        def order(c):   # the ones that need you, then running, then stopped (or Space s's order)
            st = stats.get(c["ID"][:12], {})
            if self.sort_by == "cpu":
                return (-(st.get("cpu") or 0.0), c["Names"])
            if self.sort_by == "mem":
                return (-st.get("mem", 0.0), c["Names"])
            if self.sort_by == "name":
                return (c["Names"].lower(),)
            return (0 if c["health"] in TROUBLE else 1 if c["State"] == "running" else 2, c["Names"])

        def trouble(p):
            return any(c["health"] in TROUBLE for c in groups[p])
        rows = []
        for proj in sorted(groups, key=lambda p: (not trouble(p), p == "", p)):
            items = sorted(groups[proj], key=order)
            up = sum(c["State"] == "running" for c in items)
            note = "needs you" if trouble(proj) else "all running" if up == len(items) else \
                "stopped" if up == 0 else f"{up} of {len(items)} running"
            if self.only or self.sort_by:
                note += " · " + " · ".join(x for x in (self.only and f"only {self.only}",
                                                      self.sort_by and f"by {self.sort_by}") if x)
            rows.append(Section(f"{proj} · compose" if proj else "Containers", len(items), note))
            rows += [self.row(c) for c in items]
        return rows, counts

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

    @staticmethod
    def skeleton(rows):
        """What a row looks like apart from its changing words: two lists with
        the same skeleton are updated in place, never rebuilt."""
        out = []
        for r in rows:
            if isinstance(r, Section):
                out.append(("s", r.title, r.count, r.note))
            else:
                d = r.data or {}
                out.append(("r", r.title, r.icon, r.color, r.dim, d.get("kind"), d.get("ID"), d.get("State"),
                            d.get("health"), d.get("Names")))
        return out

    def sync(self):
        """A reading onto the screen: when the list has the same rows, only
        the words, figures and sparklines that changed; else the list again
        (the selected container stays selected)."""
        p = self.panel
        if p is None or self.snap is None or not self.showing() or getattr(p, "in_flight", False):
            return
        rows, counts = self.rows(self.snap, p.query())
        shown = getattr(p, "shown", None)
        if shown is None or len(shown) != len(rows) or self.skeleton(shown) != self.skeleton(rows):
            sel = p.selected_row()
            if sel is not None and (sel.data or {}).get("kind") == "c":
                self.want = sel.data["ID"]
            if hasattr(p, "refresh_now"):
                p.refresh_now(keep_selection=True)
            else:
                self.refresh()
            return
        built = {(r.data or {}).get("ID"): (r, w) for r, w in getattr(p, "items", [])}
        for old, new in zip(shown, rows):
            if isinstance(new, Section) or (new.data or {}).get("kind") != "c":
                continue
            old.data.update(new.data)      # the shown row and the built one share this dict
            if old.subtitle != new.subtitle:
                old.subtitle = new.subtitle
                r, w = built.get(new.data["ID"], (None, None))
                if r is not None:
                    r.subtitle = new.subtitle
                    sub = self.subtitle_label(w)
                    if sub is not None:
                        set_text(sub, new.subtitle, markup=True)
        self.add_sparks()
        if counts != self.counts:
            self.counts = counts
            p.refresh_strip()
        sel = p.selected_row()
        d = self.details.get((sel.data or {}).get("ID")) if sel is not None else None
        if d is not None and d.root.get_parent() is not None:
            d.update(sel.data)

    @staticmethod
    def subtitle_label(w):
        """The subtitle label in one of Vyom's row widgets (row > text box > title box, subtitle)."""
        box = w.get_child() if hasattr(w, "get_child") else None
        text = box.get_first_child().get_next_sibling() if box is not None else None
        sub = text.get_first_child().get_next_sibling() if text is not None and text.get_first_child() else None
        return sub if isinstance(sub, Gtk.Label) else None

    # ------------------------------------------------------------ the strip
    def build_strip(self):
        """How many run, need you and are stopped; and the sparklines on the
        rows (drawn here: the panel calls this right after it built them)."""
        self.add_sparks()
        if self.want is not None:   # a rebuild patra asked for: the same container stays selected
            want, self.want = self.want, None
            GLib.idle_add(self.reselect, want, priority=GLib.PRIORITY_HIGH)
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

    def reselect(self, cid):
        p = self.panel
        if p is None:
            return False
        sel = p.selected_row()
        if sel is not None and (sel.data or {}).get("ID") == cid:
            return False
        i = next((i for i, (r, _) in enumerate(getattr(p, "items", [])) if (r.data or {}).get("ID") == cid), -1)
        if i >= 0:
            p.select(i)
        return False

    def add_sparks(self):
        """Sparklines on the running rows: made once per row widget, then updated."""
        for r, w in getattr(self.panel, "items", []):
            c = r.data or {}
            if c.get("kind") != "c" or c.get("State") != "running":
                continue
            box = w.get_child() if hasattr(w, "get_child") else None
            text = box.get_first_child().get_next_sibling() if box is not None else None
            if text is None:
                continue
            sp = getattr(box, "_patra_spark", None)
            if sp is None:
                sp = box._patra_spark = Sparks()
                box.insert_child_after(sp, text)
            cid = c["ID"][:12]
            sp.update(self.hist.get(cid, ()), self.stats.get(cid), "red" if c["health"] in TROUBLE else "green")

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

    def preview_id(self):
        p = self.panel
        sel = p.selected_row() if p is not None else None
        c = (sel.data or {}) if sel is not None else {}
        return c.get("ID") if c.get("kind") == "c" else None

    def build_preview(self, row):
        """The selected container's preview. The same container gives back
        the same widgets (updated), so a refresh changes nothing on screen
        but the figures that changed."""
        c = row.data or {}
        if c.get("kind") != "c":
            return ui.vbox(ui.title("Docker"), ui.body("The daemon is stopped. Enter starts it."))
        d = self.details.get(c["ID"])
        if d is None or d.key != (c["State"], c["health"]):
            d = self.details[c["ID"]] = Detail(self, c)
            while len(self.details) > 4:
                self.details.popitem(last=False)
        else:
            d.update(c)
        self.details.move_to_end(c["ID"])
        parent = d.root.get_parent()
        if parent is not None:
            parent.remove(d.root)
        if c["ID"] not in self.logs_text:
            self.fetch_logs(c["ID"])
        return d.root

    def fetch_logs(self, cid):
        """The preview's log lines now (one request), for a container just selected."""
        if cid in self.logs_busy:
            return
        self.logs_busy.add(cid)
        api = self.api

        def work():
            text = engine.logs(cid, 14) if api else None
            return cli_logs(cid) if text is None else text

        def got(text):
            self.logs_busy.discard(cid)
            if isinstance(text, Exception):
                return
            self.logs_text[cid] = text or ""
            d = self.details.get(cid)
            if d is not None:
                d.update(d.c)
        self.background(work, got)

    # ------------------------------------------------------------ actions
    def do(self, c, verb):
        self.set_status(f"{verb} {c['Names']}…")
        api = self.api

        def work():
            r = engine.action(c["ID"], verb) if api else None
            if r is None:
                code, _ = util.run(["docker", verb, c["ID"]], timeout=60)
                r = (code == 0, "")
            return r

        def done(r):
            ok = isinstance(r, tuple) and r[0]
            self.set_status("" if ok else f"{verb} failed" + (f": {r[1]}" if isinstance(r, tuple) and r[1] else ""),
                            None if ok else "bad")
            self.logs_text.pop(c["ID"], None)
            self.kick()
        self.background(work, done)

    def remove(self, c):
        """Asks first (y removes, any other key keeps it): it can't be undone."""
        if c["State"] == "running":
            self.set_status("stop it first (x)", "bad")
            return
        self.confirm(f"Remove {c['Names']}? It can't be undone.", lambda: self.do(c, "rm"))

    def term(self, cmd, title):
        if os.path.basename(self.terminal) == "kitty":
            util.spawn(["kitty", "--title", title, "-e", *cmd])
        else:
            util.spawn([self.terminal, "-e", *cmd])

    def shell(self, c):
        if c["State"] != "running":
            self.set_status("start it first (x)", "bad")
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
            self.kick()
            return False
        if c.get("kind") == "c":
            self.logs(c)
        return False

    # ------------------------------------------------------------ vim keys
    def keys(self, tab):
        return vim.keys(tab)

    def selected(self):
        """The selected container, or None (with a word why)."""
        row = self.panel.selected_row() if self.panel else None
        c = (row.data or {}) if row is not None else {}
        if c.get("kind") != "c":
            self.set_status("no container selected", "bad")
            return None
        return c

    def shown(self, c):
        if self.only == "running":
            return c["State"] == "running"
        if self.only == "stopped":
            return c["State"] != "running"
        if self.only == "trouble":
            return c["health"] in TROUBLE
        return True

    def v_logs(self, _n, _a):
        if (c := self.selected()):
            self.logs(c)

    def v_shell(self, _n, _a):
        if (c := self.selected()):
            self.shell(c)

    def v_toggle(self, _n, _a):
        if (c := self.selected()):
            self.do(c, "stop" if c["State"] == "running" else "start")

    def v_restart(self, _n, _a):
        if (c := self.selected()):
            self.do(c, "restart")

    def v_remove(self, _n, _a):
        if (c := self.selected()):
            self.remove(c)

    def v_copy(self, _n, what):
        if not (c := self.selected()):
            return
        text = {"id": c["ID"], "ports": c.get("Ports", "")}.get(what, c["Names"])
        if not text:
            self.set_status("nothing to copy", "bad")
            return
        util.copy(text)
        self.set_status(f"copied {text[:40]}", "ok")

    def keep_selection(self):
        """The selected container stays selected through the next rebuild."""
        c = self.panel.selected_row() if self.panel else None
        if c is not None and (c.data or {}).get("kind") == "c":
            self.want = c.data["ID"]

    def v_only(self, _n, what):
        self.keep_selection()
        self.only = "" if what == self.only else what
        self.set_status(f"only {self.only}" if self.only else "every container", "ok")
        self.refresh()

    def v_sort(self, _n, what):
        self.keep_selection()
        self.sort_by = "" if what == self.sort_by else what
        self.set_status(f"sorted by {self.sort_by}" if self.sort_by else "the usual order", "ok")
        self.refresh()
