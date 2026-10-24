"""Regression checks on synthetic endpoints; no Desktop or network calls."""
from dataclasses import replace
from pathlib import PureWindowsPath
import unittest
from unittest.mock import Mock

import native_discovery as discovery


PIPE_A = discovery.PIPE_PREFIX + "11111111-1111-4111-8111-111111111111"
PIPE_B = discovery.PIPE_PREFIX + "22222222-2222-4222-8222-222222222222"
PACKAGE_NAME = "OpenAI.Codex_26.1013.4894.0_x64__2p2nqsd0c76g0"
PACKAGE_PATH = "C:\\Program Files\\WindowsApps\\" + PACKAGE_NAME
IDENTITY = discovery.PipeIdentity(
    PACKAGE_PATH + "\\app\\ChatGPT.exe", discovery.PACKAGE_FAMILY,
    PACKAGE_NAME, PACKAGE_PATH, 3, 3,
)


class FakeBackend:
    def __init__(self, rows, *, files=True):
        self.rows = rows
        self.files = files
        self.inspected = []
        self.nodes = []

    def enumerate_pipe_paths(self):
        return list(self.rows)

    def inspect_pipe(self, path):
        self.inspected.append(path)
        value = self.rows[path]
        if isinstance(value, Exception):
            raise value
        return value

    def is_file(self, path):
        self.nodes.append(path)
        return self.files


