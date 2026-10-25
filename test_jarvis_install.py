"""Setup tests use temporary roots, dummy credentials and mocked processes."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import jarvis_install as install
import jarvis_settings as settings


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("jarvis_probe.py", "requirements.txt", "desktop_controller.py", "main.py"):
            (self.root / name).write_text("", encoding="utf-8")

    def value(self):
        return {"version": 1, "project_root": str(self.root), "local_model": "some-local-model:7b",
                "skill_targets": {"open_jarvis": str(self.root), "open_wechat": str(self.root / "Weixin.exe")}}

    def test_absent_settings_keep_legacy_defaults(self):
        self.assertEqual(settings.load_local_settings(self.root), {"version": 1})

    def test_save_and_update_back_up_only_non_secret_settings(self):
        settings.save_local_settings(self.root, self.value())
        value = self.value(); value["local_model"] = "another-local:7b"
        settings.save_local_settings(self.root, value)
        self.assertEqual(settings.load_local_settings(self.root), value)
        self.assertEqual(len(list((self.root / "backups").glob("setup-*.json"))), 1)
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse((self.root / "openai.env").exists())

    def test_credentials_or_server_urls_never_enter_local_settings(self):
        for name in ("OPENAI_API_KEY", "JARVIS_API_KEY", "ollama_url", "commands", "api_key"):
            value = self.value(); value[name] = "test-only-placeholder"
            with self.subTest(name=name), self.assertRaises(settings.SettingsError):
                settings.save_local_settings(self.root, value)
        self.assertFalse((self.root / settings.LOCAL_NAME).exists())

    def test_app_override_cannot_create_skill_or_shell_target(self):
        for overrides in ({"run_anything": str(self.root / "cmd.exe")}, {"open_wechat": str(self.root / "cmd.exe")},
                          {"open_calculator": str(self.root / "powershell.exe")}, {"open_notepad": "relative.exe"}):
            value = self.value(); value["skill_targets"] = overrides
            with self.subTest(overrides=overrides), self.assertRaises(settings.SettingsError):
                settings.validate_settings(value)

    def test_invalid_model_and_relative_or_network_root_rejected(self):
        for bad in ("model\ncommand", "model;command", "", "x" * 150):
            value = self.value(); value["local_model"] = bad
            with self.assertRaises(settings.SettingsError): settings.validate_settings(value)
        for bad in ("relative", "\\\\server\\share", "//server/share", "bad\npath"):
            value = self.value(); value["project_root"] = bad
            with self.assertRaises(settings.SettingsError): settings.validate_settings(value)

    def test_override_changes_only_existing_open_path_target(self):
        settings.save_local_settings(self.root, self.value())
        config = {"actions": {"open_jarvis": {"type": "open_path", "target": str(self.root / "old")},
                              "open_wechat": {"type": "open_path", "target": str(self.root / "old.exe")}},
                  "settings": {"wake_words": ["贾维斯"]}, "scenes": {}}
        original = copy.deepcopy(config)
        result = settings.apply_skill_targets(config, self.root)
        self.assertEqual(result["actions"]["open_wechat"]["target"], str(self.root / "Weixin.exe"))
        self.assertEqual(result["settings"], original["settings"])
        self.assertEqual(set(result["actions"]), set(original["actions"]))
        config["actions"]["open_wechat"]["type"] = "status"
        with self.assertRaises(settings.SettingsError): settings.apply_skill_targets(config, self.root)

    def test_redirect_config_is_rejected_before_reading(self):
        with mock.patch.object(settings, "is_redirect", return_value=True), mock.patch.object(Path, "read_text") as read:
            with self.assertRaises(settings.SettingsError): settings.load_local_settings(self.root)
            read.assert_not_called()

    def test_oversized_settings_rejected(self):
        (self.root / settings.LOCAL_NAME).write_text(" " * 17000, encoding="utf-8")
        with self.assertRaises(settings.SettingsError): settings.load_local_settings(self.root)

    def test_report_before_install_is_read_only(self):
        with mock.patch.object(install, "_run") as process:
            self.assertFalse(install.installation_report(self.root)["base_ready"])
            process.assert_not_called()
        self.assertFalse((self.root / ".voice-venv").exists())

    def test_existing_environment_never_upgraded_by_default(self):
        (self.root / ".voice-venv").mkdir()
        with mock.patch.object(install, "_run") as process, mock.patch.object(install.venv, "EnvBuilder") as create:
            with self.assertRaises(install.SetupError): install.initialize_environment(self.root)
            process.assert_not_called(); create.assert_not_called()

    def test_new_environment_uses_isolated_pip_and_fixed_argv(self):
        with mock.patch.object(install.venv, "EnvBuilder") as create, mock.patch.object(
            install, "_run", return_value=subprocess.CompletedProcess([], 0)
        ) as process:
            install.initialize_environment(self.root)
        create.assert_called_once_with(with_pip=True)
        args = process.call_args.args[0]
        self.assertIn("--isolated", args); self.assertIn("--only-binary=:all:", args)
        self.assertEqual(args[-1], str(self.root / "requirements.txt"))
        self.assertFalse((self.root / ".jarvis-install-pending").exists())

    def test_failed_install_can_resume_without_destroying_environment(self):
        target = self.root / ".voice-venv"; target.mkdir()
        marker = self.root / ".jarvis-install-pending"; marker.touch()
        with mock.patch.object(install, "_run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(install.SetupError): install.initialize_environment(self.root)
        self.assertTrue(marker.exists()); self.assertTrue(target.is_dir())

    def test_loopback_probe_rejects_any_custom_or_external_url(self):
        for url in ("http://example.com", "http://localhost:11434/api/tags", "http://127.0.0.1:8000/private", "https://api.openai.com"):
            with self.assertRaises(install.SetupError): install.loopback_json(url)
        self.assertIsNone(install._NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_service_report_checks_tags_but_never_generates(self):
        with mock.patch.object(install, "loopback_json", side_effect=[{"jarvis": "online"}, {"models": [{"name": "qwen3.8:27b"}]}]) as get:
            result = install.service_report(self.root)
        self.assertTrue(result["model_installed"])
        self.assertEqual(result["phone"], "online")
        self.assertEqual(get.call_count, 2)

    def test_download_only_uses_fixed_model_preparation_script(self):
        with mock.patch.object(install, "_run", return_value=subprocess.CompletedProcess([], 0)) as process:
            install.prepare_asr(self.root)
        self.assertEqual(process.call_args.args[0][-2:], [str(self.root / "jarvis_probe.py"), "--download-asr"])

    def test_desktop_not_started_until_dependencies_and_asr_ready(self):
        settings.save_local_settings(self.root, self.value())
        with mock.patch.object(install, "_spawn") as spawn:
            with self.assertRaises(install.SetupError): install.start_desktop(self.root, report={"base_ready": True, "asr_cached": False})
            spawn.assert_not_called()
            message = install.start_desktop(self.root, report={"base_ready": True, "asr_cached": True})
            self.assertIn("已请求", message)
            self.assertEqual(spawn.call_args.args[1][-1], str(self.root / "desktop_controller.py"))

    def test_existing_phone_not_restarted_or_rekeyed(self):
        with mock.patch.object(install, "service_report", return_value={"phone": "online"}), mock.patch.object(install, "_spawn") as spawn:
            result = install.start_phone(self.root, phone_key="test-only-new-password")
        self.assertIn("未重启", result); spawn.assert_not_called()

    def test_phone_password_only_in_child_environment_not_argv_or_files(self):
        settings.save_local_settings(self.root, self.value())
        password = "test-only-connection-password"
        with mock.patch.object(install, "service_report", return_value={"phone": "unavailable"}), mock.patch.object(
            install.socket, "socket"
        ) as socket_mock, mock.patch.object(install, "_spawn") as spawn:
            socket_mock.return_value.__enter__.return_value.connect_ex.return_value = 1
            result = install.start_phone(self.root, phone_key=password, report={"base_ready": True, "asr_cached": True})
        args = spawn.call_args
        self.assertNotIn(password, repr(args.args))
        self.assertEqual(args.kwargs["env"]["JARVIS_API_KEY"], password)
        self.assertEqual(args.args[1][-4:], ["--host", "127.0.0.1", "--port", "8000"])
        self.assertFalse((self.root / ".env").exists()); self.assertFalse((self.root / "openai.env").exists())
        self.assertNotIn(password, result)

    def test_weak_phone_password_rejected_without_any_network_or_process(self):
        with mock.patch.object(install, "service_report") as probe:
            for key in ("short", "with spaces" * 3, "中文" * 16):
                with self.assertRaises(install.SetupError): install.start_phone(self.root, phone_key=key)
            probe.assert_not_called()

    def test_process_wrapper_never_uses_shell_or_displays_child_errors(self):
        with mock.patch.object(install.subprocess, "run", side_effect=OSError("private-value")) as process:
            with self.assertRaises(install.SetupError) as error: install._run(["fixed-program"], self.root)
        self.assertFalse(process.call_args.kwargs["shell"])
        self.assertNotIn("private-value", str(error.exception))

    def test_auth_files_are_not_read_by_settings_or_installer(self):
        original = Path.read_text
        def guard(path, *args, **kwargs):
            if path.name in {".env", "openai.env"}:
                raise AssertionError("Protected file was read")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", guard):
            settings.save_local_settings(self.root, self.value())
            self.assertEqual(settings.load_local_settings(self.root), self.value())
            install.installation_report(self.root)

    def test_new_launch_requires_explicit_machine_configuration(self):
        with mock.patch.object(install, "_spawn") as spawn:
            with self.assertRaises(install.SetupError):
                install.start_desktop(self.root, report={"base_ready": True, "asr_cached": True})
            spawn.assert_not_called()

    def test_fixed_reply_preparation_is_explicit_and_not_a_shell(self):
        with mock.patch.object(install, "_run", return_value=subprocess.CompletedProcess([], 0)) as process:
            install.prepare_voice_replies(self.root)
        self.assertEqual(process.call_args.args[0][-1], "--prepare-replies")

    def test_build_uses_system_compiler_without_changing_powershell_policy(self):
        (self.root / "jarvis_distribution_launcher.cs").touch()
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(
            install, "_run", return_value=subprocess.CompletedProcess([], 0)
        ) as process:
            install.build_launchers(self.root)
        self.assertEqual(process.call_count, 3)
        for call in process.call_args_list:
            args = call.args[0]
            self.assertTrue(args[0].endswith("csc.exe"))
            self.assertIn("/target:winexe", args)
            self.assertFalse(any("ExecutionPolicy" in part for part in args))

    def test_auth_file_links_are_rejected_before_launch(self):
        original = install.is_redirect
        with mock.patch.object(install, "is_redirect", side_effect=lambda path: path.name == ".env" or original(path)), mock.patch.object(install, "_spawn") as spawn:
            with self.assertRaises(install.SetupError): install.start_phone(self.root, phone_key="test-only-connection-password")
            spawn.assert_not_called()

    def test_missing_environment_with_pending_marker_can_resume_creation(self):
        (self.root / ".jarvis-install-pending").touch()
        with mock.patch.object(install.venv, "EnvBuilder") as create, mock.patch.object(
            install, "_run", return_value=subprocess.CompletedProcess([], 0)
        ):
            install.initialize_environment(self.root)
        create.return_value.create.assert_called_once_with(self.root / ".voice-venv")

    def test_reply_cache_only_synthesizes_fixed_configured_messages(self):
        import jarvis_probe
        skill = SimpleNamespace(actions={"status": {"message": "固定回复"}}, scenes={}, settings={})
        def synthesize(_message):
            path = self.root / "temporary-test-wave.wav"
            path.write_bytes(b"RIFF-test-only")
            return path
        with mock.patch("jarvis_skills.SkillSystem.load", return_value=skill), mock.patch("dynamic_tts.synthesize_wave", side_effect=synthesize) as voice:
            self.assertEqual(jarvis_probe.prepare_replies(self.root), 5)
            self.assertEqual(jarvis_probe.prepare_replies(self.root), 0)
        self.assertEqual(voice.call_count, 5)
        self.assertFalse((self.root / "temporary-test-wave.wav").exists())

    def test_reply_cache_prevents_exponential_large_skill_sets(self):
        import jarvis_probe
        skill = SimpleNamespace(actions={str(i): {"message": "固定回复"} for i in range(11)}, scenes={}, settings={})
        with mock.patch("jarvis_skills.SkillSystem.load", return_value=skill), mock.patch("dynamic_tts.synthesize_wave") as voice:
            with self.assertRaises(ValueError): jarvis_probe.prepare_replies(self.root)
            voice.assert_not_called()


class WizardSmokeTests(unittest.TestCase):
    def test_wizard_builds_without_showing_window_or_starting_processes(self):
        import tkinter as tk
        import jarvis_setup
        real_tk = tk.Tk
        windows = []
        def hidden_root():
            root = real_tk()
            root.withdraw()
            windows.append(root)
            return root
        try:
            with mock.patch.object(tk, "Tk", hidden_root), mock.patch.object(tk.Tk if isinstance(tk.Tk, type) else real_tk, "mainloop"), mock.patch.object(jarvis_setup, "start_desktop") as desktop, mock.patch.object(jarvis_setup, "start_phone") as phone:
                jarvis_setup.show_wizard()
                windows[0].update_idletasks()
                self.assertEqual(windows[0].state(), "withdrawn")
                desktop.assert_not_called(); phone.assert_not_called()
        finally:
            for window in windows:
                window.destroy()


if __name__ == "__main__":
    unittest.main()
