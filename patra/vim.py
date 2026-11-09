"""patra's vim keys, as data. NORMAL is the start (it's a tool you manage
with, not a search box); `/` types a filter. The modes, counts, which-key
strip, help (?) and palette (Space Space) are Vyom's (vyom/keys.py,
vyom/modal.py; Vyom ADR 0008). Space groups follow Vayu's shared letters
(f filter, s sort, y copy, d delete, o open outside, u toggles, r refresh);
c (container) is patra's own.

An action "verb:arg" runs Plugin.v_<verb>(count, arg) in patra/__init__.py,
or Vyom's own (moved, nop...).
"""
from vyom.keys import Bind

B = Bind
GROUPS = {" c": "container"}


def moved(seq, to):
    """An old key that moved in v0.3 (for one release): says where to."""
    return B(seq, f"moved:{to}", "", hidden=True)


NORMAL = [
    B("<CR>", "logs", "Follow the selected container's logs in a terminal"),
    B("o", "shell", "Open a shell in the selected container"),
    B("x", "toggle", "Stop the selected container, or start a stopped one"),
    B("R", "restart", "Restart the selected container"),
    B("dd", "remove", "Remove the selected stopped container (y confirms)"),
    B("yy", "copy:name", "Copy the container's name"),
    B("r", "refresh", "Reload the list now"),
    # The old Ctrl keys: harmless, so they stay (Ctrl+L and Ctrl+Backspace moved).
    B("<C-r>", "restart", "", mode="any", hidden=True),
    B("<C-s>", "shell", "", mode="any", hidden=True),
    B("<C-x>", "toggle", "", mode="any", hidden=True),
    moved("<C-l>", "Enter"),
    moved("<C-BS>", "d d"),
]

LEADER = [
    B(" cr", "restart", "Restart the selected container"),
    B(" cs", "toggle", "Stop the selected container, or start a stopped one"),
    B(" cl", "logs", "Follow the selected container's logs in a terminal"),
    B(" ch", "shell", "Open a shell in the selected container"),
    B(" ot", "shell", "Open a shell in the selected container, in a terminal"),
    B(" yy", "copy:name", "Copy the container's name"),
    B(" yi", "copy:id", "Copy the container's ID"),
    B(" yp", "copy:ports", "Copy the container's ports"),
    B(" fr", "only:running", "Show only running containers (again: show all)"),
    B(" fs", "only:stopped", "Show only stopped containers (again: show all)"),
    B(" ft", "only:trouble", "Show only the ones that need you (again: show all)"),
    B(" fc", "only:", "Clear the filter: show every container"),
    B(" sc", "sort:cpu", "Sort the containers by CPU (busiest first; again: the usual order)"),
    B(" sm", "sort:mem", "Sort the containers by memory (biggest first; again: the usual order)"),
    B(" sn", "sort:name", "Sort the containers by name (A to Z; again: the usual order)"),
    B(" dd", "remove", "Remove the selected stopped container (y confirms)"),
]

SECTIONS = [
    ("docker", "normal", "Normal mode (where it opens; `/` types a filter)", NORMAL),
    ("docker leader", "normal", "Leader (Space)", LEADER),
]


def keys(_tab):
    return NORMAL + LEADER
