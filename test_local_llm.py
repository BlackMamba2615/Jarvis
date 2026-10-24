"""The local model may answer conversations, but receives no action tools."""

import unittest
import json
import threading
from unittest import mock

import requests

import local_llm
from chat_sessions import ChatCancelled


class LocalChatTests(unittest.TestCase):
    def setUp(self):
        local_llm._HISTORY.clear()

    def tearDown(self):
        local_llm._HISTORY.clear()

    @mock.patch("local_llm.requests.post")
    def test_loopback_chat_has_no_tools_and_keeps_short_history(self, post):
        post.return_value.json.return_value = {"message": {"content": "你好。"}}
        self.assertEqual(local_llm.chat_local("你好"), "你好。")
        self.assertEqual(local_llm.chat_local("你是谁"), "你好。")
        first = post.call_args_list[0]
        second = post.call_args_list[1]
        self.assertEqual(first.args[0], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(first.kwargs["json"]["model"], "qwen3.8:27b")
        self.assertFalse(first.kwargs["json"]["stream"])
        self.assertFalse(first.kwargs["json"]["think"])
        self.assertNotIn("tools", first.kwargs["json"])
        self.assertFalse(first.kwargs["allow_redirects"])
        self.assertEqual(first.kwargs["proxies"], {"http": "", "https": "", "all": ""})
        self.assertEqual(
            [entry["role"] for entry in second.kwargs["json"]["messages"]],
            ["system", "user", "assistant", "user"],
        )

    @mock.patch("local_llm.requests.post")
    def test_malformed_reply_is_a_safe_unavailable_error(self, post):
        post.return_value.json.return_value = {"message": "not an object"}
        with self.assertRaises(local_llm.LocalModelUnavailable) as raised:
            local_llm.chat_local("你好")
        self.assertEqual(raised.exception.reason, "empty_response")
        self.assertEqual(len(local_llm._HISTORY), 0)

    @mock.patch("local_llm.requests.post", side_effect=requests.exceptions.ConnectionError)
    def test_connection_failure_does_not_expose_prompt(self, post):
        with self.assertRaises(local_llm.LocalModelUnavailable) as raised:
            local_llm.chat_local("private example")
        self.assertEqual(raised.exception.reason, "connection")
        self.assertNotIn("private example", str(raised.exception))

    def test_rejects_empty_and_oversized_input(self):
        for value in (" ", "x" * 2001):
            with self.assertRaises(ValueError):
                local_llm.chat_local(value)

    @mock.patch("local_llm.requests.post")
    def test_streams_only_visible_content_and_uses_explicit_context(self, post):
        post.return_value.iter_lines.return_value = [
            json.dumps({"message": {"thinking": "private reasoning", "content": "你好，"}}, ensure_ascii=False).encode(),
            json.dumps({"message": {"content": "先生。"}, "done": True}, ensure_ascii=False).encode(),
        ]
        delivered = []
        answer = local_llm.chat_local("你好", history=[], on_delta=delivered.append)
        self.assertEqual(answer, "你好，先生。")
        self.assertEqual(delivered, ["你好，", "先生。"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertEqual(len(local_llm._HISTORY), 0)
        post.return_value.close.assert_called_once()

    @mock.patch("local_llm.requests.post")
    def test_cancellation_closes_stream_and_does_not_save_turn(self, post):
        event = threading.Event()
        post.return_value.iter_lines.return_value = [
            b'{"message":{"content":"hello"}}',
            b'{"message":{"content":"there"},"done":true}',
        ]
        def interrupt(_delta):
            event.set()
        with self.assertRaises(ChatCancelled):
            local_llm.chat_local("你好", history=[], on_delta=interrupt, cancel_event=event)
        post.return_value.close.assert_called_once()

    @mock.patch("local_llm.requests.post")
    def test_incomplete_stream_is_not_success(self, post):
        post.return_value.iter_lines.return_value = [b'{"message":{"content":"partial"}}']
        with self.assertRaises(local_llm.LocalModelUnavailable) as raised:
            local_llm.chat_local("你好", history=[], on_delta=lambda _text: None)
        self.assertEqual(raised.exception.reason, "incomplete_response")

    @mock.patch("local_llm.requests.post")
    def test_redirect_cannot_send_loopback_prompt_to_another_host(self, post):
        post.return_value.status_code = 307
        with self.assertRaises(local_llm.LocalModelUnavailable) as raised:
            local_llm.chat_local("只给本地模型", history=[])
        self.assertEqual(raised.exception.reason, "redirect_refused")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        post.return_value.json.assert_not_called()
        post.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
