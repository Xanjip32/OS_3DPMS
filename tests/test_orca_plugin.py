"""The Orca Slicer printer agent (orca_plugin/) against a running OS_3DPMS, with a stand-in `orca` module.

Orca Slicer's real embedded `orca` module is not available here, so these tests check the agent's
logic and its use of the OS_3DPMS API, not its behavior inside Orca Slicer itself.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_dashboard import ServerFixture  # noqa: E402

PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orca_plugin",
                      "os3dpms_printer_agent_any.py")


def fake_orca():
    """Just enough of Orca Slicer's `orca` module (names from its Printer Agent API reference)."""
    orca = types.ModuleType("orca")
    pa = types.ModuleType("orca.printer_agent")

    class PrinterAgentBase:
        def __init__(self):
            pass

    class AgentInfo:
        def __init__(self, id="", name="", version="", description=""):
            self.id, self.name, self.version, self.description = id, name, version, description

    class Record:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    pa.PrinterAgentBase, pa.AgentInfo, pa.Record = PrinterAgentBase, AgentInfo, Record
    orca.printer_agent = pa
    orca.registered = []
    orca.packages = []

    class base:
        pass

    def plugin(cls):
        orca.packages.append(cls)
        return cls

    orca.base = base
    orca.plugin = plugin
    orca.register_capability = orca.registered.append
    return orca


