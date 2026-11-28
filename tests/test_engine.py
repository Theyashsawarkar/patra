"""patra.engine: the Engine API's answers turned into what the list shows (no GTK, no Docker)."""
import importlib.util
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fake_engine

spec = importlib.util.spec_from_file_location("engine", pathlib.Path(__file__).parent.parent / "patra" / "engine.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class Shapes(unittest.TestCase):
    def test_ports_like_docker_ps(self):
        ports = [{"PrivatePort": 80, "PublicPort": 8080, "IP": "0.0.0.0", "Type": "tcp"},
                 {"PrivatePort": 80, "PublicPort": 8080, "IP": "::", "Type": "tcp"},
                 {"PrivatePort": 5432, "Type": "tcp"}]
        self.assertEqual(engine._ports(ports), "0.0.0.0:8080->80/tcp, [::]:8080->80/tcp, 5432/tcp")

    def test_containers_like_docker_ps(self):
        api = [{"Id": "abcdef0123456789", "Names": ["/demo-web-1"], "Image": "nginx:alpine", "State": "running",
                "Status": "Up 2 hours (healthy)", "Ports": [], "Labels": {"com.docker.compose.project": "demo"}}]
        with mock.patch.object(engine, "get", return_value=api):
            c = engine.containers()[0]
        self.assertEqual((c["ID"], c["Names"], c["Labels"]), ("abcdef012345", "demo-web-1",
                                                               "com.docker.compose.project=demo"))
        with mock.patch.object(engine, "get", return_value=None):
            self.assertIsNone(engine.containers())

    def test_cpu_between_two_readings(self):
        a = {"cpu_stats": {"cpu_usage": {"total_usage": 1000}, "system_cpu_usage": 10000, "online_cpus": 4}}
        b = {"cpu_stats": {"cpu_usage": {"total_usage": 1500}, "system_cpu_usage": 20000, "online_cpus": 4}}
        self.assertAlmostEqual(engine._cpu_pct(b, a), 20.0)
        self.assertIsNone(engine._cpu_pct(b, {}))

    def test_memory_without_the_page_cache(self):
        self.assertEqual(engine._mem({"memory_stats": {"usage": 5000, "limit": 9000,
                                                       "stats": {"inactive_file": 1000}}}), (4000, 9000))

    def test_restarts_counted_per_container(self):
        text = '{"id": "abcdef0123456789"}\n{"id": "abcdef0123456789"}\n{"Actor": {"ID": "999999999999aa"}}\n'
        with mock.patch.object(engine, "get", return_value=text):
            self.assertEqual(engine.restarts(), {"abcdef012345": 2, "999999999999": 1})


class AgainstTheFake(unittest.TestCase):
    """engine.py against a fake Engine API on a private socket (never Docker's own)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sock = os.path.join(self.dir.name, "d.sock")
        self.fake = fake_engine.Fake(self.sock).start()
        self.env = mock.patch.dict(os.environ, {"DOCKER_HOST": "unix://" + self.sock})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.fake.stop()
        self.dir.cleanup()

    def counts(self):
        return self.fake.command("counts")

    def test_list_stats_and_logs_over_one_connection(self):
        with engine.Session() as s:
            conts = engine.containers(s)
            running = [c["ID"] for c in conts if c["State"] == "running"]
            st = engine.Stats()
            first = st.read(running, s)
            self.assertEqual(set(first), set(running))
            self.assertTrue(all(v["cpu"] is None and v["mem"] > 0 for v in first.values()))   # one reading: no CPU yet
            self.assertTrue(st.needs_second(running) is False)
            time.sleep(0.05)
            second = st.read(running, s)
            self.assertTrue(all(v["cpu"] is not None for v in second.values()))
            text = engine.logs(running[0], 3, s)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertRegex(text.splitlines()[0], r"^\d{4}-\d\d-\d\dT")
        c = self.counts()
        self.assertEqual(c["stats"], 2 * len(running))           # one-shot: one request per container
        self.assertEqual(c["list"], 1)

    def test_up_with_another_socket_is_whether_it_is_there(self):
        self.assertTrue(engine.up())
        self.assertEqual(self.counts().get("total", 0), 0)        # no ping: the list's request finds out
        self.fake.command("daemon-stop")
        self.assertFalse(engine.up())

    def test_a_socket_nobody_answers_is_a_stopped_daemon(self):
        self.fake.stop()
        import socket
        s = socket.socket(socket.AF_UNIX)
        s.bind(self.sock)            # there, but nobody listens
        with engine.Session() as ses:
            self.assertIsNone(engine.containers(ses))
            self.assertTrue(engine.refused(ses))
        s.close()

    def test_no_access_is_not_a_stopped_daemon(self):
        with engine.Session() as ses:
            with mock.patch.object(engine._Unix, "connect", side_effect=PermissionError(13, "denied")):
                self.assertIsNone(engine.containers(ses))
            self.assertFalse(engine.refused(ses))

    def test_actions(self):
        cid = engine.containers()[2]["ID"]
        self.assertEqual(engine.action(cid, "stop"), (True, ""))
        self.assertEqual(engine.action(cid, "stop"), (True, ""))  # already stopped (304) is fine
        ok, msg = engine.action(engine.containers()[0]["ID"], "rm")
        self.assertFalse(ok)
        self.assertIn("running", msg)
        self.assertEqual(engine.action(cid, "rm"), (True, ""))
        self.assertNotIn(cid, [c["ID"] for c in engine.containers()])

    def test_restarts_from_the_events(self):
        cid = engine.containers()[0]["ID"]
        self.fake.command("die shop-api")
        self.fake.command("start shop-api")
        self.fake.command("die shop-api")
        self.assertEqual(engine.restarts(), {cid: 2})


class NeverWakesDocker(unittest.TestCase):
    def test_a_stopped_docker_service_is_never_connected_to(self):
        """docker.service inactive (docker.socket would start it on connect): no request at all."""
        calls = []

        class Bus:
            def call_sync(self, _dest, _path, _iface, method, args, *_):
                calls.append(method)
                v = mock.Mock()
                if method == "LoadUnit":
                    v.unpack.return_value = ("/org/freedesktop/systemd1/unit/docker_2eservice",)
                else:
                    v.unpack.return_value = ("loaded",) if args.unpack()[1] == "LoadState" else ("inactive",)
                return v
        gio = mock.Mock()
        gio.bus_get_sync.return_value = Bus()
        glib = mock.Mock()
        glib.Variant = lambda sig, val: mock.Mock(unpack=lambda: val)
        repo = mock.Mock(Gio=gio, GLib=glib)
        with mock.patch.dict(os.environ, {"DOCKER_HOST": ""}), \
                mock.patch.dict(sys.modules, {"gi": mock.Mock(repository=repo), "gi.repository": repo}), \
                mock.patch.object(engine._Unix, "connect", side_effect=AssertionError("connected")):
            engine._UNIT.clear()
            self.assertFalse(engine.up())
        self.assertIn("LoadUnit", calls)
        engine._UNIT.clear()


if __name__ == "__main__":
    unittest.main()
