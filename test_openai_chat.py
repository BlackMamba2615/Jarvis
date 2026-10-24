import os
import json
import threading
import unittest
from unittest import mock

import openai_chat
import requests
from chat_sessions import ChatCancelled

_REAL_LOAD_CONFIG = openai_chat._load_config


class OpenAIChatTests(unittest.TestCase):
    def setUp(self):
        openai_chat._HISTORY.clear()
        # Unit tests must never load the real approved credential file.
        self.config_patch = mock.patch("openai_chat._load_config")
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def tearDown(self):
        openai_chat._HISTORY.clear()

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""})
    @mock.patch("openai_chat.requests.post")
    def test_missing_separate_key_never_calls_network(self, post):
        with self.assertRaises(openai_chat.OpenAIAPIUnavailable) as raised:
            openai_chat.chat_openai("详细分析这个问题")
        self.assertEqual(raised.exception.reason, "not_configured")
        post.assert_not_called()

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-token"})
    @mock.patch("openai_chat.requests.post")
    def test_responses_api_extracts_text_without_tools(self, post):
        post.return_value.json.return_value = {
            "output": [
                {"type": "reasoning", "content": []},
                {"type": "message", "content": [
                    {"type": "output_text", "text": "第一部分。"},
                    {"type": "output_text", "text": "第二部分。"},
                ]},
            ],
        }
        self.assertEqual(openai_chat.chat_openai("分析"), "第一部分。\n第二部分。")
        args = post.call_args
        self.assertEqual(args.args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(args.kwargs["headers"]["Authorization"], "Bearer test-only-token")
        self.assertFalse(args.kwargs["json"]["store"])
        self.assertEqual(args.kwargs["timeout"], (8, 30))
        self.assertNotIn("tools", args.kwargs["json"])
        self.assertEqual(args.kwargs["json"]["input"][-1], {"role": "user", "content": "分析"})

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-token"})
    @mock.patch("openai_chat.requests.post")
    def test_empty_or_malformed_response_is_safe_failure(self, post):
        post.return_value.json.return_value = {"output": [{"type": "message", "content": None}]}
        with self.assertRaises(openai_chat.OpenAIAPIUnavailable) as raised:
            openai_chat.chat_openai("分析")
        self.assertEqual(raised.exception.reason, "empty_response")
        self.assertEqual(len(openai_chat._HISTORY), 0)

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-token"})
    @mock.patch("openai_chat.requests.post")
    def test_unsupported_region_is_classified_without_exposing_response(self, post):
        response = post.return_value
        response.status_code = 403
        response.json.return_value = {
            "error": {"code": "unsupported_country_region_territory"}
        }
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "sensitive raw response", response=response
        )
        with self.assertRaises(openai_chat.OpenAIAPIUnavailable) as raised:
            openai_chat.chat_openai("分析")
        self.assertEqual(raised.exception.reason, "unsupported_region")
        self.assertNotIn("sensitive", str(raised.exception))

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-token"})
    @mock.patch("openai_chat.requests.post")
    def test_text_stream_uses_sse_and_no_reasoning_deltas(self, post):
        events = [
            {"type": "response.reasoning_text.delta", "delta": "private"},
            {"type": "response.output_text.delta", "delta": "您好。"},
            {"type": "response.completed", "response": {"output": []}},
        ]
        post.return_value.iter_lines.return_value = [b"data: " + json.dumps(event).encode() for event in events]
        delivered = []
        self.assertEqual(openai_chat.chat_openai("分析", history=[], on_delta=delivered.append), "您好。")
        self.assertEqual(delivered, ["您好。"])
        self.assertTrue(post.call_args.kwargs["stream"])
        post.return_value.close.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-token"})
    @mock.patch("openai_chat.requests.post")
    def test_web_search_requires_real_call_and_returns_sanitized_sources(self, post):
        post.return_value.json.return_value = {"output": [
            {"type": "web_search_call", "status": "completed", "action": {"sources": [
                {"url": "https://example.com/news", "title": "新闻"},
                {"url": "javascript:alert(1)"},
                {"url": "https://secret@example.com/private"},
            ]}},
            {"type": "message", "content": [{"type": "output_text", "text": "有来源的回答", "annotations": [
                {"type": "url_citation", "url": "https://example.com/news", "title": "新闻"},
            ]}]},
        ]}
        sources = []
        self.assertEqual(openai_chat.chat_openai("最新新闻", history=[], web_search=True, sources=sources), "有来源的回答")
        self.assertEqual(sources, [{"url": "https://example.com/news", "title": "新闻"}])
        self.assertEqual(post.call_args.kwargs["json"]["tools"], [{"type": "web_search"}])
        self.assertEqual(post.call_args.kwargs["json"]["tool_choice"], "required")

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-token"})
    @mock.patch("openai_chat.requests.post")
    def test_unsourced_search_is_not_published_even_if_tokens_arrived(self, post):
        events = [
            {"type": "response.output_text.delta", "delta": "声称已经查到"},
            {"type": "response.completed", "response": {"output": []}},
        ]
        post.return_value.iter_lines.return_value = [b"data: " + json.dumps(event).encode() for event in events]
        delivered = []
        with self.assertRaises(openai_chat.OpenAIAPIUnavailable) as raised:
            openai_chat.chat_openai("最新新闻", history=[], web_search=True, on_delta=delivered.append)
        self.assertEqual(raised.exception.reason, "search_unverified")
        self.assertEqual(delivered, [])

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-token"})
    @mock.patch("openai_chat.requests.post")
    def test_cancellation_does_not_save_history(self, post):
        event = threading.Event()
        post.return_value.iter_lines.return_value = [
            b'data: {"type":"response.output_text.delta","delta":"hello"}',
            b'data: {"type":"response.completed","response":{"output":[]}}',
        ]
        with self.assertRaises(ChatCancelled):
            openai_chat.chat_openai("分析", history=[], on_delta=lambda _: event.set(), cancel_event=event)
        self.assertEqual(len(openai_chat._HISTORY), 0)
        post.return_value.close.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-only-token"})
    @mock.patch("openai_chat.requests.get")
    def test_probe_is_metadata_only(self, get):
        get.return_value.json.return_value = {"id": "gpt-6-astra"}
        self.assertEqual(openai_chat.probe_cloud()["status"], "available")
        self.assertIn("/v1/models/", get.call_args.args[0])
        self.assertNotIn("json", get.call_args.kwargs)
        self.assertEqual(get.call_args.kwargs["timeout"], (8, 5))

    def test_429_distinguishes_quota_from_rate_limit_without_raw_error(self):
        for details, expected in (
            ({"code": "insufficient_quota"}, "quota_exhausted"),
            ({"type": "insufficient_quota"}, "quota_exhausted"),
            ({"code": "rate_limit_exceeded"}, "rate_limited"),
            ({"type": "rate_limit_exceeded"}, "rate_limited"),
            ({"code": "unknown", "message": "private billing detail"}, "quota_or_rate_limit"),
        ):
            response = mock.Mock(status_code=429)
            response.json.return_value = {"error": details}
            self.assertEqual(openai_chat._http_reason(response), expected)

    @mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "JARVIS_OPENAI_MODEL": ""})
    @mock.patch("openai_chat._ENV_FILE")
    @mock.patch("dotenv.dotenv_values")
    def test_approved_config_loads_only_cloud_whitelist_without_interpolation(self, values, path):
        path.is_symlink.return_value = False
        path.is_file.return_value = True
        values.return_value = {
            "OPENAI_API_KEY": "unit-fake-token", "JARVIS_OPENAI_MODEL": "unit-model",
            "UNRELATED_TEST_VALUE": "must-not-load",
        }
        _REAL_LOAD_CONFIG()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "unit-fake-token")
        self.assertEqual(os.environ["JARVIS_OPENAI_MODEL"], "unit-model")
        self.assertNotEqual(os.environ.get("UNRELATED_TEST_VALUE"), "must-not-load")
        values.assert_called_once_with(path, interpolate=False)


if __name__ == "__main__":
    unittest.main()
