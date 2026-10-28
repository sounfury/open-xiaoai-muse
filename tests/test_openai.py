import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_managers():
    sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
    sys.modules.setdefault(
        "aiohttp",
        types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
    )
    sys.modules.setdefault("requests", types.SimpleNamespace(post=None, get=None))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for name in ("core.hermes", "core.openai"):
        sys.modules.pop(name, None)
    openai = importlib.import_module("core.openai").OpenAIManager
    hermes = importlib.import_module("core.hermes").HermesManager
    return openai, hermes


class OpenAIHeadersTest(unittest.TestCase):
    """通用 OpenAI 兼容后端：不发 Hermes 的请求头。"""

    def setUp(self):
        self.manager, _ = load_managers()
        self.manager._api_key = ""
        self.manager._session_key = "agent:default:open-xiaoai-bridge"

    def test_plain_headers(self):
        self.assertEqual({"Content-Type": "application/json"}, self.manager._headers())

    def test_bearer_auth(self):
        self.manager._api_key = "secret"
        self.assertEqual("Bearer secret", self.manager._headers()["Authorization"])
        self.assertNotIn("X-Hermes-Session-Id", self.manager._headers())

    def test_named_sse_events_are_parsed_as_chunks(self):
        self.assertIsNone(self.manager._sse_events("hermes.tool.progress", {"tool": "x"}))

    def test_no_server_session(self):
        self.manager.begin_conversation()
        self.manager.end_conversation()
        self.assertNotIn("X-Hermes-Session-Id", self.manager._headers())


class HermesHeadersTest(unittest.TestCase):
    def setUp(self):
        _, self.manager = load_managers()
        self.manager._api_key = ""
        self.manager._session_key = "agent:default:open-xiaoai-bridge"
        self.manager._session_header = "X-Hermes-Session-Key"
        self.manager._session_id_header = "X-Hermes-Session-Id"
        self.manager._conversation_id = None

    def test_sends_memory_scope_header(self):
        self.assertEqual(
            "agent:default:open-xiaoai-bridge",
            self.manager._headers()["X-Hermes-Session-Key"],
        )

    def test_empty_session_header_disables_it(self):
        self.manager._session_header = ""
        self.assertNotIn("X-Hermes-Session-Key", self.manager._headers())

    def test_session_header_omitted_when_session_key_empty(self):
        self.manager._session_key = ""
        self.assertNotIn("X-Hermes-Session-Key", self.manager._headers())

    def test_session_id_needs_api_key(self):
        self.assertNotIn("X-Hermes-Session-Id", self.manager._headers())

    def test_one_session_id_per_conversation(self):
        self.manager._api_key = "secret"
        self.manager.begin_conversation()
        first = self.manager._headers()["X-Hermes-Session-Id"]
        self.assertEqual(first, self.manager._headers()["X-Hermes-Session-Id"])
        self.assertEqual("Bearer secret", self.manager._headers()["Authorization"])
        self.manager.end_conversation()
        # 单次请求每次新开一个
        self.assertNotEqual(first, self.manager._headers()["X-Hermes-Session-Id"])

    def test_tool_progress_event(self):
        event = {"tool": "web_search", "status": "running"}
        self.assertEqual([("tool", event)], self.manager._sse_events("hermes.tool.progress", event))
        self.assertEqual([], self.manager._sse_events("hermes.tool.progress", "bad"))
        self.assertIsNone(self.manager._sse_events(None, {"choices": []}))


class BackendStateTest(unittest.TestCase):
    """HermesManager 继承 OpenAIManager，但运行状态和配置段各自独立。"""

    def test_state_not_shared(self):
        openai, hermes = load_managers()
        self.assertIsNot(openai._response_events, hermes._response_events)
        openai._initialized = True
        self.assertFalse(hermes._initialized)

    def test_identity(self):
        openai, hermes = load_managers()
        self.assertEqual(("openai", "openai", ("OPENAI_ENABLE",)), (openai.NAME, openai.CONFIG_SECTION, openai.ENABLE_ENV))
        self.assertEqual(("hermes", "hermes", ("HERMES_ENABLE",)), (hermes.NAME, hermes.CONFIG_SECTION, hermes.ENABLE_ENV))

    def test_registry(self):
        load_managers()
        from core.backends import backend_names, get_backend

        self.assertEqual(("hermes", "openai", "openclaw", "qwenpaw"), backend_names())
        for name in backend_names():
            backend = get_backend(name)
            assert backend is not None
            self.assertEqual(name, backend.NAME)
        self.assertIsNone(get_backend("xiaozhi"))

    def test_with_rule_prompt(self):
        _, hermes = load_managers()
        hermes._rule_prompt = "纯文字"
        hermes._rule_prompt_for_skill = ""
        self.assertEqual("开灯\n纯文字", hermes.with_rule_prompt("开灯"))
        self.assertEqual("开灯", hermes.with_rule_prompt("开灯", for_skill=True))


if __name__ == "__main__":
    unittest.main()
