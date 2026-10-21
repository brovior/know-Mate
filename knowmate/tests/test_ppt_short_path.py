"""8.3 registration paths retain the PowerPoint executable trust checks."""
from __future__ import annotations

import ctypes
import io
import os
from pathlib import Path, PureWindowsPath
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from knowmate.secure import office_guard, ppt_installation, ppt_launch, ppt_shutdown


SHORT = r"c:\PROGRA~1\MICROS~1\Office16\POWERPNT.EXE"
LONG = r"C:\Program Files\Microsoft Office\Office16\POWERPNT.EXE"


class WindowsPath(PureWindowsPath):
    """Windows lexical paths with explicit fake filesystem metadata and PE bytes."""

    links: set[str] = set()
    symlinks: set[str] = set()
    missing: set[str] = set()
    checked: list[str] = []
    opened: list[str] = []
    machine = 0x8664

    def is_file(self):
        return str(self).lower() not in self.missing

    def is_symlink(self):
        return str(self).lower() in self.symlinks

    def lstat(self):
        name = str(self).lower()
        self.checked.append(name)
        return SimpleNamespace(st_file_attributes=0x400 if name in self.links else 0)

    def stat(self):
        # Following a junction would conceal the reparse bit: only lstat sees it.
        return SimpleNamespace(st_file_attributes=0, st_size=128)

    def open(self, mode):
        assert mode == "rb"
        self.opened.append(str(self).lower())
        image = bytearray(128)
        image[:2] = b"MZ"
        struct.pack_into("<I", image, 0x3C, 64)
        image[64:68] = b"PE\0\0"
        struct.pack_into("<H", image, 68, self.machine)
        return io.BytesIO(image)


