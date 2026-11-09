"""Finding printers on the network, and filling in a Bambu serial number automatically."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import cameras, config, discover, web  # noqa: E402
from tests.mock_printer import MockPrinter  # noqa: E402
from tests.test_cameras import CameraServer  # noqa: E402

NOTIFY = (b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nServer: UPnP/1.0\r\n"
          b"Location: 10.9.9.9\r\nNT: urn:bambulab-com:device:3dprinter:1\r\n"
          b"USN: 01P00A000000001\r\nCache-Control: max-age=1800\r\nDevModel.bambu.com: C12\r\n"
          b"DevName.bambu.com: Workshop P1S\r\nDevConnect.bambu.com: lan\r\n"
          b"DevVersion.bambu.com: 01.10.00.00\r\n\r\n")


class ParseTests(unittest.TestCase):
    def test_bambu_announcement(self):
        info = discover.parse_bambu(NOTIFY, "192.0.2.6")
        self.assertEqual(info["host"], "192.0.2.6")  # sender address, not the Location header
        self.assertEqual(info["serial"], "01P00A000000001")
        self.assertEqual((info["model"], info["name"], info["firmware"]), ("P1S", "Workshop P1S", "01.10.00.00"))
        self.assertTrue(info["lan_only"])
        self.assertFalse(info["rtsp_camera"])

    def test_x1_camera_needs_rtsp(self):
        info = discover.parse_bambu(NOTIFY.replace(b"C12", b"BL-P001"), "192.0.2.7")
        self.assertEqual(info["model"], "X1 Carbon")
        self.assertTrue(info["rtsp_camera"])

    def test_other_devices_are_ignored(self):
        self.assertIsNone(discover.parse_bambu(NOTIFY.replace(b"bambulab-com", b"roku-com"), "192.0.2.6"))
        self.assertIsNone(discover.parse_bambu(b"M-SEARCH * HTTP/1.1\r\n\r\n", "192.0.2.6"))
        self.assertIsNone(discover.parse_bambu(NOTIFY.replace(b"01P00A000000001", b"../../x"), "192.0.2.6"))
        self.assertIsNone(discover.parse_bambu(b"\xff\xfe garbage", "192.0.2.6"))

    def test_control_characters_are_removed_from_names(self):
        info = discover.parse_bambu(NOTIFY.replace(b"Workshop P1S", b"P1S\x1b[31m"), "192.0.2.6")
        self.assertEqual(info["name"], "P1S[31m")

    def test_only_private_networks(self):
        with mock.patch.object(discover.socket, "getaddrinfo", return_value=[
                (2, 1, 6, "", ("100.101.1.2", 0)), (2, 1, 6, "", ("192.168.7.20", 0)),
                (2, 1, 6, "", ("8.8.8.8", 0)), (2, 1, 6, "", ("127.0.0.1", 0))]), \
                mock.patch.object(discover.socket, "socket", side_effect=OSError):
            self.assertEqual([str(n) for n in discover.local_networks()], ["192.168.7.0/24"])


class KlipperProbeTests(unittest.TestCase):
    def test_probe_reads_name_and_camera_orientation(self):
        printer = MockPrinter()
        self.addCleanup(printer.shutdown)
        found = discover.probe_klipper("127.0.0.1", ports=(printer.port,))
        self.assertEqual(found["name"], "Mock Voron")
        self.assertEqual(found["http_port"], printer.port)
        self.assertEqual((found["rotate"], found["flip_vertical"], found["cameras"]), (180, True, 1))

    def test_probe_ignores_non_moonraker_servers(self):
        srv = CameraServer()  # answers HTTP 500 for /server/info
        self.addCleanup(srv.shutdown)
        self.assertIsNone(discover.probe_klipper("127.0.0.1", ports=(srv.server_address[1],)))


class ListenerTests(unittest.TestCase):
    def test_callbacks_and_memory(self):
        listener = discover.BambuListener()
        seen = []
        listener.callbacks.append(seen.append)
        packets = [(NOTIFY, ("192.0.2.6", 1900))]

        class FakeUdp:
            def recvfrom(self, n):
                if packets:
                    return packets.pop()
                listener.stop()
                raise discover.socket.timeout()

            def close(self):
                pass

        with mock.patch.object(listener, "_open", return_value=FakeUdp()):
            listener.until = time.time() + 5
            listener._run()
        self.assertEqual([i["serial"] for i in seen], ["01P00A000000001"])
        self.assertEqual(listener.printers()[0]["host"], "192.0.2.6")
        self.assertIsNone(listener.thread)

    def test_port_in_use_is_reported(self):
        listener = discover.BambuListener()
        with mock.patch.object(listener, "_open", side_effect=OSError("in use")):
            listener.until = time.time() + 5
            listener._run()
        self.assertIn("UDP port 2021", listener.error)


class AutoSerialTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg_path = os.path.join(self.dir, "config.json")
        config.save(self.cfg_path, {"port": 18080, "admin_port": 18079, "printers": [
            {"id": "p1s", "name": "P1S", "type": "bambu", "host": "192.0.2.6", "access_code": "12345678"}]})

    def test_serial_from_announcement_is_used_and_saved(self):
        app = web.App.__new__(web.App)
        app.config_path = self.cfg_path
        app.lock = web.threading.RLock()
        app.cfg = config.load(self.cfg_path)
        app.bambu_watchers = {}
        app.pins = None
        with mock.patch.object(web.bambu.BambuWatcher, "start"):
            app._bambu_seen(discover.parse_bambu(NOTIFY, "192.0.2.6"))
            app._bambu_seen(discover.parse_bambu(NOTIFY, "192.0.2.99"))  # another printer: ignored
        self.assertEqual(app.cfg["printers"][0]["serial"], "01P00A000000001")
        self.assertIn("p1s", app.bambu_watchers)
        saved = config.load(self.cfg_path)["printers"][0]
        self.assertEqual(saved["serial"], "01P00A000000001")
        self.assertEqual(config.secret(saved, "access_code"), "12345678")


class CameraErrorTests(unittest.TestCase):
    def test_printer_camera_service_error_is_explained(self):
        srv = CameraServer()
        self.addCleanup(srv.shutdown)
        hub = cameras.CameraHub("v", None)
        hub.last_request = time.time()
        url = "http://127.0.0.1:%d/broken" % srv.server_address[1]
        with self.assertRaises(ConnectionError) as ctx:
            cameras.klipper_camera(hub, [url])
        self.assertIn("camera service answered HTTP 500", str(ctx.exception))
        self.assertIn("crowsnest", str(ctx.exception))

    def test_snapshot_answers_quickly_when_camera_is_known_broken(self):
        hub = cameras.CameraHub("v", None)
        hub.error = "camera unplugged"
        with mock.patch.object(hub, "touch"), mock.patch.object(hub, "wait_frame", return_value=(None, 0)) as wait:
            self.assertIsNone(hub.snapshot())
        self.assertEqual(wait.call_args.args[1], 1)

    def test_retries_slow_down(self):
        hub = cameras.CameraHub("v", mock.Mock(side_effect=OSError("down")))
        waits = []
        with mock.patch.object(hub, "active", side_effect=[True, True, True, False]), \
                mock.patch.object(hub.cond, "wait_for", side_effect=lambda pred, t: waits.append(t)):
            hub._run()
        self.assertEqual(waits, [3, 5, 10])
        self.assertEqual(hub.error, "down")

    def test_restart_clears_old_error(self):
        hub = cameras.CameraHub("v", lambda h: None)
        hub.error = "old"
        hub.touch()
        self.assertIsNone(hub.error)
        hub.stop()


class DiscoverEndpointTests(unittest.TestCase):
    def test_marks_saved_printers(self):
        handler = web.Handler.__new__(web.Handler)
        handler.app = mock.Mock()
        handler.app.lock = web.threading.RLock()
        handler.app.cfg = {"printers": [{"type": "klipper", "host": "192.0.2.21"}]}
        result = {"printers": [{"type": "klipper", "host": "192.0.2.21"}, {"type": "klipper", "host": "192.0.2.22"}],
                  "networks": ["192.0.2.0/24"], "bambu_error": None}
        sent = {}
        handler.send_json = lambda code, obj: sent.update(code=code, body=json.loads(json.dumps(obj)))
        with mock.patch.object(web.discover, "find_printers", return_value=result):
            handler.send_discovery()
        self.assertEqual([p["saved"] for p in sent["body"]["printers"]], [True, False])


if __name__ == "__main__":
    unittest.main()