class NativeDiscoveryTests(unittest.TestCase):
    def discover(self, backend):
        return discovery.discover_environment(backend=backend)

    def assert_error(self, code, backend):
        with self.assertRaises(discovery.DiscoveryError) as raised:
            self.discover(backend)
        self.assertEqual(code, str(raised.exception))
        self.assertNotIn(PIPE_A, str(raised.exception))
        return raised.exception

    def test_single_registered_same_session_package_returns_three_overrides(self):
        backend = FakeBackend({PIPE_A: IDENTITY})
        environment = self.discover(backend)
        self.assertEqual({
            "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_MCP_NODE_PATH", "CODEX_THREAD_ID",
        }, set(environment))
        self.assertEqual(PIPE_A, environment["CODEX_APP_TOOLS_PIPE_PATH"])
        self.assertEqual(discovery.SERVICE_THREAD_ID, environment["CODEX_THREAD_ID"])
        self.assertEqual(
            PureWindowsPath(PACKAGE_PATH) / "app/resources/cua_node/bin/node.exe",
            PureWindowsPath(environment["CODEX_MCP_NODE_PATH"]),
        )

    def test_second_discovery_uses_new_pipe_without_saved_path(self):
        first = self.discover(FakeBackend({PIPE_A: IDENTITY}))
        second = self.discover(FakeBackend({PIPE_B: IDENTITY}))
        self.assertNotEqual(first["CODEX_APP_TOOLS_PIPE_PATH"], second["CODEX_APP_TOOLS_PIPE_PATH"])

    def test_no_desktop_fails_without_fallback(self):
        self.assert_error("native_desktop_unavailable", FakeBackend({}))

    def test_multiple_verified_instances_fail_closed(self):
        self.assert_error(
            "native_desktop_ambiguous", FakeBackend({PIPE_A: IDENTITY, PIPE_B: IDENTITY})
        )

    def test_unverified_candidate_does_not_displace_verified(self):
        result = self.discover(FakeBackend({PIPE_A: None, PIPE_B: IDENTITY}))
        self.assertEqual(PIPE_B, result["CODEX_APP_TOOLS_PIPE_PATH"])

    def test_unknown_pipe_names_are_not_opened(self):
        backend = FakeBackend({
            "\\\\.\\pipe\\some-other-application": IDENTITY,
            discovery.PIPE_PREFIX + "not-a-guid": IDENTITY,
            PIPE_A + "\\extra": IDENTITY,
            PIPE_A: IDENTITY,
        })
        self.discover(backend)
        self.assertEqual([PIPE_A], backend.inspected)

    def test_fake_image_path_without_registered_openai_package_rejected(self):
        self.assert_error(
            "native_desktop_unavailable",
            FakeBackend({PIPE_A: replace(IDENTITY, package_family="Other.App_publisher")}),
        )

    def test_process_image_must_be_expected_executable_in_registered_package(self):
        for path in (
            "C:\\Temp\\ChatGPT.exe",
            PACKAGE_PATH + "\\app\\other.exe",
            PACKAGE_PATH + "\\app\\subfolder\\ChatGPT.exe",
            PACKAGE_PATH + "\\app\\..\\app\\ChatGPT.exe",
        ):
            with self.subTest(path=path):
                self.assert_error(
                    "native_desktop_unavailable",
                    FakeBackend({PIPE_A: replace(IDENTITY, image_path=path)}),
                )

    def test_package_path_must_match_package_full_name(self):
        self.assert_error(
            "native_desktop_unavailable",
            FakeBackend({PIPE_A: replace(IDENTITY, package_path="C:\\Temp\\OtherPackage")}),
        )

    def test_network_and_relative_package_paths_rejected(self):
        for package in ("\\\\remote\\share\\" + PACKAGE_NAME, PACKAGE_NAME):
            with self.subTest(package=package):
                identity = replace(
                    IDENTITY, package_path=package,
                    image_path=str(PureWindowsPath(package) / "app/ChatGPT.exe"),
                )
                self.assert_error("native_desktop_unavailable", FakeBackend({PIPE_A: identity}))

    def test_package_publisher_full_name_mismatch_rejected(self):
        self.assert_error(
            "native_desktop_unavailable",
            FakeBackend({PIPE_A: replace(IDENTITY, package_full_name=PACKAGE_NAME + "bad")}),
        )

    def test_different_windows_session_is_rejected(self):
        self.assert_error(
            "native_desktop_unavailable",
            FakeBackend({PIPE_A: replace(IDENTITY, server_session_id=4)}),
        )

    def test_missing_bundled_node_does_not_use_system_path(self):
        self.assert_error("native_desktop_unavailable", FakeBackend({PIPE_A: IDENTITY}, files=False))

    def test_os_error_is_sanitized(self):
        error = self.assert_error(
            "native_discovery_failed", FakeBackend({PIPE_A: OSError("failed at " + PIPE_A)})
        )
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_only_known_existing_service_context_is_allowed(self):
        backend = FakeBackend({PIPE_A: IDENTITY})
        with self.assertRaisesRegex(discovery.DiscoveryError, "caller_context_mismatch"):
            discovery.discover_environment(caller_thread_id="arbitrary-other-thread", backend=backend)
        self.assertEqual([], backend.inspected)

    def test_candidate_limit_precedes_inspection(self):
        rows = {discovery.PIPE_PREFIX + str(i): IDENTITY for i in range(65)}
        backend = FakeBackend(rows)
        self.assert_error("native_discovery_candidate_limit", backend)
        self.assertEqual([], backend.inspected)

    def test_case_insensitive_duplicate_pipe_not_ambiguous(self):
        backend = FakeBackend({PIPE_A: IDENTITY, PIPE_A.upper(): IDENTITY})
        result = self.discover(backend)
        self.assertEqual(PIPE_A.casefold(), result["CODEX_APP_TOOLS_PIPE_PATH"].casefold())

    def test_identity_repr_excludes_paths_and_package_name(self):
        value = repr(IDENTITY)
        self.assertNotIn(PACKAGE_PATH, value)
        self.assertNotIn(PACKAGE_NAME, value)


