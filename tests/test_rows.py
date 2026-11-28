"""patra's rows between readings: what may change in place and what rebuilds the list (needs Vyom on PYTHONPATH)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
try:
    import patra
except ImportError as e:   # no Vyom / GTK here
    patra = None
    why = str(e)


def cont(cid, name, state="running", status="Up 3 days", project="shop"):
    c = {"ID": cid, "Names": name, "Image": "example:1", "State": state, "Status": status, "Ports": "",
         "Labels": f"com.docker.compose.project={project}" if project else ""}
    c["health"] = patra.health(c)
    return c


@unittest.skipIf(patra is None, "patra can't be imported (Vyom on PYTHONPATH?)")
class Rows(unittest.TestCase):
    def setUp(self):
        self.p = patra.Plugin(None, {})

    def rows(self, conts, q=""):
        return self.p.rows({"up": True, "conts": conts}, q)[0]

    def test_uptime_ticking_by_is_an_update_in_place(self):
        a = self.rows([cont("a1", "web", status="Up 5 seconds"), cont("b2", "db")])
        b = self.rows([cont("a1", "web", status="Up 8 seconds"), cont("b2", "db")])
        self.assertNotEqual([r.subtitle for r in a if hasattr(r, "subtitle")],
                            [r.subtitle for r in b if hasattr(r, "subtitle")])
        self.assertEqual(self.p.skeleton(a), self.p.skeleton(b))

    def test_a_container_dying_or_coming_rebuilds(self):
        a = self.rows([cont("a1", "web"), cont("b2", "db")])
        died = self.rows([cont("a1", "web", "exited", "Exited (1) 2 seconds ago"), cont("b2", "db")])
        more = self.rows([cont("a1", "web"), cont("b2", "db"), cont("c3", "cache")])
        self.assertNotEqual(self.p.skeleton(a), self.p.skeleton(died))
        self.assertNotEqual(self.p.skeleton(a), self.p.skeleton(more))

    def test_the_ones_that_need_you_come_first(self):
        rows = self.rows([cont("a1", "web"), cont("b2", "db", "exited", "Exited (1) 1 minute ago")])
        names = [r.data["Names"] for r in rows if getattr(r, "data", None)]
        self.assertEqual(names, ["db", "web"])

    def test_sort_by_cpu_uses_the_last_reading(self):
        self.p.sort_by = "cpu"
        self.p.stats = {"a1": {"cpu": 1.0, "mem": 1, "limit": 2}, "b2": {"cpu": 50.0, "mem": 1, "limit": 2}}
        rows = self.rows([cont("a1", "web"), cont("b2", "db")])
        self.assertEqual([r.data["Names"] for r in rows if getattr(r, "data", None)], ["db", "web"])

    def test_docker_down_is_one_row(self):
        rows, counts = self.p.rows({"up": False, "conts": []}, "")
        self.assertEqual((len(rows), rows[0].data["kind"], counts), (1, "start", None))


if __name__ == "__main__":
    unittest.main()
