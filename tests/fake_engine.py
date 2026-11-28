"""A fake Docker Engine API on a private unix socket, for the tests and for
trying patra without a real Docker daemon (made-up containers only).

    python3 tests/fake_engine.py SOCKET [--scenario demo|many] [--ctl CTL]

Then DOCKER_HOST=unix://SOCKET points patra at it. CTL (default
SOCKET.ctl) takes one line per connection and answers one JSON line:

    die NAME | start NAME | stop NAME | add NAME [PROJECT]
    daemon-stop | daemon-start          (the socket goes away / comes back)
    counts                              (requests served, by kind, and since when)
    reset                               (counts back to zero)

It speaks HTTP/1.1 with keep-alive, with or without a /v1.xx prefix:
/_ping, /version, /containers/json, /containers/ID/stats (one-shot, or
with Docker's ~1 s wait for a second reading), /containers/ID/logs
(multiplexed frames), /events (since/until, or a stream), and POST
start / stop / restart, DELETE a container.
"""
import argparse
import collections
import http.server
import json
import math
import os
import re
import socket
import socketserver
import struct
import threading
import time
import urllib.parse

GB = 1024 ** 3
MB = 1024 ** 2


def human(sec):
    """Docker's own wording for how long ago ("5 seconds", "About a minute")."""
    sec = int(sec)
    if sec < 1:
        return "Less than a second"
    if sec < 60:
        return f"{sec} seconds"
    if sec < 120:
        return "About a minute"
    if sec < 3600:
        return f"{sec // 60} minutes"
    if sec < 7200:
        return "About an hour"
    if sec < 48 * 3600:
        return f"{sec // 3600} hours"
    return f"{sec // 86400} days"


class Container:
    def __init__(self, n, name, image, project="", cpu=5.0, mem=100, ports=(), health="", running=True,
                 age=3600, code=0):
        self.id = f"{n:04x}c0ffee{n:04x}" + "0" * 48
        self.name, self.image, self.project = name, image, project
        self.cpu, self.mem, self.ports, self.health = cpu, mem, list(ports), health
        self.running, self.code = running, code
        self.started = time.time() - age
        self.finished = time.time() - age
        self.created = int(time.time() - age - 600)
        self.usage = 0          # CPU ns used so far
        self.at = time.time()

    def tick(self, now):
        # a made-up, gently changing load
        load = max(0.0, self.cpu * (1 + 0.5 * math.sin(now / 4 + len(self.name))))
        self.usage += int(load / 100 * (now - self.at) * 1e9)
        self.at = now

    def status(self, now):
        if self.running:
            h = f" ({self.health})" if self.health else ""
            return f"Up {human(now - self.started)}{h}"
        return f"Exited ({self.code}) {human(now - self.finished)} ago"

    def summary(self, now):
        labels = {"com.docker.compose.project": self.project, "com.docker.compose.service": self.name} \
            if self.project else {}
        return {"Id": self.id, "Names": [f"/{self.name}"], "Image": self.image, "ImageID": "sha256:" + "f" * 64,
                "Command": "/entrypoint.sh", "Created": self.created, "State": "running" if self.running else "exited",
                "Status": self.status(now), "Ports": [dict(p) for p in self.ports], "Labels": labels}


def demo():
    p = lambda pub, priv: {"IP": "0.0.0.0", "PrivatePort": priv, "PublicPort": pub, "Type": "tcp"}
    return [Container(1, "shop-api", "shop-api:1.8.2", "shop", 40, 410, [p(8080, 80)], "unhealthy", age=720),
            Container(2, "postgres", "postgres:16", "shop", 3, 220, [{"PrivatePort": 5432, "Type": "tcp"}],
                      "healthy", age=3 * 86400),
            Container(3, "redis", "redis:7-alpine", "shop", 1, 18, [{"PrivatePort": 6379, "Type": "tcp"}],
                      age=3 * 86400),
            Container(4, "shop-worker", "shop-api:1.8.2", "shop", 12, 160, age=3 * 86400),
            Container(5, "blog-web", "ghost:5", "blog", running=False, age=2 * 86400),
            Container(6, "blog-db", "mysql:8", "blog", running=False, age=2 * 86400),
            Container(7, "registry", "registry:2", "", 0.4, 24, [{"IP": "127.0.0.1", "PrivatePort": 5000,
                                                                  "PublicPort": 5000, "Type": "tcp"}], age=5 * 3600)]


def many(n=40):
    out = []
    for i in range(n):
        out.append(Container(i + 1, f"svc-{i:02d}", f"example/svc:{i % 5}", f"stack{i // 10}", 1 + (i * 7) % 30,
                             30 + (i * 37) % 400, running=i % 5 != 4, age=3600 + i * 60))
    return out


