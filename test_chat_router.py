import unittest
import threading
from unittest import mock

import chat_router
import chat_sessions
from local_llm import LocalModelUnavailable
from openai_chat import OpenAIAPIUnavailable


class ChatRouterTests(unittest.TestCase):
    def setUp(self):
        chat_sessions.clear_history()
        self.memory_patch = mock.patch("chat_sessions.list_memories", return_value=[])
        self.memory_patch.start()
        self.addCleanup(self.memory_patch.stop)

    @mock.patch("chat_router.chat_openai")
    @mock.patch("chat_router.chat_local", return_value="本地回答")
    def test_simple_query_prefers_local(self, local, cloud):
        self.assertEqual(chat_router.chat("你好"), "本地回答")
        self.assertEqual(local.call_args.args, ("你好",))
        self.assertEqual(local.call_args.kwargs["history"], [])
        cloud.assert_not_called()

    @mock.patch("chat_router.chat_openai", return_value="云端回答")
    @mock.patch("chat_router.chat_local")
    def test_complex_query_prefers_independent_api(self, local, cloud):
        self.assertEqual(chat_router.chat("深入分析这个方案"), "云端回答")
        self.assertEqual(cloud.call_args.args, ("深入分析这个方案",))
        self.assertFalse(cloud.call_args.kwargs["web_search"])
        local.assert_not_called()

    @mock.patch("chat_router.chat_openai", side_effect=OpenAIAPIUnavailable(reason="timeout"))
    @mock.patch("chat_router.chat_local", return_value="本地兜底")
    def test_openai_failure_falls_back_to_local(self, local, cloud):
        self.assertEqual(chat_router.chat("详细分析这个方案"), "本地兜底")
        self.assertEqual(local.call_args.args, ("详细分析这个方案",))
        self.assertTrue(chat_router.get_last_route()["fallback"])

    @mock.patch("chat_router.chat_openai", return_value="云端兜底")
    @mock.patch("chat_router.chat_local", side_effect=LocalModelUnavailable(reason="connection"))
    def test_local_failure_falls_back_to_openai(self, local, cloud):
        self.assertEqual(chat_router.chat("你好"), "云端兜底")
        self.assertEqual(cloud.call_args.args, ("你好",))

    @mock.patch("chat_router.chat_openai")
    @mock.patch("chat_router.chat_local", side_effect=ValueError("invalid input"))
    def test_invalid_input_does_not_call_cloud(self, local, cloud):
        with self.assertRaises(ValueError):
            chat_router.chat("")
        cloud.assert_not_called()

    def test_complexity_classifier(self):
        self.assertFalse(chat_router.is_complex_query("讲个笑话"))
        self.assertTrue(chat_router.is_complex_query("请做架构设计"))
        self.assertTrue(chat_router.is_complex_query("你怎么看" * 30))

    @mock.patch("chat_router.chat_openai", return_value="云端续答")
    @mock.patch("chat_router.chat_local", return_value="本地首答")
    def test_model_switch_shares_history_but_sessions_do_not(self, local, cloud):
        chat_router.chat("我喜欢简洁回答")
        chat_router.chat("请详细分析这个方案")
        self.assertEqual(cloud.call_args.kwargs["history"], [
            {"role": "user", "content": "我喜欢简洁回答"},
            {"role": "assistant", "content": "本地首答"},
        ])
        chat_sessions.clear_history("unit-other")
        chat_router.chat("你好", session_id="unit-other")
        self.assertEqual(local.call_args.kwargs["history"], [])

    @mock.patch("chat_router.chat_openai", side_effect=OpenAIAPIUnavailable(reason="timeout"))
    @mock.patch("chat_router.chat_local")
    def test_search_failure_never_uses_offline_answer(self, local, cloud):
        answer = chat_router.chat("联网搜索最新新闻")
        self.assertIn("无法完成联网", answer)
        self.assertTrue(cloud.call_args.kwargs["web_search"])
        local.assert_not_called()
        self.assertEqual(chat_router.get_last_route()["backend"], "unavailable")

    @mock.patch("chat_router.chat_openai")
    @mock.patch("chat_router.chat_local")
    def test_partial_stream_is_not_mixed_with_fallback(self, local, cloud):
        def partial(_text, **kwargs):
            kwargs["on_delta"]("已经开始。")
            raise LocalModelUnavailable(reason="connection")
        local.side_effect = partial
        delivered = []
        with self.assertRaises(LocalModelUnavailable):
            chat_router.chat("你好", on_delta=delivered.append)
        self.assertEqual(delivered, ["已经开始。"])
        cloud.assert_not_called()
        self.assertEqual(chat_sessions.get_history("default"), [])

    @mock.patch("chat_router.chat_local")
    def test_pre_cancelled_request_never_calls_model(self, local):
        event = threading.Event()
        event.set()
        with self.assertRaises(chat_sessions.ChatCancelled):
            chat_router.chat("你好", cancel_event=event)
        local.assert_not_called()

    @mock.patch("chat_router.chat_openai", return_value="云端回答")
    @mock.patch("chat_router.chat_local", return_value="本地回答")
    def test_request_route_snapshot_survives_later_request(self, local, cloud):
        first = {"old": "discarded"}
        chat_router.chat("你好", route_out=first)
        chat_router.chat("请详细分析这个方案")
        self.assertEqual(first["backend"], "local")
        self.assertNotIn("old", first)
        self.assertEqual(chat_router.get_last_route()["backend"], "openai")

    @mock.patch("chat_router.chat_openai", side_effect=OpenAIAPIUnavailable(reason="quota_exhausted"))
    @mock.patch("chat_router.chat_local", side_effect=LocalModelUnavailable(reason="connection"))
    def test_request_route_snapshot_is_filled_on_terminal_failure(self, local, cloud):
        route = {}
        with self.assertRaises(OpenAIAPIUnavailable):
            chat_router.chat("你好", route_out=route)
        self.assertEqual(route["backend"], "openai")
        self.assertEqual(route["reason"], "quota_exhausted")
        self.assertTrue(route["fallback"])

    @mock.patch("chat_router.chat_local")
    def test_request_route_snapshot_is_filled_on_mid_request_cancel(self, local):
        event, route = threading.Event(), {}
        def cancel(_text, **kwargs):
            kwargs["cancel_event"].set()
            raise chat_sessions.ChatCancelled()
        local.side_effect = cancel
        with self.assertRaises(chat_sessions.ChatCancelled):
            chat_router.chat("你好", cancel_event=event, route_out=route)
        self.assertEqual(route["reason"], "cancelled")


if __name__ == "__main__":
    unittest.main()
