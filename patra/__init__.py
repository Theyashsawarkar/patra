"""patra (पात्र, "vessel"): Docker containers for Vyom.

Every container, grouped by compose project, running ones first, with
live CPU and memory and its ports; the preview shows the last log lines.
Never wakes a stopped Docker daemon (it's often socket-activated): it
says Docker isn't running instead.
"""
import json
import os
import shutil
import threading
import time

from vyom import Hint, Row, Section, ui, util
from vyom import Plugin as Base

# keybind: Vyom/docker | Enter | Follow the selected container's logs in a terminal
# keybind: Vyom/docker | Ctrl+R | Restart the selected container
# keybind: Vyom/docker | Ctrl+S | Stop or start the selected container
# keybind: Vyom/docker | Ctrl+T | Open a shell in the selected container
# keybind: Vyom/docker | Ctrl+Backspace | Remove the selected stopped container


def daemon_up():
    if shutil.which("systemctl"):
        return util.run(["systemctl", "is-active", "--quiet", "docker.service"])[0] == 0
    return util.run(["docker", "info", "--format", "x"], timeout=3)[0] == 0


class Plugin(Base):
    name, title, icon, color = "patra", "Docker", "", "blue"
    placeholder = "Filter containers…"
    preview = True
    preview_width = 330
    width = 780
    live = 3
    hints = [Hint("↵", "logs"), Hint("⌃R", "restart"), Hint("⌃S", "stop/start"), Hint("⌃T", "shell")]
    empty_text = "No containers"

    def __init__(self, vyom, conf):
        super().__init__(vyom, conf)
        self.stats = {}
        self.stats_busy = False
        self.terminal = self.conf.get("terminal") or os.environ.get("TERMINAL") or \
            next((t for t in ("kitty", "foot", "alacritty", "wezterm") if shutil.which(t)), "xterm")

    def update_stats(self):
        """docker stats takes ~1 s: run it on the side, show the last result."""
        if self.stats_busy:
            return
        self.stats_busy = True

        def go():
            code, out = util.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=10)
            st = {}
            for line in out.splitlines() if code == 0 else []:
                try:
                    j = json.loads(line)
                    st[j["ID"][:12]] = j
                except (ValueError, KeyError):
                    pass
            self.stats = st
            self.stats_busy = False
        threading.Thread(target=go, daemon=True).start()

    def search(self, q):
        if not daemon_up():
            return [Row("Docker isn't running", "Enter starts it (docker.service)", icon="", color="gray",
                        data={"kind": "start"})]
        self.update_stats()
        code, out = util.run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=8)
        conts = []
        for line in out.splitlines() if code == 0 else []:
            try:
                conts.append(json.loads(line))
            except ValueError:
                pass
        groups = {}
        for c in conts:
            if q and util.fuzzy(q, c["Names"] + " " + c["Image"]) is None:
                continue
            labels = dict(kv.split("=", 1) for kv in c.get("Labels", "").split(",") if "=" in kv)
            groups.setdefault(labels.get("com.docker.compose.project", ""), []).append(c)
        rows = []
        for proj in sorted(groups, key=lambda p: (p == "", p)):
            items = sorted(groups[proj], key=lambda c: (c["State"] != "running", c["Names"]))
            rows.append(Section(f"{proj} (compose)" if proj else "Containers"))
            rows += [self.row(c) for c in items]
        return rows

    def row(self, c):
        running = c["State"] == "running"
        failed = c["State"] == "exited" and "Exited (0)" not in c["Status"]
        st = self.stats.get(c["ID"][:12], {})
        meta = f"{st.get('CPUPerc', '')} · {st.get('MemUsage', '').split(' / ')[0]}" if running and st else ""
        ports = c.get("Ports", "").replace("0.0.0.0:", "").replace("127.0.0.1:", "").replace("->", "→")
        sub = " · ".join(x for x in (c["Image"], c["Status"], ports.split(",")[0] if ports else "") if x)
        return Row(c["Names"], sub, icon="" if running else "\U000f0156" if failed else "\U000f03e4",
                   color="green" if running else "red" if failed else "gray", meta=meta, dim=not running and not failed,
                   badge="failed" if failed else "", badge_color="red", data={"kind": "c", **c})

    def build_preview(self, row):
        c = row.data or {}
        if c.get("kind") != "c":
            return ui.vbox(ui.title("Docker"), ui.body("The daemon is stopped. Enter starts it."))
        st = self.stats.get(c["ID"][:12], {})
        parts = [ui.title(c["Names"]), ui.meta(f"{c['Image']} · {c['Status']}")]
        facts = [("ID", c["ID"][:12])]
        if st:
            facts += [("CPU", st.get("CPUPerc", "")), ("Memory", st.get("MemUsage", "")), ("Net", st.get("NetIO", ""))]
        if c.get("Ports"):
            facts.append(("Ports", c["Ports"].replace(", ", "\n")))
        parts.append(ui.kv(facts))
        logs = ui.mono("loading logs…")
        parts.append(logs)

        def got(r):
            if isinstance(r, tuple):
                logs.set_label(r[1][-1800:] or "(no logs)")
        self.background(lambda: util.run(f"docker logs --tail 14 {c['ID']} 2>&1", timeout=6), got)
        running = c["State"] == "running"
        parts.append(ui.buttons([("Restart", lambda: self.do(c, "restart")),
                                 ("Stop" if running else "Start", lambda: self.do(c, "stop" if running else "start"),
                                  "danger" if running else "primary"),
                                 ("Shell", lambda: self.shell(c))]))
        return ui.vbox(*parts, spacing=10)

    def do(self, c, verb):
        self.set_status(f"{verb} {c['Names']}…")
        self.background(lambda: util.run(["docker", verb, c["ID"]], timeout=60),
                        lambda r: (self.set_status("" if isinstance(r, tuple) and r[0] == 0
                                                   else f"{verb} failed"), self.refresh()))

    def term(self, cmd, title):
        if os.path.basename(self.terminal) == "kitty":
            util.spawn(["kitty", "--title", title, "-e", *cmd])
        else:
            util.spawn([self.terminal, "-e", *cmd])

    def shell(self, c):
        self.term(["docker", "exec", "-it", c["ID"], "sh", "-c", "command -v bash >/dev/null && exec bash || exec sh"],
                  f"{c['Names']} shell")
        self.close()

    def activate(self, row, mods):
        c = row.data or {}
        if c.get("kind") == "start":
            util.spawn(["docker", "info"])  # socket activation starts the daemon
            self.set_status("starting Docker…")
            return False
        if c.get("kind") == "c":
            self.term(["docker", "logs", "-f", "--tail", "200", c["ID"]], f"{c['Names']} logs")
            return True
        return False

    def key(self, name, row, mods):
        if "ctrl" not in mods or row is None or (row.data or {}).get("kind") != "c":
            return False
        c = row.data
        if name == "r":
            self.do(c, "restart")
        elif name == "s":
            self.do(c, "stop" if c["State"] == "running" else "start")
        elif name == "t":
            self.shell(c)
        elif name == "BackSpace" and c["State"] != "running":
            self.do(c, "rm")
        else:
            return False
        return True
