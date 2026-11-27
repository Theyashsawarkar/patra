"""Docker's Engine API over its own socket, in this process: the list, the
stats and the restarts without starting a `docker` command (each one a Go
program: 40-80 ms of CPU, and `docker stats --no-stream` waits a second for
its second reading). No GTK here (the unit tests run without it).

Never wakes a stopped daemon: Docker is often socket-activated
(docker.socket), and connecting to the socket starts it. up() asks systemd
(D-Bus, no command) whether docker.service runs before anything connects.
DOCKER_HOST (unix://...) points it at another socket.
"""
import http.client
import json
import os
import socket
import time
import urllib.parse

SOCKET = "/var/run/docker.sock"


def sock_path():
    host = os.environ.get("DOCKER_HOST", "")
    return host[len("unix://"):] if host.startswith("unix://") else SOCKET


class _Unix(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.unix = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix)
        self.sock = s


def get(path, timeout=5, raw=False):
    """GET path from the Engine API: its JSON (or text with raw), or None."""
    c = _Unix(sock_path(), timeout)
    try:
        c.request("GET", path, headers={"Host": "docker"})
        r = c.getresponse()
        body = r.read()
        if r.status >= 300:
            return None
        return body.decode("utf-8", "replace") if raw else json.loads(body or b"null")
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        c.close()


def up():
    """Is docker.service running? From systemd over the system bus (no
    command, never starts it). True when systemd can't be asked but the
    socket is there and answers (no systemd: no socket activation either)."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

        def call(path, iface, method, args, sig):
            return bus.call_sync("org.freedesktop.systemd1", path, iface, method, args, GLib.VariantType(sig),
                                 Gio.DBusCallFlags.NO_AUTO_START, 2000, None).unpack()
        path = call("/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "LoadUnit",
                    GLib.Variant("(s)", ("docker.service",)), "(o)")[0]
        state = call(path, "org.freedesktop.DBus.Properties", "Get",
                     GLib.Variant("(ss)", ("org.freedesktop.systemd1.Unit", "ActiveState")), "(v)")[0]
        if call(path, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("org.freedesktop.systemd1.Unit", "LoadState")), "(v)")[0] == "loaded":
            return state in ("active", "reloading")
    except Exception:   # noqa: BLE001  (no systemd, no bus: look at the socket)
        pass
    return os.path.exists(sock_path()) and get("/_ping", timeout=2, raw=True) == "OK"


# ------------------------------------------------------------------ the list
def _ports(ports):
    """The API's port list as `docker ps` writes it: "0.0.0.0:8080->80/tcp, 5432/tcp"."""
    out = []
    for p in sorted(ports or (), key=lambda p: (p.get("PrivatePort", 0), p.get("IP", ""))):
        inside = f"{p.get('PrivatePort')}/{p.get('Type', 'tcp')}"
        if p.get("PublicPort"):
            ip = p.get("IP") or "0.0.0.0"
            ip = f"[{ip}]" if ":" in ip else ip
            out.append(f"{ip}:{p['PublicPort']}->{inside}")
        else:
            out.append(inside)
    return ", ".join(dict.fromkeys(out))


def containers():
    """Every container like `docker ps -a --format '{{json .}}'` gives them
    (ID, Names, Image, State, Status, Ports, Labels as text), or None when
    Docker can't be asked."""
    rows = get("/containers/json?all=1")
    if rows is None:
        return None
    out = []
    for c in rows:
        labels = c.get("Labels") or {}
        out.append({"ID": (c.get("Id") or "")[:12], "Names": ",".join(n.lstrip("/") for n in c.get("Names") or ()),
                    "Image": c.get("Image", ""), "State": c.get("State", ""), "Status": c.get("Status", ""),
                    "Ports": _ports(c.get("Ports")), "Labels": ",".join(f"{k}={v}" for k, v in labels.items()),
                    "Command": c.get("Command", ""), "CreatedAt": c.get("Created", 0)})
    return out


# ------------------------------------------------------------------ stats
def _mem(st):
    """Memory in use the way `docker stats` counts it (without the page cache)."""
    m = st.get("memory_stats") or {}
    use = m.get("usage") or 0
    s = m.get("stats") or {}
    cache = s.get("inactive_file") if "inactive_file" in s else s.get("total_inactive_file", 0)
    return max(0, use - (cache or 0)), m.get("limit") or 0


def _cpu_pct(cur, prev):
    """CPU % between two readings (`docker stats`' formula), or None."""
    try:
        cd = cur["cpu_stats"]["cpu_usage"]["total_usage"] - prev["cpu_stats"]["cpu_usage"]["total_usage"]
        sd = cur["cpu_stats"]["system_cpu_usage"] - prev["cpu_stats"]["system_cpu_usage"]
        n = cur["cpu_stats"].get("online_cpus") or len(cur["cpu_stats"]["cpu_usage"].get("percpu_usage") or ()) or 1
    except (KeyError, TypeError):
        return None
    return cd / sd * n * 100.0 if sd > 0 and cd >= 0 else 0.0


class Stats:
    """CPU and memory of the running containers, one reading per container
    per call (one-shot: the daemon answers at once), the CPU worked out from
    this reading and the one before. A container last read long ago (or
    never) is read without one-shot: Docker then takes the second reading
    itself (about a second), so it has a CPU figure at once."""
    FRESH = 30      # seconds a previous reading stays good for the CPU figure

    def __init__(self):
        self.prev = {}   # id12 -> (monotonic time, raw reading)

    def read(self, ids):
        """{id12: {"cpu": percent, "mem": bytes, "limit": bytes}} for these ids."""
        out, now = {}, time.monotonic()
        for cid in ids:
            p = self.prev.get(cid)
            fresh = p is not None and now - p[0] < self.FRESH
            st = get(f"/containers/{urllib.parse.quote(cid)}/stats?stream=false" + ("&one-shot=true" if fresh else ""),
                     timeout=5)
            if not st:
                continue
            cpu = _cpu_pct(st, p[1]) if fresh else _cpu_pct(st, {"cpu_stats": st.get("precpu_stats") or {}})
            mem, limit = _mem(st)
            out[cid] = {"cpu": cpu or 0.0, "mem": float(mem), "limit": float(limit)}
            self.prev[cid] = (time.monotonic(), st)
        for cid in [c for c in self.prev if c not in ids]:
            del self.prev[cid]
        return out


def restarts(minutes=10):
    """{id12: deaths in the last `minutes`} (a restart loop), from the
    daemon's events."""
    now = int(time.time())
    filters = urllib.parse.quote(json.dumps({"type": ["container"], "event": ["die"]}))
    text = get(f"/events?since={now - minutes * 60}&until={now}&filters={filters}", timeout=6, raw=True)
    n = {}
    for line in (text or "").splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        cid = (j.get("id") or (j.get("Actor") or {}).get("ID") or "")[:12]
        if cid:
            n[cid] = n.get(cid, 0) + 1
    return n