class ShortPathTrustTests(unittest.TestCase):
    def setUp(self):
        WindowsPath.links = set()
        WindowsPath.symlinks = set()
        WindowsPath.missing = set()
        WindowsPath.checked = []
        WindowsPath.opened = []
        WindowsPath.machine = 0x8664
        self.raw, self.kind = SHORT, 1
        key = Mock()
        key.__enter__ = Mock(return_value=key)
        key.__exit__ = Mock(return_value=False)
        self.registry = SimpleNamespace(
            HKEY_LOCAL_MACHINE="HKLM", KEY_READ=1, KEY_WOW64_64KEY=256,
            KEY_WOW64_32KEY=512, REG_SZ=1, OpenKey=Mock(return_value=key),
            QueryValueEx=lambda *_: (self.raw, self.kind))
        self.expand = Mock(side_effect=self.native_expand)
        self.kernel = SimpleNamespace(GetLongPathNameW=self.expand)
        for context in (
            patch.dict(sys.modules, {"winreg": self.registry}),
            patch.object(ppt_launch, "Path", WindowsPath),
            patch.object(ppt_installation, "_known_folder", side_effect=lambda guid: WindowsPath(
                r"C:\Program Files" if guid.startswith("905") else r"C:\Program Files (x86)")),
            patch.object(ctypes, "WinDLL", create=True, return_value=self.kernel),
        ):
            context.start()
            self.addCleanup(context.stop)

    @staticmethod
    def native_expand(source, buffer, capacity):
        buffer.value = LONG
        return len(LONG)

    def test_short_alias_expands_before_containment_and_original_pe_is_checked(self):
        self.assertEqual(ppt_launch.trusted_powerpoint_path(64), WindowsPath(LONG))
        self.assertEqual(WindowsPath.opened, [SHORT.lower()])
        self.assertIn(str(WindowsPath(SHORT).parent).lower(), WindowsPath.checked)
        self.assertIn(str(WindowsPath(LONG).parent).lower(), WindowsPath.checked)
        self.assertEqual(self.registry.OpenKey.call_args.args[0], "HKLM")
        self.assertEqual(self.registry.OpenKey.call_args.args[3], 257)

    def test_normal_long_path_needs_no_native_api(self):
        self.raw = LONG
        self.assertEqual(ppt_launch.trusted_powerpoint_path(64), WindowsPath(LONG))
        self.expand.assert_not_called()

    def test_quoted_registry_alias_is_accepted_but_missing_original_is_not(self):
        self.raw = '"' + SHORT + '"'
        self.assertEqual(ppt_launch.trusted_powerpoint_path(64), WindowsPath(LONG))
        WindowsPath.missing.add(SHORT.lower())
        with self.assertRaisesRegex(ValueError, "office_path"):
            ppt_launch.trusted_powerpoint_path(64)

    def test_both_registry_views_keep_their_pe_check(self):
        WindowsPath.machine = 0x14C
        self.assertEqual(ppt_launch.trusted_powerpoint_path(32), WindowsPath(LONG))
        self.assertEqual(self.registry.OpenKey.call_args.args[3], 513)
        with self.assertRaisesRegex(ValueError, "office_bitness"):
            ppt_launch.trusted_powerpoint_path(64)

    def test_original_and_expanded_ancestors_reject_junction_and_symlink(self):
        for path in (WindowsPath(SHORT), WindowsPath(LONG)):
            for target in (path, path.parent):
                for marker in (WindowsPath.links, WindowsPath.symlinks):
                    with self.subTest(target=str(target), marker="junction" if marker is WindowsPath.links else "symlink"):
                        marker.add(str(target).lower())
                        with self.assertRaisesRegex(ValueError, "office_path_link"):
                            ppt_launch.trusted_powerpoint_path(64)
                        marker.clear()

    def test_original_link_is_rejected_before_native_expansion(self):
        WindowsPath.links.add(str(WindowsPath(SHORT).parent).lower())
        with self.assertRaisesRegex(ValueError, "office_path_link"):
            ppt_launch.trusted_powerpoint_path(64)
        self.expand.assert_not_called()

    def test_expanded_path_still_requires_trusted_root_name_absolute_and_file(self):
        for output in (r"C:\Users\Office\POWERPNT.EXE", r"C:\Program Files\Office\other.exe",
                       r"Program Files\POWERPNT.EXE", r"C:\Program FilesExtra\POWERPNT.EXE",
                       r"C:\Program Files\..\Users\POWERPNT.EXE"):
            with self.subTest(output=output):
                self.expand.side_effect = lambda source, buffer, capacity: self.write_result(buffer, output)
                with self.assertRaisesRegex(ValueError, "office_path"):
                    ppt_launch.trusted_powerpoint_path(64)
        self.expand.side_effect = self.native_expand
        WindowsPath.missing.add(LONG.lower())
        with self.assertRaisesRegex(ValueError, "office_path"):
            ppt_launch.trusted_powerpoint_path(64)

    @staticmethod
    def write_result(buffer, value):
        buffer.value = value
        return len(value.encode("utf-16-le")) // 2

    def test_native_length_uses_utf16_units_for_non_bmp_names(self):
        output = r"C:\Program Files\Office 📊\POWERPNT.EXE"
        self.expand.side_effect = lambda source, buffer, capacity: self.write_result(buffer, output)
        self.assertEqual(ppt_launch.trusted_powerpoint_path(64), WindowsPath(output))

    def test_registry_and_original_path_checks_remain(self):
        for raw, kind in ((SHORT, 2), (123, 1), (r"PROGRA~1\POWERPNT.EXE", 1),
                          (SHORT + " /unsafe", 1), (r"C:\PROGRA~1\..\Users\POWERPNT.EXE", 1)):
            with self.subTest(raw=raw, kind=kind):
                self.raw, self.kind = raw, kind
                with self.assertRaisesRegex(ValueError, "^office_path$"):
                    ppt_launch.trusted_powerpoint_path(64)
        self.expand.assert_not_called()

    def test_native_abi(self):
        from ctypes import wintypes
        ppt_launch._expand_short_path(WindowsPath(SHORT))
        self.assertEqual(self.expand.argtypes, [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD])
        self.assertIs(self.expand.restype, wintypes.DWORD)

    def test_native_failure_and_malformed_response_fail_closed(self):
        for response in (0, -1, 32768, len(LONG) + 1):
            with self.subTest(response=response):
                self.expand.side_effect = lambda source, buffer, capacity: self.write_result(buffer, LONG) and response
                with self.assertRaisesRegex(ValueError, "^office_path_expansion$"):
                    ppt_launch.trusted_powerpoint_path(64)
        self.expand.side_effect = OSError("native error")
        with self.assertRaises(OSError):
            ppt_launch.trusted_powerpoint_path(64)

    def test_native_resize_and_unstable_size_fail_closed(self):
        calls = []
        output = "C:\\Program Files\\" + "a" * 270 + "\\POWERPNT.EXE"
        def expand(source, buffer, capacity):
            calls.append(capacity)
            if capacity <= len(output):
                return len(output) + 1
            return self.write_result(buffer, output)
        self.expand.side_effect = expand
        self.assertEqual(ppt_launch._expand_short_path(WindowsPath(SHORT)), WindowsPath(output))
        self.assertEqual(calls, [260, len(output) + 1])
        self.expand.side_effect = lambda source, buffer, capacity: capacity + 1
        with self.assertRaisesRegex(ValueError, "^office_path_expansion$"):
            ppt_launch._expand_short_path(WindowsPath(SHORT))


