"""Unit tests for fleet's pure-python parts (roster, commands, themes, versions).
Everything that touches disk goes to a temp HOME so a developer's real
~/.fleet and ~/.claude are never read or written."""
import json, os, sys, tempfile, time, unittest

TMP = tempfile.mkdtemp(prefix="fleet-test-")
os.environ["HOME"] = TMP                      # must be set before fleet is imported
os.environ.pop("FLEET_THEME", None)
os.environ.pop("COLORFGBG", None)
os.environ.pop("FLEET_ROSTER_DIR", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet  # noqa: E402


def live(pid, sid, name="s", source="user", cwd="/tmp", kind="interactive"):
    return {"pid": pid, "sessionId": sid, "kind": kind, "cwd": cwd,
            "name": name, "nameSource": source}


class RosterTest(unittest.TestCase):
    def setUp(self):
        for sid in list(fleet.roster_read()):
            fleet.roster_forget(sid)

    def test_add_read_update_forget(self):
        fleet.roster_add("aaaa-1", "one", "/tmp", "yolo", prompt="hi", win_name="tab-one")
        r = fleet.roster_read()["aaaa-1"]
        self.assertEqual((r["name"], r["preset"], r["prompt"], r["state"]), ("one", "yolo", "hi", "active"))
        self.assertEqual(fleet.roster_update("aaaa-1", state="closed")["state"], "closed")
        self.assertIsNone(fleet.roster_update("nope", state="closed"))
        self.assertTrue(fleet.roster_forget("aaaa-1"))
        self.assertFalse(fleet.roster_forget("aaaa-1"))

    def test_reconcile_adopts_closes_and_parks(self):
        fleet.roster_add("aaaa-1", "one", "/tmp", "yolo", win_name="tab-one")
        fleet.roster_add("bbbb-2", None, "/tmp", None, win_name="tab-two")
        st = {}
        rows = [live(1, "aaaa-1", "one"), live(2, "cccc-3", "fleet-9", "derived"),
                live(3, "dddd-4", "x"), live(4, "eeee-5", "bg", kind="bg")]
        fleet.roster_reconcile(rows, {1: "one", 2: "adopted-tab", 4: "bg"}, st)
        r = fleet.roster_read()
        self.assertIn("cccc-3", r)                       # hosted stranger adopted
        self.assertIsNone(r["cccc-3"]["name"])           # derived name not pinned
        self.assertEqual(r["cccc-3"]["win_name"], "adopted-tab")
        self.assertNotIn("dddd-4", r)                    # not on the fleet server
        self.assertNotIn("eeee-5", r)                    # background agents never
        self.assertEqual(r["aaaa-1"]["win_name"], "one")  # tab name follows tmux
        self.assertEqual(st["seen"], {"aaaa-1", "cccc-3"})
        parked = [x["sessionId"] for x in fleet.roster_parked({"aaaa-1", "cccc-3"})]
        self.assertEqual(parked, ["bbbb-2"])
        # aaaa-1 vanishes while watching -> closed; bbbb-2 (never seen) stays parked
        fleet.roster_reconcile(rows[1:], {2: "adopted-tab"}, st)
        r = fleet.roster_read()
        self.assertEqual(r["aaaa-1"]["state"], "closed")
        self.assertEqual(r["aaaa-1"]["closed_why"], "exited")
        self.assertEqual(r["bbbb-2"]["state"], "active")
        # a closed row that comes back live is active again
        fleet.roster_reconcile(rows[:1], {1: "one"}, st)
        self.assertEqual(fleet.roster_read()["aaaa-1"]["state"], "active")

    def test_parked_hides_just_resumed(self):
        fleet.roster_add("aaaa-1", "one", "/tmp", None)
        fleet.roster_update("aaaa-1", resumed_at=int(time.time()))
        self.assertEqual(fleet.roster_parked(set()), [])
        fleet.roster_update("aaaa-1", resumed_at=int(time.time()) - fleet.RESUME_GRACE - 1)
        self.assertEqual(len(fleet.roster_parked(set())), 1)

    def test_find(self):
        fleet.roster_add("abcd1234-1", "one", "/tmp", None, win_name="tab-one")
        fleet.roster_add("abcd9999-2", "two", "/tmp", None, win_name="tab-two")
        self.assertEqual(fleet.roster_find("tab-two")["sessionId"], "abcd9999-2")
        self.assertEqual(fleet.roster_find("one")["sessionId"], "abcd1234-1")
        self.assertEqual(fleet.roster_find("abcd1234")["sessionId"], "abcd1234-1")
        self.assertIsNone(fleet.roster_find("abc"))       # prefix too short to be safe
        self.assertIsNone(fleet.roster_find("zzz"))

    def test_close_prunes_old_history(self):
        fleet.roster_add("old-1", "old", "/tmp", None)
        fleet.roster_update("old-1", state="closed", closed_at=int(time.time()) - fleet.ROSTER_KEEP_CLOSED - 10)
        fleet.roster_reconcile([], {}, {})
        self.assertNotIn("old-1", fleet.roster_read())


class CommandTest(unittest.TestCase):
    def test_build_claude_cmd(self):
        c = fleet.build_claude_cmd("yolo", "nm", None, resume="sid1")
        self.assertEqual(c, ["claude", "--dangerously-skip-permissions", "--name", "nm", "--resume", "sid1"])
        c = fleet.build_claude_cmd(None, None, "hi", session_id="u1")
        self.assertEqual(c, ["claude", "--session-id", "u1", "hi"])
        self.assertEqual(fleet.build_claude_cmd("no-such-preset"), ["claude"])

    def test_wrap_inner_exit_hook(self):
        self.assertNotIn("_exited", fleet.wrap_inner(["claude"]))
        w = fleet.wrap_inner(["claude", "--resume", "x y"], "sid")
        self.assertIn("'x y'", w)
        self.assertIn("_exited sid", w)
        self.assertTrue(w.endswith("exec $SHELL"))

    def test_shell_quote(self):
        self.assertEqual(fleet.shell_quote("plain-1.0/x"), "plain-1.0/x")
        self.assertEqual(fleet.shell_quote("it's"), "'it'\\''s'")


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(fleet.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(fleet.parse_version("0.10.0-rc1"), (0, 10, 0))
        self.assertIsNone(fleet.parse_version("junk"))
        self.assertGreater(fleet.parse_version("v0.10.0"), fleet.parse_version("v0.9.9"))
        self.assertIsNotNone(fleet.parse_version(fleet.__version__))

    def test_update_available_from_cache(self):
        fleet._write_update_cache("v0.0.1")
        self.assertIsNone(fleet.update_available())
        fleet._write_update_cache("v99.0.0")
        self.assertEqual(fleet.update_available(), "v99.0.0")
        fleet._write_update_cache(None)
        self.assertIsNone(fleet.update_available())


class ThemeTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLEET_THEME", None)
        os.environ.pop("COLORFGBG", None)
        try:
            os.remove(fleet.USER_CONFIG)
        except FileNotFoundError:
            pass

    def test_resolution_order(self):
        self.assertEqual(fleet.resolve_theme(), "dark")          # nothing set -> dark
        os.environ["COLORFGBG"] = "0;15"
        self.assertEqual(fleet.resolve_theme(), "light")         # light bg reported
        os.environ["COLORFGBG"] = "15;0"
        self.assertEqual(fleet.resolve_theme(), "dark")
        fleet.save_user_config(theme="light")
        self.assertEqual(fleet.resolve_theme(), "light")         # config beats detection
        os.environ["FLEET_THEME"] = "dark"
        self.assertEqual(fleet.resolve_theme(), "dark")          # env beats config

    def test_palettes_complete(self):
        for name, t in fleet.THEMES.items():
            self.assertEqual(set(t["dash"]), {"waiting", "busy", "idle", "shell", "parked", "hdr"}, name)
            self.assertEqual(set(t["tmux"]["win"]), {"waiting", "busy", "idle", "shell"}, name)
            for c256, c8, attr in t["dash"].values():
                self.assertTrue(0 <= c256 < 256 and attr in ("", "bold", "dim"))


if __name__ == "__main__":
    unittest.main()