def load_plugin():
    orca = fake_orca()
    sys.modules["orca"] = orca
    try:
        spec = importlib.util.spec_from_file_location("os3dpms_orca_plugin", PLUGIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("orca", None)
    return orca, mod


class PluginFileTests(unittest.TestCase):
    def test_metadata_block_and_registration(self):
        with open(PLUGIN, encoding="utf-8") as f:
            head = f.read(1200)
        self.assertTrue(head.startswith("# /// script\n"))
        self.assertIn("# [tool.orcaslicer.plugin]", head)
        self.assertIn('# dependencies = []', head)
        orca, mod = load_plugin()
        self.assertEqual(len(orca.packages), 1)
        orca.packages[0]().register_capabilities()
        ids = [cls().get_agent_info().id for cls in orca.registered]
        self.assertEqual(ids, ["os3dpms_queue", "os3dpms_direct"])
        names = [cls().get_name() for cls in orca.registered]
        self.assertEqual(len(set(names)), 2)
        self.assertFalse(any(";" in n for n in names))       # ';' is reserved by Orca

    def test_print_host_parsing(self):
        _, mod = load_plugin()
        self.assertEqual(mod.parse_print_host("127.0.0.1:8079"), ("http://127.0.0.1:8079", ""))
        self.assertEqual(mod.parse_print_host("127.0.0.1", "8079"), ("http://127.0.0.1:8079", ""))
        self.assertEqual(mod.parse_print_host("https://pc.tail1.ts.net/voron"), ("https://pc.tail1.ts.net", "voron"))
        self.assertEqual(mod.parse_print_host("pc.tail1.ts.net"), ("https://pc.tail1.ts.net", ""))
        with self.assertRaises(mod.AgentError):
            mod.parse_print_host("")

    def test_status_translation(self):
        _, mod = load_plugin()
        msg = json.loads(mod.bambu_status({"status": {
            "state": "printing", "progress": 0.426, "remaining_s": 3000, "file": "cube.gcode",
            "temperatures": {"nozzle": {"actual": 214.8, "target": 215}, "bed": {"actual": 60, "target": None}},
            "layer": {"current": 5, "total": 50}}}))["print"]
        self.assertEqual((msg["gcode_state"], msg["mc_percent"], msg["mc_remaining_time"]), ("RUNNING", 43, 50))
        self.assertEqual((msg["nozzle_temper"], msg["bed_temper"], msg["layer_num"]), (214.8, 60.0, 5))
        self.assertNotIn("bed_target_temper", msg)
        self.assertEqual(json.loads(mod.bambu_status({"status": {"state": None}}))["print"]["gcode_state"], "IDLE")


class PluginAgainstDashboardTests(ServerFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = cls.app.change_token(add="orca-test")
        cls.orca, cls.mod = load_plugin()

    def params(self, **kw):
        values = {"dev_id": "", "host": "127.0.0.1:%d" % self.app.cfg["port"], "port": "", "username": "",
                  "password": self.token, "use_ssl": False, "ca_file": ""}
        values.update(kw)
        return self.orca.printer_agent.Record(**values)

    def agent(self, cls_name="OS3DPMSQueueAgent"):
        agent = getattr(self.mod, cls_name)()
        self.events = {"ssdp": [], "connect": [], "message": []}
        agent.set_on_ssdp_msg_fn(lambda m: self.events["ssdp"].append(json.loads(m)))
        agent.set_on_local_connect_fn(lambda *a: self.events["connect"].append(a))
        agent.set_on_local_message_fn(lambda *a: self.events["message"].append(a))
        self.addCleanup(agent.disconnect_printer)
        return agent

    def gcode(self, text=b"G28\n"):
        fd, path = tempfile.mkstemp(suffix=".gcode")
        with os.fdopen(fd, "wb") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_connect_announces_printers_and_rejects_a_wrong_token(self):
        agent = self.agent()
        self.assertEqual(agent.connect_printer(self.params(password="os3_wrong")), -1)
        self.assertEqual(self.events["connect"][-1][0], 1)
        self.app.token_failures.hits.clear()
        self.assertEqual(agent.connect_printer(self.params()), 0)
        self.assertEqual(self.events["connect"][-1][0], 0)
        self.assertEqual({d["dev_id"] for d in self.events["ssdp"]}, {"os3dpms:voron", "os3dpms:p1s"})
        self.assertTrue(agent.start_discovery(True, False))

    def test_queue_agent_adds_the_job_to_the_os3dpms_queue(self):
        agent = self.agent()
        self.assertEqual(agent.connect_printer(self.params()), 0)
        updates = []
        params = self.orca.printer_agent.Record(dev_id="os3dpms:voron", filename=self.gcode(), task_name="Cube",
                                                project_name="Cube", dev_ip="", password=self.token)
        self.assertEqual(agent.start_local_print(params, lambda *a: updates.append(a), lambda: False), 0)
        self.addCleanup(os.remove, os.path.join(self.app.files_dir, "Cube.gcode"))
        job = [j for j in self.app.queue if j["by"] == "token:orca-test"][-1]
        self.assertEqual((job["file"], job["printer"]), ("Cube.gcode", "voron"))
        self.assertEqual(updates[-1][0], self.mod.STAGE_FINISHED)
        with self.app.lock:
            self.app.queue = [j for j in self.app.queue if j["by"] != "token:orca-test"]
            self.app.save_queue()

    def test_direct_agent_prints_now_and_pinned_host_picks_the_printer(self):
        agent = self.agent("OS3DPMSDirectAgent")
        self.assertEqual(agent.connect_printer(self.params(host="127.0.0.1:%d/voron" % self.app.cfg["port"])), 0)
        params = self.orca.printer_agent.Record(dev_id="", filename=self.gcode(), task_name="Now.gcode",
                                                project_name="", dev_ip="", password=self.token)
        self.assertEqual(agent.start_local_print(params, None, None), 0)
        self.assertIn(b'name="print"', self.printer.uploads[-1])
        self.assertIn(b'filename="Now.gcode"', self.printer.uploads[-1])

    def test_errors_are_reported_not_raised(self):
        agent = self.agent()
        self.assertEqual(agent.connect_printer(self.params()), 0)
        updates = []
        params = self.orca.printer_agent.Record(dev_id="", filename=self.gcode(), task_name="x",
                                                project_name="", dev_ip="", password=self.token)
        self.assertEqual(agent.start_local_print(params, lambda *a: updates.append(a), None), -1)
        self.assertIn("choose an OS_3DPMS printer", updates[-1][2])       # two printers, none chosen
        params.dev_id = "os3dpms:voron"
        params.filename = "/no/such/file.gcode"
        self.assertEqual(agent.start_local_print(params, lambda *a: updates.append(a), None), -1)
        cancelled = threading.Event()
        cancelled.set()
        params.filename = self.gcode()
        self.assertEqual(agent.start_local_print(params, lambda *a: updates.append(a), cancelled.is_set), -1)
        self.assertEqual(updates[-1][2], "cancelled")
        self.assertEqual(agent.start_sdcard_print(params, None, None), -7010)

    def test_verify_job_only_checks_the_connection(self):
        agent = self.agent()
        params = self.orca.printer_agent.Record(dev_id="", filename="", task_name="", project_name="verify_job",
                                                dev_ip="127.0.0.1:%d" % self.app.cfg["port"], password=self.token)
        self.assertEqual(agent.start_send_gcode_to_sdcard(params, None, None, None), 0)
        params.password = "os3_wrong"
        self.app.token_failures.hits.clear()
        self.assertEqual(agent.start_send_gcode_to_sdcard(params, None, None, None), -1)
        self.app.token_failures.hits.clear()

    def test_status_push_and_device_commands(self):
        agent = self.agent()
        self.mod.POLL_S = 0.2
        self.addCleanup(setattr, self.mod, "POLL_S", 3.0)
        self.assertEqual(agent.connect_printer(self.params()), 0)
        for _ in range(50):
            if self.events["message"]:
                break
            threading.Event().wait(0.1)
        dev, payload = self.events["message"][0]
        self.assertTrue(dev.startswith("os3dpms:"))
        self.assertIn("gcode_state", json.loads(payload)["print"])
        self.assertEqual(agent.send_message_to_printer("os3dpms:voron", '{"print": {"command": "gcode_line"}}', 0, 0),
                         -7010)
        self.assertEqual(agent.send_message_to_printer("os3dpms:voron", "not json", 0, 0), -1)
        self.assertEqual(agent.request_bind_ticket(), (0, ""))


if __name__ == "__main__":
    unittest.main()