class PathDecisionLogTests(unittest.TestCase):
    """Rejected launch paths explain the failure without echoing exception text."""

    def test_fallback_logs_fixed_detail_and_does_not_start_process(self):
        os_error = OSError("PRIVATE_PATH_FROM_OS")
        os_error.winerror = 5
        for error, expected in (
            (ValueError("office_path_expansion"), "detail=office_path_expansion error_type=ValueError winerror=None"),
            (ValueError("PRIVATE_PATH_FROM_EXCEPTION"), "detail=validation_failed error_type=ValueError winerror=None"),
            (os_error, "detail=os_error error_type=OSError winerror=5"),
            (ImportError("PRIVATE_DEPENDENCY_PATH"), "detail=dependency_unavailable error_type=ImportError winerror=None"),
        ):
            with self.subTest(error=type(error).__name__), patch.object(
                    office_guard, "ensure_application_open"), patch.object(
                    ppt_shutdown, "ensure_admitted"), patch.object(
                    ppt_launch, "trusted_powerpoint_path", side_effect=error), patch.object(
                    ppt_launch, "LaunchedPowerPoint") as launch, patch.object(
                    office_guard, "prepare_powerpoint_dispatch") as prepare, self.assertLogs(
                    ppt_shutdown.logger, level="INFO") as logs:
                self.assertIsNone(ppt_shutdown.launch_and_bind(Mock(), 64))
                launch.assert_not_called()
                prepare.assert_not_called()
            output = "\n".join(logs.output)
            self.assertIn("path=dispatch reason=untrusted_path", output)
            self.assertIn(expected, output)
            self.assertNotIn("PRIVATE_", output)


class LongPathFilesystemTests(unittest.TestCase):
    def test_normal_path_with_real_file_and_pe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "POWERPNT.EXE"
            image = bytearray(128)
            image[:2] = b"MZ"
            struct.pack_into("<I", image, 0x3C, 64)
            image[64:68] = b"PE\0\0"
            struct.pack_into("<H", image, 68, 0x8664)
            executable.write_bytes(image)
            key = Mock()
            key.__enter__ = Mock(return_value=key)
            key.__exit__ = Mock(return_value=False)
            registry = SimpleNamespace(
                HKEY_LOCAL_MACHINE="HKLM", KEY_READ=1, KEY_WOW64_64KEY=256,
                KEY_WOW64_32KEY=512, REG_SZ=1, OpenKey=Mock(return_value=key),
                QueryValueEx=lambda *_: (str(executable), 1))
            with patch.dict(sys.modules, {"winreg": registry}), patch.object(
                    ppt_installation, "_known_folder", return_value=root):
                self.assertEqual(ppt_launch.trusted_powerpoint_path(64), executable)
                with self.assertRaisesRegex(ValueError, "office_bitness"):
                    ppt_launch.trusted_powerpoint_path(32)


@unittest.skipUnless(os.name == "nt", "Windows native filesystem required")
class NativeShortPathTests(unittest.TestCase):
    def test_real_filesystem_alias_expansion_without_office(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        short_name = kernel.GetShortPathNameW
        short_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        short_name.restype = wintypes.DWORD
        with tempfile.TemporaryDirectory(prefix="Aegis Short Path ") as temporary:
            executable = Path(temporary) / "Office Long Folder" / "POWERPNT.EXE"
            executable.parent.mkdir()
            executable.write_bytes(b"fixture, not an executable")
            buffer = ctypes.create_unicode_buffer(32768)
            size = short_name(str(executable), buffer, len(buffer))
            if not 0 < size < len(buffer) or "~" not in buffer.value:
                self.skipTest("8.3 aliases unavailable on this volume")
            self.assertEqual(ppt_launch._expand_short_path(Path(buffer.value)), executable)


if __name__ == "__main__":
    unittest.main()
