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
    with Session(timeout) as s:
        return s.get(path, raw=raw)


class Session:
    """Several requests over one kept-alive connection (one refresh's list,
    stats and logs: no new socket per request). Not thread-safe: one per
    worker. A request that fails on a reused connection is tried once more
    on a new one (the daemon may have closed an idle one)."""

    def __init__(self, timeout=5):
        self.timeout, self.conn, self.requests = timeout, None, 0
        self.error = None   # the last OSError (PermissionError: no access to the socket)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def request(self, method, path, timeout=None):
        """(status, body bytes), or None when the socket can't be used."""
        for attempt in (0, 1):
            fresh = self.conn is None
            if fresh:
                self.conn = _Unix(sock_path(), timeout or self.timeout)
            elif timeout:
                self.conn.timeout = timeout
                if self.conn.sock is not None:
                    self.conn.sock.settimeout(timeout)
            try:
                self.requests += 1
                self.conn.request(method, path, headers={"Host": "docker"})
                r = self.conn.getresponse()
                body = r.read()
                if r.will_close:
                    self.close()
                return r.status, body
            except (OSError, http.client.HTTPException) as e:
                self.error = e
                self.close()
                if fresh or attempt:
                    return None
        return None

    def get(self, path, raw=False, timeout=None):
        r = self.request("GET", path, timeout)
        if r is None or r[0] >= 300:
            return None
        try:
            return r[1].decode("utf-8", "replace") if raw else json.loads(r[1] or b"null")
        except ValueError:
            return None


_UNIT = {}   # the docker.service object path on systemd's bus, asked once


def up():
    """Is docker.service running? From systemd over the system bus (no
    command, never starts it). DOCKER_HOST naming another socket isn't
    docker.service's, and without systemd there's no socket activation:
    then it's whether the socket is there (the list's request finds out
    whether it answers: no extra ping)."""
    if sock_path() == SOCKET:
        try:
            from gi.repository import Gio, GLib
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

            def call(path, iface, method, args, sig):
                return bus.call_sync("org.freedesktop.systemd1", path, iface, method, args, GLib.VariantType(sig),
                                     Gio.DBusCallFlags.NO_AUTO_START, 2000, None).unpack()

            def prop(path, name):
                return call(path, "org.freedesktop.DBus.Properties", "Get",
                            GLib.Variant("(ss)", ("org.freedesktop.systemd1.Unit", name)), "(v)")[0]
            path = _UNIT.get("path")
            if path is None:
                path = call("/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "LoadUnit",
                            GLib.Variant("(s)", ("docker.service",)), "(o)")[0]
                if prop(path, "LoadState") != "loaded":
                    raise LookupError("no docker.service")
                _UNIT["path"] = path
            return prop(path, "ActiveState") in ("active", "reloading")
        except Exception:   # noqa: BLE001  (no systemd, no bus, no unit: look at the socket)
            pass
    return os.path.exists(sock_path())


def refused(session):
    """The socket is there but nobody answers (a stopped daemon's leftover),
    rather than one this user may not use."""
    return session.error is not None and not isinstance(session.error, PermissionError)


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


def containers(session=None):
    """Every container like `docker ps -a --format '{{json .}}'` gives them
    (ID, Names, Image, State, Status, Ports, Labels as text), or None when
    Docker can't be asked."""
    rows = session.get("/containers/json?all=1") if session else get("/containers/json?all=1")
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
    """CPU and memory of the running containers: one one-shot reading per
    container per call (the daemon answers at once from the cgroup, no
    second-long wait), the CPU worked out from this reading and the one
    before. Docker has no call for several containers' stats at once, so
    the calls share one kept-alive connection. A container without a
    recent reading has no CPU figure yet (None) until the next call."""
    FRESH = 30      # seconds a previous reading stays good for the CPU figure

    def __init__(self):
        self.prev = {}   # id12 -> (monotonic time, raw reading)

    def read(self, ids, session=None, running=None):
        """{id12: {"cpu": percent or None, "mem": bytes, "limit": bytes}} for
        these ids; the earlier readings of `running` (default: ids) are kept."""
        own = session is None
        session = session or Session(5)
        out = {}
        try:
            for cid in ids:
                now = time.monotonic()
                p = self.prev.get(cid)
                fresh = p is not None and now - p[0] < self.FRESH
                st = session.get(f"/containers/{urllib.parse.quote(cid)}/stats?stream=false&one-shot=true")
                if not st or not st.get("cpu_stats"):
                    continue
                mem, limit = _mem(st)
                out[cid] = {"cpu": _cpu_pct(st, p[1]) if fresh else None, "mem": float(mem), "limit": float(limit)}
                self.prev[cid] = (now, st)
        finally:
            if own:
                session.close()
        keep = set(ids if running is None else running)
        for cid in [c for c in self.prev if c not in keep]:
            del self.prev[cid]
        return out

    def needs_second(self, ids):
        """Containers read for the first time (no CPU yet): read again soon."""
        return any(cid not in self.prev for cid in ids)


def restarts(minutes=10, session=None):
    """{id12: deaths in the last `minutes`} (a restart loop), from the
    daemon's events (a bounded query: since..until, not a stream)."""
    now = int(time.time())
    filters = urllib.parse.quote(json.dumps({"type": ["container"], "event": ["die"]}))
    path = f"/events?since={now - minutes * 60}&until={now}&filters={filters}"
    text = session.get(path, raw=True, timeout=6) if session else get(path, timeout=6, raw=True)
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


# ------------------------------------------------------------------ logs, actions
def demux(body):
    """A logs answer as text: a container without a TTY sends frames (one
    byte stream type, three zero bytes, a 4-byte length, the payload);
    one with a TTY sends the text as is."""
    if len(body) >= 8 and body[0] in (0, 1, 2) and body[1:4] == b"\0\0\0":
        out, i = [], 0
        while i + 8 <= len(body):
            n = int.from_bytes(body[i + 4:i + 8], "big")
            out.append(body[i + 8:i + 8 + n])
            i += 8 + n
        body = b"".join(out)
    return body.decode("utf-8", "replace")


def logs(cid, tail=14, session=None):
    """The last `tail` log lines with Docker's timestamps, or None."""
    path = f"/containers/{urllib.parse.quote(cid)}/logs?stdout=1&stderr=1&timestamps=1&tail={int(tail)}"
    own = session is None
    session = session or Session(6)
    try:
        r = session.request("GET", path)
    finally:
        if own:
            session.close()
    return demux(r[1]) if r is not None and r[0] < 300 else None


def action(cid, verb):
    """start / stop / restart / rm a container: (ok, Docker's message), or
    None when the socket can't be used (then the command)."""
    q = urllib.parse.quote(cid)
    method, path = ("DELETE", f"/containers/{q}") if verb == "rm" else ("POST", f"/containers/{q}/{verb}")
    with Session(70) as s:
        r = s.request(method, path)
    if r is None:
        return None
    if r[0] < 300 or r[0] == 304:     # 304: already started / stopped
        return True, ""
    try:
        msg = json.loads(r[1]).get("message", "")
    except (ValueError, AttributeError):
        msg = ""
    return False, msg or f"HTTP {r[0]}"