class InstalledImageDiscoveryTests(unittest.TestCase):
    """Exercise Windows identity queries with synthetic process/API handles."""

    def backend(self, *, image=None, family_error=15700, full_error=15700,
                installed_path=PACKAGE_PATH, derived_family=discovery.PACKAGE_FAMILY,
                session=3):
        backend = object.__new__(discovery._WindowsBackend)
        kernel = Mock()
        backend._kernel = kernel
        kernel.GetCurrentProcessId.return_value = 99
        kernel.OpenProcess.return_value = 1001

        def process_session(pid, target):
            target._obj.value = 3 if pid == 99 else session
            return True

        def image_name(process, flags, target, size):
            target.value = image or IDENTITY.image_path
            return True

        def text_query(error, value):
            def query(subject, size, target):
                if error:
                    return error
                if target is None:
                    size._obj.value = len(value) + 1
                    return 122
                target.value = value
                return 0
            return Mock(side_effect=query)

        kernel.ProcessIdToSessionId.side_effect = process_session
        kernel.QueryFullProcessImageNameW.side_effect = image_name
        kernel.GetPackageFamilyName = text_query(family_error, discovery.PACKAGE_FAMILY)
        kernel.GetPackageFullName = text_query(full_error, PACKAGE_NAME)
        kernel.PackageFamilyNameFromFullName = text_query(0, derived_family)
        kernel.GetPackagePathByFullName = text_query(0 if installed_path else 15700,
                                                    installed_path or "")
        return backend, kernel

    def test_official_installed_image_without_process_package_identity_is_verified(self):
        backend, kernel = self.backend()
        self.assertEqual(backend.inspect_process(2001), IDENTITY)
        self.assertEqual(kernel.PackageFamilyNameFromFullName.call_args.args[0], PACKAGE_NAME)
        self.assertEqual(kernel.GetPackagePathByFullName.call_args.args[0], PACKAGE_NAME)
        kernel.CloseHandle.assert_called_once_with(1001)

    def test_registered_process_path_keeps_original_verification(self):
        backend, kernel = self.backend(family_error=0, full_error=0)
        self.assertEqual(backend.inspect_process(2001), IDENTITY)
        kernel.PackageFamilyNameFromFullName.assert_not_called()

    def test_missing_registration_or_different_installed_path_fails_closed(self):
        for installed in (None, "C:\\Program Files\\WindowsApps\\OtherPackage",
                          "C:\\Temp\\" + PACKAGE_NAME):
            with self.subTest(installed=installed):
                backend, _ = self.backend(installed_path=installed)
                self.assertIsNone(backend.inspect_process(2001))

    def test_basename_copy_nested_executable_traversal_and_other_publisher_are_rejected(self):
        for image in ("C:\\Temp\\ChatGPT.exe", "C:\\Temp\\" + PACKAGE_NAME + "\\app\\ChatGPT.exe",
                      PACKAGE_PATH + "\\app\\sub\\ChatGPT.exe",
                      PACKAGE_PATH + "\\app\\..\\app\\ChatGPT.exe",
                      PACKAGE_PATH.replace("2p2nqsd0c76g0", "foreignpublisher") + "\\app\\ChatGPT.exe",
                      "\\\\server\\WindowsApps\\" + PACKAGE_NAME + "\\app\\ChatGPT.exe"):
            with self.subTest(image=image):
                backend, kernel = self.backend(image=image)
                self.assertIsNone(backend.inspect_process(2001))
                kernel.GetPackagePathByFullName.assert_not_called()

    def test_derived_wrong_family_cannot_use_installed_package_path(self):
        backend, kernel = self.backend(derived_family="Other.App_publisher")
        self.assertIsNone(backend.inspect_process(2001))
        kernel.GetPackagePathByFullName.assert_not_called()

    def test_other_api_errors_or_partial_identity_do_not_trigger_fallback(self):
        for errors in ((5, 5), (15700, 5), (5, 15700), (0, 15700), (15700, 0)):
            with self.subTest(errors=errors):
                backend, kernel = self.backend(family_error=errors[0], full_error=errors[1])
                self.assertIsNone(backend.inspect_process(2001))
                kernel.PackageFamilyNameFromFullName.assert_not_called()

    def test_other_windows_session_is_rejected_before_image_identity(self):
        backend, kernel = self.backend(session=4)
        self.assertIsNone(backend.inspect_process(2001))
        kernel.OpenProcess.assert_not_called()


if __name__ == "__main__":
    unittest.main()
