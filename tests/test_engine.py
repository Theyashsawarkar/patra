"""patra.engine: the Engine API's answers turned into what the list shows (no GTK, no Docker)."""
import importlib.util
import pathlib
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
