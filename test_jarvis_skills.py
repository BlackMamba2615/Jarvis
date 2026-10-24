import copy
import json
import unittest
from pathlib import Path

from jarvis_skills import SkillConfigError, SkillSystem


PROJECT_DIRECTORY = Path(__file__).resolve().parent
SKILLS_PATH = PROJECT_DIRECTORY / "skills.json"


def normalize_for_test(value: str) -> str:
    punctuation = "。！？!？，,、；;：:\"'（）()【】[]"
    normalized = value.lower().strip()
    for character in punctuation:
        normalized = normalized.replace(character, "")
    return " ".join(normalized.split())


class SkillSystemRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.system = SkillSystem.load(
            SKILLS_PATH,
            normalize_term=normalize_for_test,
        )

    def plan(self, command: str) -> list[str]:
        normalized = normalize_for_test(command)
        for wake_word in self.system.setting_list("wake_words"):
            normalized_wake_word = normalize_for_test(wake_word)
            if normalized.startswith(normalized_wake_word):
                normalized = normalized[len(normalized_wake_word) :].strip()
                break
        planned, _ = self.system.plan(normalized)
        return [item.action_id for item in planned]

    def test_calculator_natural_phrases_are_allowlisted(self) -> None:
        for command in (
            "打开计算器",
            "贾维斯，帮我打开计算器",
            "开启 calculator",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.plan(command), ["open_calculator"])

    def test_multiple_apps_remain_separate_allowlisted_actions(self) -> None:
        self.assertEqual(
            self.plan("打开微信和计算器"),
            ["open_wechat", "open_calculator"],
        )

    def test_wechat_login_qr_is_a_separate_allowlisted_action(self) -> None:
        for phrase in (
            "帮我在电脑上登陆微信",
            "贾维斯，帮我在电脑上登录微信",
            "微信扫码登录",
            "电脑上帮我微信扫码",
            "在计算机上用二维码登录微信",
            "PC微信需要登陆",
            "微信要在 Windows 电脑扫码",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn("wechat_login_qr", self.plan(phrase))
        for phrase in (
            "帮我打开微信",
            "打开电脑微信",
            "在电脑上运行微信",
            "电脑状态",
            "登录微信网页版",
            "电脑扫码登录其他应用",
            "不要在电脑上登录微信",
            "电脑微信暂时不用扫码",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn("wechat_login_qr", self.plan(phrase))

    def test_unknown_command_does_not_execute_any_action(self) -> None:
        result = self.system.run(
            original_text="打开终端并执行任意命令",
            normalized_command=normalize_for_test("打开终端并执行任意命令"),
            dry_run=True,
        )
        self.assertFalse(result["recognized"])
        self.assertEqual(result["actions"], [])

    def test_dry_run_returns_message_without_starting_program(self) -> None:
        result = self.system.run(
            original_text="打开计算器",
            normalized_command=normalize_for_test("打开计算器"),
            dry_run=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["actions"], ["open_calculator"])
        self.assertEqual(result["message"], "计算器已打开")


class SkillConfigurationSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with SKILLS_PATH.open("r", encoding="utf-8-sig") as config_file:
            cls.base_config = json.load(config_file)

    def assert_config_rejected(self, config: dict) -> None:
        with self.assertRaises(SkillConfigError):
            SkillSystem(SKILLS_PATH, config)

    def test_shell_action_type_is_rejected(self) -> None:
        config = copy.deepcopy(self.base_config)
        config["actions"]["unsafe"] = {
            "type": "shell",
            "command": "user supplied text",
            "triggers": ["执行命令"],
            "message": "不应执行",
        }
        self.assert_config_rejected(config)

    def test_exit_phrases_are_separate_from_sleep_phrases(self) -> None:
        self.assertIn("退出", self.base_config["settings"]["exit_phrases"])
        self.assertNotIn("退出", self.base_config["settings"]["sleep_phrases"])
        config = copy.deepcopy(self.base_config)
        config["settings"]["exit_phrases"] = ["退出", 42]
        self.assert_config_rejected(config)
        config = copy.deepcopy(self.base_config)
        config["settings"]["exit_phrases"].append("休眠")
        self.assert_config_rejected(config)

    def test_relative_open_path_is_rejected(self) -> None:
        config = copy.deepcopy(self.base_config)
        config["actions"]["unsafe"] = {
            "type": "open_path",
            "target": ".\\untrusted.exe",
            "aliases": ["未知程序"],
            "intent": "open",
            "message": "不应执行",
        }
        self.assert_config_rejected(config)

    def test_malformed_login_keyword_groups_are_rejected(self) -> None:
        for groups in ([], ["微信"], [["微信"], []], [["微信"], [123]]):
            with self.subTest(groups=groups):
                config = copy.deepcopy(self.base_config)
                config["actions"]["wechat_login_qr"]["keyword_groups"] = groups
                self.assert_config_rejected(config)

    def test_other_actions_cannot_use_login_keyword_groups(self) -> None:
        config = copy.deepcopy(self.base_config)
        config["actions"]["lock_pc"]["keyword_groups"] = [["电脑"]]
        self.assert_config_rejected(config)


if __name__ == "__main__":
    unittest.main()
