"""Direct Marlin serial adapter with a simulated printer (no pyserial, no hardware)."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import adapters  # noqa: E402
from dashboard.adapters import marlin_serial as ms  # noqa: E402

M115 = ["FIRMWARE_NAME:Marlin 2.0.9.3 (Jan  1 2024) SOURCE_CODE_URL:github.com/MarlinFirmware/Marlin "
        "PROTOCOL_VERSION:1.0 MACHINE_TYPE:Anycubic Kossel EXTRUDER_COUNT:1 UUID:cede2a2f",
        "Cap:SERIAL_XON_XOFF:0", "Cap:AUTOREPORT_TEMP:0", "Cap:AUTOREPORT_POS:0", "Cap:EEPROM:1"]


class FakeMarlin:
    """Answers like Marlin: lines, then `ok`. `silent` simulates a dead printer."""

    def __init__(self, printing=True, unknown=("M31",), silent=False):
        self.printing, self.unknown, self.silent = printing, set(unknown), silent
        self.out, self.written, self.closed = [], [], False
        self.lock = threading.Lock()

    def write(self, data):
        cmd = data.decode().strip()
        self.written.append(cmd)
        if self.silent:
            return
        word = cmd.split()[0]
        lines = []
        if word in self.unknown:
            lines = ['echo:Unknown command: "%s"' % cmd]
        elif word == "M115":
            lines = list(M115)
        elif word == "M105":
            lines = []
            with self.lock:
                self.out.append(b"ok T:205.3 /205.0 B:60.1 /60.0 @:64 B@:0\n")
            return
        elif word == "M27":
            lines = ["SD printing byte 2500/10000"] if self.printing else ["Not SD printing"]
        elif word == "M114":
            lines = ["X:10.00 Y:-5.00 Z:12.40 E:0.00 Count A:1 B:2 C:3"]
        elif word == "M119":
            lines = ["Reporting endstop status", "x_max: open", "y_max: open", "z_max: TRIGGERED"]
        elif word == "M503":
            lines = ["echo:; Steps per unit:", "echo:  M92 X80.00 Y80.00 Z80.00 E96.00"]
        with self.lock:
            self.out += [(ln + "\n").encode() for ln in lines] + [b"ok\n"]

    def readline(self):
        with self.lock:
            if self.out:
                return self.out.pop(0)
        time.sleep(0.01)
        return b""

    def close(self):
        self.closed = True


def printer(**extra):
    p = {"id": "kossel", "name": "Kossel", "type": "marlin_serial", "host": "localhost",
         "serial_port": "COM7", "baudrate": 250000, "allow_remote_control": False, "allow_file_upload": False}
    p.update(extra)
    return p


class MarlinParsingTests(unittest.TestCase):
    def test_m115(self):
        fw, caps = ms.parse_m115(M115)
        self.assertEqual(fw["firmware_name"], "Marlin 2.0.9.3 (Jan  1 2024)")
        self.assertEqual(fw["machine_type"], "Anycubic Kossel")
        self.assertEqual(caps, {"SERIAL_XON_XOFF": False, "AUTOREPORT_TEMP": False, "AUTOREPORT_POS": False,
                                "EEPROM": True})

    def test_temperatures(self):
        self.assertEqual(ms.parse_temps("ok T:205.3 /205.0 B:60.1 /60.0 @:64 B@:0"),
                         {"nozzle": {"actual": 205.3, "target": 205.0}, "bed": {"actual": 60.1, "target": 60.0}})
        self.assertEqual(ms.parse_temps(" T0:20.0 /0.0 T1:21.0 /0.0 B:19.0 /0.0")["nozzle1"]["actual"], 21.0)

    def test_print_time(self):
        self.assertEqual(ms.parse_print_time("echo:Print time: 1h 2m 3s"), 3723)
        self.assertEqual(ms.parse_print_time("echo:Print time: 2d 0h 0m 1s"), 172801)
        self.assertIsNone(ms.parse_print_time("ok"))


class MarlinAdapterTests(unittest.TestCase):
    def make(self, fake, **extra):
        a = ms.MarlinSerialAdapter(printer(**extra), opener=lambda port, baud: fake)
        self.addCleanup(a.disconnect)
        return a

    def test_status_from_simulated_printer(self):
        fake = FakeMarlin()
        a = self.make(fake)
        a.open()
        a.poll()
        st = a.get_status()
        self.assertEqual(st["state"], "printing")
        self.assertEqual(st["progress"], 0.25)
        self.assertEqual(st["temperatures"]["nozzle"]["actual"], 205.3)
        self.assertEqual(st["position"], [10.0, -5.0, 12.4])
        self.assertEqual(st["transport"], "serial")
        self.assertEqual(a.firmware["machine_type"], "Anycubic Kossel")

    def test_unsupported_command_is_remembered(self):
        fake = FakeMarlin()
        a = self.make(fake)
        a.open()
        a.poll()   # M31 unknown here
        self.assertIn("M31", a.unsupported)
        sent = len([c for c in fake.written if c == "M31"])
        a.poll()
        self.assertEqual(len([c for c in fake.written if c == "M31"]), sent)

    def test_only_read_commands_are_ever_sent(self):
        fake = FakeMarlin()
        a = self.make(fake)
        a.open()
        for cmd in ("M24", "M25", "M112", "G28", "M104 S200", "M524", "M500"):
            with self.assertRaises(adapters.AdapterError):
                a.send(cmd)
        a.poll()
        a.query_endstops()
        self.assertTrue(all(c.split()[0] in ms.READ_COMMANDS for c in fake.written), fake.written)

    def test_no_control_capabilities(self):
        a = self.make(FakeMarlin(), allow_remote_control=True, allow_file_upload=True)
        caps = a.get_capabilities()
        self.assertTrue(caps["monitor"])
        self.assertFalse(any(caps[k] for k in ("can_upload", "can_start_print", "can_pause", "can_cancel")))
        with self.assertRaises(adapters.NotSupported):
            a.cancel_print()

    def test_serial_timeout(self):
        a = self.make(FakeMarlin(silent=True))
        with self.assertRaises(adapters.AdapterError):
            a.open()  # no answer to M115 within its timeout

    def test_one_owner_per_port(self):
        a = self.make(FakeMarlin())
        a.open()
        b = self.make(FakeMarlin(), id="other")
        with self.assertRaises(adapters.AdapterError):
            b.open()
        a.disconnect()
        b.open()  # free again once the first one let go

    def test_stale_status_is_offline(self):
        a = self.make(FakeMarlin())
        a.open()
        a.last_line = time.time() - 60
        self.assertIsNone(a.get_status())

    def test_missing_pyserial_is_explained(self):
        import builtins
        real = builtins.__import__

        def no_serial(name, *args, **kw):
            if name == "serial":
                raise ImportError(name)
            return real(name, *args, **kw)
        builtins.__import__ = no_serial
        try:
            with self.assertRaises(adapters.AdapterError) as cm:
                ms.open_pyserial("COM7", 115200)
        finally:
            builtins.__import__ = real
        self.assertIn("pyserial", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