class State:
    def __init__(self, conts):
        self.lock = threading.Lock()
        self.conts = collections.OrderedDict((c.id, c) for c in conts)
        self.events = []            # (time, json)
        self.counts = collections.Counter()
        self.since = self.since0 = time.time()
        self.streams = []           # open /events streams (wfile, filters)

    def find(self, ref):
        for c in self.conts.values():
            if c.id.startswith(ref) or c.name == ref:
                return c
        return None

    def event(self, c, action):
        now = time.time()
        e = {"status": action, "id": c.id, "from": c.image, "Type": "container", "Action": action,
             "Actor": {"ID": c.id, "Attributes": {"name": c.name, "image": c.image}}, "scope": "local",
             "time": int(now), "timeNano": int(now * 1e9)}
        self.events.append((now, e))
        for s in list(self.streams):
            try:
                s.write(json.dumps(e).encode() + b"\n")
                s.flush()
            except OSError:
                self.streams.remove(s)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    st: State = None

    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="application/json", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Api-Version", "1.47")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        u = urllib.parse.urlsplit(self.path)
        path = re.sub(r"^/v1\.\d+", "", u.path)
        return path, dict(urllib.parse.parse_qsl(u.query))

    def count(self, kind):
        with self.st.lock:
            self.st.counts[kind] += 1
            self.st.counts["total"] += 1

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        st, now = self.st, time.time()
        path, q = self.route()
        if path == "/_ping":
            self.count("ping")
            return self.send(200, "OK", "text/plain")
        if path == "/version":
            self.count("version")
            return self.send(200, {"Version": "27.0.0-fake", "ApiVersion": "1.47", "MinAPIVersion": "1.24",
                                   "Os": "linux", "Arch": "amd64"})
        if path == "/containers/json":
            self.count("list")
            with st.lock:
                rows = [c.summary(now) for c in st.conts.values() if c.running or q.get("all") in ("1", "true")]
            return self.send(200, rows)
        m = re.match(r"^/containers/([^/]+)/(stats|logs|json)$", path)
        if m:
            with st.lock:
                c = st.find(m.group(1))
            if c is None:
                self.count("missing")
                return self.send(404, {"message": f"No such container: {m.group(1)}"})
            if m.group(2) == "json":
                self.count("inspect")
                return self.send(200, {"Id": c.id, "Name": "/" + c.name, "Config": {"Tty": False, "Image": c.image},
                                       "State": {"Running": c.running, "Status": "running" if c.running else "exited"}})
            if m.group(2) == "stats":
                self.count("stats")
                return self.stats(c, q)
            self.count("logs")
            return self.logs(c, q)
        if path == "/events":
            self.count("events")
            return self.events(q)
        self.count("other")
        self.send(404, {"message": "page not found"})

    def reading(self, c, now):
        with self.st.lock:
            c.tick(now)
            mem = int(c.mem * MB * (1 + 0.04 * math.sin(now / 3 + c.created % 7)))
            return {"read": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "name": "/" + c.name, "id": c.id,
                    "cpu_stats": {"cpu_usage": {"total_usage": c.usage}, "system_cpu_usage": int(now * 1e9) * 4,
                                  "online_cpus": 4},
                    "memory_stats": {"usage": mem + 8 * MB, "limit": 16 * GB, "stats": {"inactive_file": 8 * MB}}}

    def stats(self, c, q):
        if not c.running:
            return self.send(200, {"read": "0001-01-01T00:00:00Z", "cpu_stats": {}, "memory_stats": {},
                                   "precpu_stats": {}, "id": c.id})
        one = q.get("one-shot") in ("1", "true")
        if q.get("stream") not in ("0", "false"):
            return self.send(400, {"message": "the fake answers stream=false only"})
        if one:
            r = self.reading(c, time.time())
            r["precpu_stats"] = {"cpu_usage": {"total_usage": 0}}
        else:   # like Docker: waits for a second reading
            first = self.reading(c, time.time())
            time.sleep(1.0)
            r = self.reading(c, time.time())
            r["precpu_stats"] = first["cpu_stats"]
        self.send(200, r)

    def logs(self, c, q):
        tail = int(q.get("tail", "100")) if q.get("tail", "all") != "all" else 1000
        t0 = self.st.since0 - 60    # fixed times: the same lines every time, like a quiet container
        lines = [(0, "INFO  listening on :80"), (5, "INFO  GET /api/cart 200 12ms"),
                 (7, "WARN  slow query 1.8s (orders)"), (9, "INFO  GET /api/products 200 31ms")]
        if c.health == "unhealthy":
            lines += [(11, "ERROR db pool exhausted (20/20 in use)"), (12, "ERROR health check failed: timeout after 5s"),
                      (14, "WARN  retrying db connection (3)")]
        out = b""
        for d, text in lines[-tail:]:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z ", time.gmtime(t0 + d)) if q.get("timestamps") in \
                ("1", "true") else ""
            payload = (ts + text + "\n").encode()
            out += struct.pack(">BxxxI", 2 if "ERROR" in text else 1, len(payload)) + payload
        self.send(200, out, "application/vnd.docker.multiplexed-stream")

    def events(self, q):
        st = self.st
        since = float(q.get("since", "0") or 0)
        until = q.get("until")
        try:
            filt = json.loads(q.get("filters") or "{}")
        except ValueError:
            filt = {}

        def ok(e):
            return all(e.get(k.capitalize() if k == "type" else "Action" if k == "event" else k) in v
                       for k, v in filt.items() if k in ("type", "event"))
        with st.lock:
            past = [e for t, e in st.events if int(t) >= since and (until is None or int(t) <= float(until)) and ok(e)]
        if until is not None:
            return self.send(200, "".join(json.dumps(e) + "\n" for e in past))
        # a stream: chunked, open until the client goes
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        class Chunked:
            def __init__(s, w):
                s.w = w

            def write(s, b):
                s.w.write(b"%x\r\n%s\r\n" % (len(b), b))

            def flush(s):
                s.w.flush()
        ch = Chunked(self.wfile)
        for e in past:
            ch.write(json.dumps(e).encode() + b"\n")
        ch.flush()
        with st.lock:
            st.streams.append(ch)
        try:
            while self.connection.recv(1, socket.MSG_PEEK):
                pass
        except OSError:
            pass
        with st.lock:
            if ch in st.streams:
                st.streams.remove(ch)
        self.close_connection = True

    def do_POST(self):
        st = self.st
        path, _ = self.route()
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        m = re.match(r"^/containers/([^/]+)/(start|stop|restart|kill)$", path)
        if not m:
            self.count("other")
            return self.send(404, {"message": "page not found"})
        self.count("action")
        with st.lock:
            c = st.find(m.group(1))
            if c is None:
                return self.send(404, {"message": f"No such container: {m.group(1)}"})
            verb = m.group(2)
            if verb == "start" and c.running or verb == "stop" and not c.running:
                return self.send(304)
            if verb in ("stop", "restart", "kill"):
                c.running, c.code, c.finished = False, 0 if verb != "kill" else 137, time.time()
                st.event(c, "die")
                st.event(c, "stop")
            if verb in ("start", "restart"):
                c.running, c.started = True, time.time()
                st.event(c, "start")
        self.send(204)

    def do_DELETE(self):
        st = self.st
        path, q = self.route()
        m = re.match(r"^/containers/([^/]+)$", path)
        self.count("action")
        with st.lock:
            c = st.find(m.group(1)) if m else None
            if c is None:
                return self.send(404, {"message": "No such container"})
            if c.running and q.get("force") not in ("1", "true"):
                return self.send(409, {"message": "container is running: stop it first"})
            del st.conts[c.id]
            st.event(c, "destroy")
        self.send(204)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        req, _ = self.socket.accept()
        return req, ("local", 0)


