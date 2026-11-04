import contextlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import centerhud  # noqa: E402

BUILD = "26C5035r"


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state_path = self.directory / "state" / "session.json"
        self.patch(centerhud, "STATE_PATH", self.state_path)
        self.agent = centerhud.Agent(123, "Mon Jan 1 00:00:00 2024", os.getuid())
        self.command = self.patch(centerhud, "command", side_effect=AssertionError("Unexpected subprocess"))
        self.kill = self.patch(centerhud.os, "kill", side_effect=AssertionError("Unexpected process signal"))
        self.patch(centerhud.time, "sleep")
        self.output = io.StringIO()
        context = contextlib.redirect_stdout(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def result(self, stdout="", code=0, stderr=""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def record(self, phase="installed"):
        return centerhud.record_for(self.agent, BUILD, phase)

    def save(self, state=None):
        centerhud.write_session(self.record() if state is None else state)

    def mock_current(self):
        self.patch(centerhud, "find_agent", return_value=self.agent)
        self.patch(centerhud, "os_build", return_value=BUILD)

    def mock_enable(self):
        self.mock_current()
        self.patch(centerhud, "ensure_supported", return_value=BUILD)
        self.patch(centerhud, "inspect_agent", return_value=self.agent)
        self.command.side_effect = None
        self.command.return_value = self.result(
            '(String) $R0 = "CENTERHUD_OVERRIDE_INSTALLED"\nProcess 123 detached\n'
        )

    def test_root_execution_is_refused(self):
        self.patch(centerhud.os, "getuid", return_value=0)
        self.patch(centerhud.os, "geteuid", return_value=0)
        with self.assertRaisesRegex(RuntimeError, "root"):
            centerhud.enable()
        self.assertFalse(self.state_path.parent.exists())
        self.command.assert_not_called()
        self.kill.assert_not_called()

    def test_process_identity_uses_uid_birth_and_exact_path(self):
        self.command.side_effect = None
        self.command.return_value = self.result(
            f"{self.agent.uid} Mon Jan  1 00:00:00 2024 {centerhud.AGENT_PATH}\n"
        )
        self.assertEqual(centerhud.inspect_agent(123), self.agent)
        self.assertEqual(self.command.call_args.args[0][0], "/bin/ps")

    def test_process_with_wrong_user_or_path_is_not_a_target(self):
        for uid, path in (
            (self.agent.uid + 1, centerhud.AGENT_PATH),
            (self.agent.uid, "/tmp/MenuBarAgent"),
        ):
            with self.subTest(uid=uid, path=path):
                self.command.side_effect = None
                self.command.return_value = self.result(f"{uid} Mon Jan 1 00:00:00 2024 {path}\n")
                self.assertIsNone(centerhud.inspect_agent(123))

    def test_failed_process_inspection_is_not_treated_as_exit(self):
        self.command.side_effect = None
        self.command.return_value = self.result(code=2, stderr="inspection failed")
        with self.assertRaisesRegex(RuntimeError, "inspect"):
            centerhud.inspect_agent(123)

    def test_exit_one_with_stderr_is_not_a_missing_process(self):
        self.mock_current()
        self.save()
        self.command.side_effect = None
        self.command.return_value = self.result(code=1, stderr="query denied")
        with self.assertRaisesRegex(RuntimeError, "inspect"):
            centerhud.disable()
        self.assertEqual(centerhud.read_session(), self.record())
        self.kill.assert_not_called()

    def test_enumeration_error_is_not_an_empty_process_list(self):
        self.command.side_effect = None
        self.command.return_value = self.result(code=1, stderr="query failed")
        with self.assertRaisesRegex(RuntimeError, "enumerate"):
            centerhud.find_agent()

    def test_multiple_matching_agents_are_rejected(self):
        self.command.side_effect = None
        self.command.return_value = self.result("123\n124\n")
        self.patch(centerhud, "inspect_agent", side_effect=[
            self.agent, centerhud.Agent(124, self.agent.birth, self.agent.uid)
        ])
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            centerhud.find_agent()
        self.assertEqual(
            self.command.call_args.args[0],
            ["/usr/bin/pgrep", "-u", str(self.agent.uid), "-x", "MenuBarAgent"],
        )

    def test_name_match_without_apple_path_is_ignored(self):
        self.command.side_effect = None
        self.command.return_value = self.result("123\n")
        self.patch(centerhud, "inspect_agent", return_value=None)
        self.assertIsNone(centerhud.find_agent())

    def test_targeted_signal_waits_for_original_identity_to_disappear(self):
        inspect = self.patch(centerhud, "inspect_agent", side_effect=[self.agent, None])
        self.kill.side_effect = None
        centerhud.stop_agent(self.agent)
        self.kill.assert_called_once_with(123, signal.SIGTERM)
        self.assertEqual(inspect.call_count, 2)

    def test_reused_pid_is_not_signaled(self):
        replacement = centerhud.Agent(123, "Tue Jan 2 00:00:00 2024", self.agent.uid)
        self.patch(centerhud, "inspect_agent", return_value=replacement)
        centerhud.stop_agent(self.agent)
        self.kill.assert_not_called()

    def test_signal_failure_does_not_report_success(self):
        self.patch(centerhud, "inspect_agent", return_value=self.agent)
        self.kill.side_effect = PermissionError("signal denied")
        with self.assertRaisesRegex(RuntimeError, "Could not stop"):
            centerhud.stop_agent(self.agent)

    def test_process_exit_timeout_is_reported(self):
        self.patch(centerhud, "inspect_agent", return_value=self.agent)
        self.kill.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "restoration is unconfirmed"):
            centerhud.stop_agent(self.agent, timeout=0)

    def test_malformed_session_data_is_rejected_without_type_errors(self):
        self.state_path.parent.mkdir()
        values = [None, [], "text", 1, {}, {"pid": 123}]
        for key, value in (
            ("pid", True), ("pid", -1), ("birth", []), ("birth", ""),
            ("build", {}), ("uid", True), ("phase", []), ("mode", "other"),
            ("version", True), ("extra", "unexpected"),
        ):
            candidate = self.record()
            candidate[key] = value
            values.append(candidate)
        for value in values:
            with self.subTest(value=value):
                self.state_path.write_text(json.dumps(value))
                self.assertEqual(centerhud.read_session(), {})
        for data in (b"{", b"\xff", b" " * 16385, b"[" * 2000):
            with self.subTest(data_type=data[:1]):
                self.state_path.write_bytes(data)
                self.assertEqual(centerhud.read_session(), {})

    def test_legacy_record_is_validated_without_rewriting_it(self):
        legacy = {"pid": 123, "birth": "Mon Jan  1 00:00:00 2024",
                  "build": BUILD, "mode": "builtin-notch"}
        self.state_path.parent.mkdir()
        original = json.dumps(legacy)
        self.state_path.write_text(original)
        state = centerhud.read_session()
        self.assertTrue(centerhud.session_matches(state, self.agent, BUILD))
        self.assertEqual(state["phase"], "installed")
        self.assertEqual(self.state_path.read_text(), original)

    def test_session_symlink_is_never_read_or_overwritten(self):
        self.state_path.parent.mkdir()
        target = self.directory / "unrelated.txt"
        target.write_text("keep")
        self.state_path.symlink_to(target)
        for operation in (centerhud.read_session, lambda: centerhud.write_session(self.record())):
            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                operation()
        self.assertEqual(target.read_text(), "keep")

    def test_session_directory_symlink_is_rejected(self):
        target = self.directory / "other"
        target.mkdir()
        self.state_path.parent.symlink_to(target, target_is_directory=True)
        for operation in (centerhud.read_session, lambda: centerhud.write_session(self.record())):
            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                operation()
        self.assertEqual(list(target.iterdir()), [])

    def test_session_write_is_private_and_atomic(self):
        original = self.record("pending")
        self.save(original)
        replace = os.replace
        calls = []

        def checked_replace(source, destination):
            calls.append((source, destination))
            self.assertEqual(json.loads(self.state_path.read_text()), original)
            self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            replace(source, destination)

        self.patch(centerhud.os, "replace", side_effect=checked_replace)
        centerhud.write_session(self.record())
        self.assertEqual(len(calls), 1)
        self.assertEqual(centerhud.read_session(), self.record())
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_path.parent.stat().st_mode), 0o700)
        self.assertEqual(list(self.state_path.parent.glob(".session-*.tmp")), [])

    def test_atomic_replace_failure_preserves_previous_record(self):
        original = self.record("pending")
        self.save(original)
        self.patch(centerhud.os, "replace", side_effect=PermissionError("replace denied"))
        with self.assertRaises(PermissionError):
            centerhud.write_session(self.record())
        self.assertEqual(centerhud.read_session(), original)
        self.assertEqual(list(self.state_path.parent.glob(".session-*.tmp")), [])

    def test_overlapping_mutation_is_refused_without_waiting(self):
        with centerhud.mutation_lock():
            with self.assertRaisesRegex(RuntimeError, "in progress"):
                with centerhud.mutation_lock():
                    self.fail("The second operation acquired the lock")

    def test_lock_symlink_is_not_followed(self):
        self.state_path.parent.mkdir()
        target = self.directory / "unrelated.txt"
        target.write_text("keep")
        (self.state_path.parent / "session.lock").symlink_to(target)
        with self.assertRaises(OSError):
            with centerhud.mutation_lock():
                self.fail("A symbolic-link lock was accepted")
        self.assertEqual(target.read_text(), "keep")

    def test_unsupported_build_is_rejected_before_tool_checks(self):
        self.patch(centerhud, "os_build", return_value="UNSUPPORTED0")
        with self.assertRaisesRegex(RuntimeError, "not allowlisted"):
            centerhud.ensure_supported()
        self.command.assert_not_called()

    def test_failed_sip_check_does_not_grant_permission(self):
        self.patch(centerhud, "os_build", return_value=BUILD)
        self.command.side_effect = [
            self.result("/usr/bin/lldb\n"),
            self.result("Debugging Restrictions: disabled\n", code=1),
        ]
        with self.assertRaisesRegex(RuntimeError, "configuration was not confirmed"):
            centerhud.ensure_supported()

    def test_dry_run_creates_no_state_or_lock(self):
        self.mock_enable()
        centerhud.enable(dry_run=True)
        self.assertFalse(self.state_path.parent.exists())
        self.command.assert_not_called()
        self.kill.assert_not_called()

    def test_doctor_and_status_do_not_create_state_or_locks(self):
        self.mock_current()
        self.command.side_effect = None
        self.command.return_value = self.result("Debugging Restrictions: disabled\n")
        centerhud.check()
        centerhud.tracked_status()
        self.assertFalse(self.state_path.parent.exists())
        self.kill.assert_not_called()

    def test_disable_without_matching_record_changes_nothing(self):
        self.mock_current()
        centerhud.disable()
        self.assertFalse(self.state_path.parent.exists())
        self.command.assert_not_called()
        self.kill.assert_not_called()

    def test_disable_keeps_record_when_termination_fails(self):
        self.mock_current()
        self.save()
        self.patch(centerhud, "inspect_agent", return_value=self.agent)
        self.kill.side_effect = PermissionError("signal denied")
        with self.assertRaisesRegex(RuntimeError, "Could not stop"):
            centerhud.disable()
        self.assertEqual(centerhud.read_session(), self.record())
        self.assertNotIn("no longer running", self.output.getvalue())

    def test_disable_does_not_touch_a_replacement_process(self):
        self.save()
        replacement = centerhud.Agent(123, "Tue Jan 2 00:00:00 2024", self.agent.uid)
        self.patch(centerhud, "find_agent", side_effect=[self.agent, replacement])
        self.patch(centerhud, "os_build", return_value=BUILD)
        stop = self.patch(centerhud, "stop_agent")
        centerhud.disable()
        stop.assert_not_called()
        self.assertEqual(centerhud.read_session(), self.record())

    def test_disable_removes_record_only_after_original_identity_is_gone(self):
        self.mock_current()
        self.save()
        self.patch(centerhud, "inspect_agent", side_effect=[self.agent, None])
        self.kill.side_effect = None
        centerhud.disable()
        self.kill.assert_called_once_with(123, signal.SIGTERM)
        self.assertFalse(self.state_path.exists())
        self.assertIn("no longer running", self.output.getvalue())
        self.assertIn("visual recovery were not checked", self.output.getvalue())

    def test_pending_record_precedes_attachment_and_success_updates_it(self):
        self.mock_enable()

        def attach(args, timeout):
            self.assertEqual(centerhud.read_session(), self.record("pending"))
            self.assertEqual(args[0], "/usr/bin/xcrun")
            self.assertIn("--no-lldbinit", args)
            return self.result('(String) $R0 = "CENTERHUD_OVERRIDE_INSTALLED"\nProcess 123 detached\n')

        self.command.side_effect = attach
        centerhud.enable()
        self.assertEqual(centerhud.read_session(), self.record())
        self.kill.assert_not_called()
        self.assertIn("visibility and interactions were not checked", self.output.getvalue())

    def test_state_write_failure_prevents_attachment(self):
        self.mock_enable()
        self.patch(centerhud, "write_session", side_effect=PermissionError("write denied"))
        with self.assertRaises(PermissionError):
            centerhud.enable()
        self.command.assert_not_called()
        self.kill.assert_not_called()

    def test_missing_detach_confirmation_triggers_recovery(self):
        self.mock_enable()
        self.command.return_value = self.result('(String) $R0 = "CENTERHUD_OVERRIDE_INSTALLED"\n')
        stop = self.patch(centerhud, "stop_agent")
        with self.assertRaisesRegex(RuntimeError, "detachment"):
            centerhud.enable()
        stop.assert_called_once_with(self.agent)
        self.assertFalse(self.state_path.exists())

    def test_echoed_expression_is_not_accepted_as_success(self):
        self.mock_enable()
        self.command.return_value = self.result(centerhud.SWIFT_EXPRESSION + "\nProcess 123 detached\n")
        stop = self.patch(centerhud, "stop_agent")
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            centerhud.enable()
        stop.assert_called_once_with(self.agent)
        self.assertFalse(self.state_path.exists())

    def test_expression_failure_reports_actual_error_without_echoing_source(self):
        self.mock_enable()
        self.command.return_value = self.result(
            stdout=centerhud.SWIFT_EXPRESSION + "\nerror: Expression execution was interrupted: EXC_BAD_ACCESS\n",
            code=1,
        )
        stop = self.patch(centerhud, "stop_agent")
        with self.assertRaisesRegex(RuntimeError, "EXC_BAD_ACCESS") as raised:
            centerhud.enable()
        self.assertNotIn("import AppKit", str(raised.exception))
        stop.assert_called_once_with(self.agent)
        self.assertFalse(self.state_path.exists())

    def test_confirmed_precondition_rejection_does_not_restart_agent(self):
        self.mock_enable()
        stop = self.patch(centerhud, "stop_agent")
        for token in centerhud.NO_CHANGE_RESULTS:
            with self.subTest(token=token):
                self.command.return_value = self.result(f'(String) $R0 = "{token}"\nProcess 123 detached\n')
                with self.assertRaisesRegex(RuntimeError, "no methods were replaced"):
                    centerhud.enable()
                self.assertFalse(self.state_path.exists())
        stop.assert_not_called()
        self.kill.assert_not_called()

    def test_failed_rollback_preserves_pending_record_and_reports_both_failures(self):
        self.mock_enable()
        self.command.return_value = self.result(code=1, stderr="expression failed")
        self.patch(centerhud, "stop_agent", side_effect=RuntimeError("signal denied"))
        with self.assertRaisesRegex(RuntimeError, "(?s)Attempt failed.*Recovery failed.*record retained"):
            centerhud.enable()
        self.assertEqual(centerhud.read_session(), self.record("pending"))

    def test_attachment_timeout_attempts_only_original_identity_recovery(self):
        self.mock_enable()
        self.command.side_effect = subprocess.TimeoutExpired(["lldb"], 20)
        stop = self.patch(centerhud, "stop_agent")
        with self.assertRaises(subprocess.TimeoutExpired):
            centerhud.enable()
        stop.assert_called_once_with(self.agent)
        self.assertFalse(self.state_path.exists())

    def test_replaced_process_before_attachment_is_not_touched(self):
        self.mock_enable()
        replacement = centerhud.Agent(123, "Tue Jan 2 00:00:00 2024", self.agent.uid)
        self.patch(centerhud, "inspect_agent", return_value=replacement)
        stop = self.patch(centerhud, "stop_agent")
        with self.assertRaisesRegex(RuntimeError, "changed before attachment"):
            centerhud.enable()
        self.command.assert_not_called()
        stop.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_installed_record_write_failure_recovers_original_process(self):
        self.mock_enable()
        write = centerhud.write_session
        writes = []

        def write_or_fail(state):
            writes.append(state["phase"])
            if state["phase"] == "installed":
                raise PermissionError("write denied")
            write(state)

        self.patch(centerhud, "write_session", side_effect=write_or_fail)
        stop = self.patch(centerhud, "stop_agent")
        with self.assertRaises(PermissionError):
            centerhud.enable()
        self.assertEqual(writes, ["pending", "installed"])
        stop.assert_called_once_with(self.agent)
        self.assertFalse(self.state_path.exists())

    def test_pending_record_blocks_another_enable_attempt(self):
        self.mock_enable()
        self.save(self.record("pending"))
        with self.assertRaisesRegex(RuntimeError, "incomplete attempt"):
            centerhud.enable()
        self.command.assert_not_called()
        self.kill.assert_not_called()
        self.assertEqual(centerhud.read_session(), self.record("pending"))

    def test_invalid_record_is_not_silently_overwritten(self):
        self.mock_enable()
        self.state_path.parent.mkdir()
        self.state_path.write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "record is invalid"):
            centerhud.enable()
        self.assertEqual(self.state_path.read_text(), "[]")
        self.command.assert_not_called()
        self.kill.assert_not_called()

    def test_status_describes_tracking_without_claiming_runtime_validation(self):
        self.mock_current()
        self.save()
        before = self.state_path.read_bytes()
        centerhud.tracked_status()
        self.assertIn("TRACKED installed", self.output.getvalue())
        self.assertIn("not checked", self.output.getvalue())
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertFalse((self.state_path.parent / "session.lock").exists())
        self.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
