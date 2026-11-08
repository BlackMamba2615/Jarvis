"""Deterministic desktop tests: no real UI, voice, credentials or cloud calls."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import desktop_automation as desktop
from jarvis_skills import SkillSystem, SkillConfigError


def window(**overrides):
    return {"id":"w-100", "pid":10, "process_instance":"process-10-v1", "app_name":"notepad.exe",
            "title":"测试文档", "visible":True, "minimized":False, **overrides}


def node(name="测试按钮", role="button", ref="@stest:e1", **extra):
    return {"name":name, "role":role, "ref_id":ref,
            "native_id":{"kind":"automation_id","value":name}, **extra}


class FakeCLI:
    def __init__(self):
        self.windows = [window()]
        self.nodes = [node(), node("测试输入", "textfield", "@stest:e2"), node("测试选项", "checkbox", "@stest:e3")]
        self.calls = []
        self.complete = True
        self.result = {"ok":True, "data":{"disposition":{"delivery":"delivered_verified", "retry":"unsafe"}}}

    def __call__(self, args, *, value=None):
        self.calls.append((args, value))
        if args[0] == "list-windows":
            return {"ok":True,"data":copy.deepcopy(self.windows)}
        if args[0] == "snapshot":
            return {"ok":True,"data":{"complete":self.complete,"window":{"id":"w-100"},
                    "tree":{"role":"window","children":copy.deepcopy(self.nodes)}}}
        if args[0] == "is":
            return {"ok":True,"data":{"result":False,"applicable":True}}
        return copy.deepcopy(self.result)

    def mutations(self):
        return [(args,value) for args,value in self.calls if args[0] not in {"list-windows","snapshot","is"}]


class DesktopAutomationTests(unittest.TestCase):
    def setUp(self):
        self.skills = SkillSystem.load(Path(__file__).with_name("skills.json"))
        self.fake = FakeCLI()
        self.now = 100.0
        self.engine = desktop.DesktopAutomation(self.skills, runner=self.fake, clock=lambda:self.now)
        self.addCleanup(self.engine.close)

    def prepare(self, operation="click", **extra):
        return self.engine.handle({"operation":operation,"app":"记事本","control":"测试按钮",**extra},owner="mobile")

    def confirm(self, result, owner="mobile"):
        return self.engine.handle({"operation":"confirm","token":result["desktop_token"]},owner=owner)

    def test_grammar_and_whitelist_do_not_change_wechat_routing(self):
        for text, action in [("最大化记事本窗口","desktop_maximize"),("还原记事本","desktop_restore"),
                             ("查看计算器控件","desktop_inspect"),("点击记事本的保存按钮","desktop_click"),
                             ("确认桌面操作","desktop_confirm")]:
            self.assertEqual([x.action_id for x in self.skills.plan(text)[0]],[action])
        for text in ("不要最大化记事本","怎么最大化记事本","如何点击记事本的保存按钮","打开微信"):
            self.assertIsNone(desktop.parse_desktop_command(text))
        self.assertEqual([x.action_id for x in self.skills.plan("帮我在电脑上登录微信")[0]],["wechat_login_qr"])

    def test_unknown_commands_fields_and_shell_apps_fail_closed(self):
        for payload in ({"operation":"launch"},{"operation":"windows","argv":"cmd.exe"},
                        {"operation":"click","window_id":["w-100"]}):
            with self.assertRaises(desktop.DesktopControlUnavailable):
                self.engine.handle(payload,owner="mobile")
        self.assertFalse(self.fake.calls)
        for app in ("cmd.exe","powershell.exe","Weixin.exe","../notepad.exe"):
            with self.assertRaises(ValueError):
                desktop.validate_apps({"unsafe":[app]})

    def test_only_whitelisted_non_sensitive_windows_are_returned(self):
        self.fake.windows += [window(id="w-101",app_name="cmd.exe"), window(id="w-102",title="openai.env"),
                              window(id="w-103",visible=False),window(id="w-104",app_name="Weixin.exe")]
        result=self.engine.handle({"operation":"windows"},owner="mobile")
        self.assertEqual(len(result["windows"]),1)
        self.assertEqual(set(result["windows"][0]),{"id","title","app","minimized"})

    def test_same_app_windows_are_never_guessed(self):
        self.fake.windows += [window(id="w-101",pid=11)]
        with self.assertRaises(desktop.DesktopControlUnavailable):
            self.prepare()
        self.assertFalse(self.fake.mutations())

    def test_control_preview_never_mutates_or_exposes_values(self):
        self.fake.nodes += [node("密码","textfield","@stest:e3", value="do-not-export"),
                            node("secret","textfield","@stest:e4"), node("普通输入","textfield","@stest:e5",value="private")]
        result=self.engine.handle({"operation":"inspect","app":"记事本"},owner="mobile")
        rendered=json.dumps(result,ensure_ascii=False)
        for value in ("do-not-export","private","@stest",'"value"',"密码"):
            self.assertNotIn(value,rendered)
        self.assertFalse(self.fake.mutations())

    def test_confirmation_is_once_and_reresolves_fresh_ref(self):
        result=self.prepare()
        self.assertTrue(result["pending_confirmation"])
        self.assertFalse(self.fake.mutations())
        self.fake.nodes[0]["ref_id"]="@sfresh:e20"
        self.assertTrue(self.confirm(result)["success"])
        self.assertEqual(self.fake.mutations(),[(["click","@sfresh:e20"],None)])
        with self.assertRaises(desktop.DesktopControlUnavailable):
            self.confirm(result)
        self.assertEqual(len(self.fake.mutations()),1)

    def test_confirmation_is_owner_and_token_scoped(self):
        result=self.prepare()
        with self.assertRaises(desktop.DesktopControlUnavailable):
            self.confirm(result,owner="desktop")
        with self.assertRaises(desktop.DesktopControlUnavailable):
            self.engine.handle({"operation":"confirm"},owner="mobile")
        with self.assertRaises(desktop.DesktopControlUnavailable):
            self.engine.handle({"operation":"confirm","token":"wrong"},owner="mobile")
        self.assertFalse(self.fake.mutations())

    def test_pending_expires_and_cannot_resume(self):
        result=self.prepare()
        self.now += desktop.TTL+1
        with self.assertRaises(desktop.DesktopControlUnavailable):
            self.confirm(result)
        self.assertFalse(self.engine._pending)
        self.assertFalse(self.fake.mutations())

    def test_new_request_and_cancel_invalidate_approval(self):
        for request in ({"operation":"windows"},{"operation":"cancel"}):
            result=self.prepare()
            self.engine.handle(request,owner="mobile")
            with self.assertRaises(desktop.DesktopControlUnavailable):
                self.confirm(result)
        self.assertFalse(self.fake.mutations())

    def test_window_process_or_title_change_invalidates_confirmation(self):
        for field,value in (("pid",99),("process_instance","new-process"),("title","new title")):
            self.fake.windows=[window()]
            result=self.prepare()
            self.fake.windows[0][field]=value
            with self.assertRaises(desktop.DesktopControlUnavailable):
                self.confirm(result)
        self.assertFalse(self.fake.mutations())

    def test_duplicate_incomplete_or_changed_control_never_clicks(self):
        for variant in ("duplicate","incomplete","removed"):
            self.fake.nodes=[node()];self.fake.complete=True
            result=self.prepare()
            if variant=="duplicate":self.fake.nodes.append(node(ref="@stwin:e10"))
            elif variant=="incomplete":self.fake.complete=False
            else:self.fake.nodes=[]
            with self.assertRaises(desktop.DesktopControlUnavailable):
                self.confirm(result)
        self.assertFalse(self.fake.mutations())

    def test_text_uses_stdin_without_shell_or_argv_content(self):
        text='literal $(cmd.exe) `whoami` "hello"'
        result=self.prepare("set-value",control="测试输入",value=text)
        self.assertNotIn(text,json.dumps(result))
        self.confirm(result)
        args,value=self.fake.mutations()[0]
        self.assertEqual(args,["set-value","@stest:e2","--stdin"])
        self.assertEqual(value,text)
        self.assertNotIn(text," ".join(args))

    def test_uncertain_delivery_is_not_retried(self):
        result=self.prepare()
        self.fake.result={"ok":False,"error":{"code":"ACTION_FAILED","disposition":{
            "delivery":"delivered_unverified","retry":"unsafe"},"details":{"kind":"post_action_verification"}}}
        response=self.confirm(result)
        self.assertFalse(response["success"])
        self.assertTrue(response["readback"])
        self.assertEqual(len(self.fake.mutations()),1)
        self.assertEqual(self.fake.calls[-1][0][0],"snapshot")

    def test_minimized_window_requires_explicit_restore(self):
        self.fake.windows[0]["minimized"]=True
        with self.assertRaises(desktop.DesktopControlUnavailable):self.prepare()
        self.assertFalse(self.fake.mutations())

    def test_operation_removed_from_skills_invalidates_pending(self):
        result=self.prepare()
        self.skills.actions.pop("desktop_click")
        with self.assertRaises(desktop.DesktopControlUnavailable):self.confirm(result)
        self.assertFalse(self.fake.mutations())

    def test_already_unchecked_is_observed_without_toggle(self):
        draft=self.prepare("uncheck",control="测试选项")
        response=self.confirm(draft)
        self.assertTrue(response["success"])
        self.assertEqual(response["delivery"],"not_needed")
        self.assertFalse(self.fake.mutations())

    def test_private_state_cleanup_is_bounded_to_owned_temp_directory(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-desktop-") as directory:
            self.engine._home=directory
            self.engine.close()
            self.assertFalse(Path(directory).exists())
        self.engine._home=str(Path(__file__).parent)
        with mock.patch("desktop_automation.shutil.rmtree") as remove:
            self.engine.close()
        remove.assert_not_called()

    def test_cli_uses_fixed_argv_stdin_and_no_secret_environment(self):
        import subprocess
        binary=desktop.BASE_DIR / "tools" / "agent-desktop.exe"
        with tempfile.TemporaryDirectory(prefix="jarvis-desktop-") as directory:
            self.engine._runner=None
            self.engine._home=directory
            with (mock.patch.object(Path,"is_file",return_value=True),
                  mock.patch("jarvis_settings.is_redirect",return_value=False),
                  mock.patch("desktop_automation.subprocess.run",return_value=subprocess.CompletedProcess([],0,b'{"ok":true,"data":{}}')) as run,
                  mock.patch.dict(os.environ,{"OPENAI_API_KEY":"test-only-secret","AGENT_DESKTOP_TRACE":"not-forwarded"})):
                self.engine._call(["set-value","@stest:e1","--stdin"],value="hello $(anything)")
            args=run.call_args.args[0];options=run.call_args.kwargs
            self.assertEqual(args,[str(binary),"set-value","@stest:e1","--stdin"])
            self.assertFalse(options["shell"])
            self.assertEqual(options["input"],b"hello $(anything)")
            self.assertNotIn("OPENAI_API_KEY",options["env"])
            self.assertNotIn("AGENT_DESKTOP_TRACE",options["env"])
            self.engine._home=None

    def test_config_rejects_arbitrary_operations(self):
        config=copy.deepcopy(self.skills.config)
        config["actions"]["desktop_click"]["operation"]="batch"
        with self.assertRaises(SkillConfigError):SkillSystem(Path("unused.json"),config)


class DesktopAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib
        from fastapi.testclient import TestClient
        with mock.patch.dict(os.environ,{"JARVIS_API_KEY":"test-only-key"}),mock.patch(
                "dotenv.load_dotenv",return_value=False),mock.patch("faster_whisper.WhisperModel"):
            cls.main=importlib.import_module("main")
        cls.client=TestClient(cls.main.app)
        cls.headers={"X-Jarvis-Key":"test-only-key"}

    def test_private_and_fixed_endpoint(self):
        with mock.patch.object(self.main,"request_desktop_control") as bridge:
            self.assertEqual(self.client.post("/desktop/control",json={"operation":"windows"}).status_code,401)
        bridge.assert_not_called()
        with mock.patch.object(self.main,"request_desktop_control",return_value={"windows":[]}) as bridge:
            response=self.client.post("/desktop/control",json={"operation":"windows"},headers=self.headers)
        self.assertEqual(response.status_code,200)
        bridge.assert_called_once_with({"operation":"windows"})

    def test_voice_uses_typed_local_bridge_and_never_chat(self):
        with (mock.patch.object(self.main,"request_desktop_control",return_value={"success":True,"message":"ok"}) as bridge,
              mock.patch.object(self.main,"assistant_chat") as chat):
            response=self.main.handle_mobile_voice("帮我最大化记事本")
        bridge.assert_called_once_with({"operation":"maximize","app":"记事本"})
        chat.assert_not_called()
        self.assertTrue(response["success"])

    def test_dry_run_never_calls_adapter(self):
        with mock.patch.object(self.main,"request_desktop_control") as bridge:
            result=self.main.execute_command("最大化记事本",dry_run=True)
        bridge.assert_not_called();self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