class Fake:
    """The fake daemon, in a thread (tests) or as a script."""

    def __init__(self, path, conts=None):
        self.path = path
        self.state = State(demo() if conts is None else conts)
        self.server = None

    def start(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        handler = type("H", (Handler,), {"st": self.state})
        self.server = Server(self.path, handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            with self.state.lock:
                streams, self.state.streams = self.state.streams, []
            del streams
        if os.path.exists(self.path):
            os.unlink(self.path)

    def command(self, line):
        st = self.state
        w = line.split()
        if not w:
            return {"ok": False}
        if w[0] == "counts":
            with st.lock:
                return {"since": st.since, "now": time.time(), **st.counts}
        if w[0] == "reset":
            with st.lock:
                st.counts.clear()
                st.since = time.time()
            return {"ok": True}
        if w[0] == "daemon-stop":
            self.stop()
            return {"ok": True}
        if w[0] == "daemon-start":
            if self.server is None:
                self.start()
            return {"ok": True}
        if w[0] == "add" and len(w) >= 2:
            with st.lock:
                c = Container(len(st.conts) + 100, w[1], "example/new:1", w[2] if len(w) > 2 else "", 8, 64, age=0)
                st.conts[c.id] = c
                st.event(c, "create")
                st.event(c, "start")
            return {"ok": True, "id": c.id[:12]}
        if w[0] in ("die", "start", "stop") and len(w) == 2:
            with st.lock:
                c = st.find(w[1])
                if c is None:
                    return {"ok": False, "error": "no such container"}
                if w[0] == "start":
                    c.running, c.started = True, time.time()
                    st.event(c, "start")
                else:
                    c.running, c.finished, c.code = False, time.time(), 1 if w[0] == "die" else 0
                    st.event(c, "die")
            return {"ok": True}
        return {"ok": False, "error": "unknown command"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("socket")
    ap.add_argument("--scenario", default="demo", choices=("demo", "many"))
    ap.add_argument("--ctl")
    a = ap.parse_args()
    fake = Fake(a.socket, demo() if a.scenario == "demo" else many()).start()
    ctl = a.ctl or a.socket + ".ctl"
    if os.path.exists(ctl):
        os.unlink(ctl)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(ctl)
    s.listen(4)
    try:
        while True:
            conn, _ = s.accept()
            with conn:
                line = conn.makefile().readline()
                conn.sendall((json.dumps(fake.command(line)) + "\n").encode())
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()
        os.unlink(ctl)


if __name__ == "__main__":
    main()
