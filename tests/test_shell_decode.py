"""Вывод команд в чужих кодировках не роняет тул bash (Windows: dir в cp866)."""

import sys
import unittest

from luna.tools.shell import _decode


class DecodeTest(unittest.TestCase):
    def test_utf8_and_crlf(self):
        self.assertEqual(_decode("привет\r\nмир".encode("utf-8")), "привет\nмир")

    def test_never_raises_on_foreign_bytes(self):
        text = _decode("Том в устройстве".encode("cp866") + b"\x98\xff")
        self.assertIsInstance(text, str)

    @unittest.skipUnless(sys.platform == "win32", "OEM-кодировка есть только на Windows")
    def test_oem_codepage_on_windows(self):
        import ctypes

        oem = "cp" + str(ctypes.windll.kernel32.GetOEMCP())
        sample = "Том в устройстве" if oem == "cp866" else "Volume in drive"
        self.assertEqual(_decode(sample.encode(oem)), sample)


if __name__ == "__main__":
    unittest.main()
