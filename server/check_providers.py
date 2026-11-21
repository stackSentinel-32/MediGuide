"""Offline checks for provider requests, routing, benchmark metrics, and summaries."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from .provider_check import stats
from .providers import CASES, ENDPOINTS, measure, run_demo


def stream(answer, complete=True):
    events = [{"model": "resolved-model", "choices": [{"delta": {"content": json.dumps(answer)}}]}]
    if complete:
        events.append({"choices": [{"delta": {}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": .001}})
    return httpx.Response(200, text="".join("data: "+json.dumps(e)+"\n\n" for e in events)+"data: [DONE]\n\n")


class ProviderChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = {f"{p.upper()}_{field}": "fixture" for p in ENDPOINTS for field in ("API_KEY", "MODEL")}
        self.enterContext(patch.dict(os.environ, config))

    async def test_same_fixture_and_metrics_all_services(self):
        messages = []
        def handler(request):
            body = json.loads(request.content)
            messages.append(body["messages"])
            self.assertEqual(request.headers["authorization"], "Bearer fixture")
            if "googleapis" in request.url.host:
                self.assertEqual(body["response_format"]["type"], "json_schema")
            return stream({"value":"6","unit":"mL","page":7})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rows = [await measure(client, p, "sample_b") for p in ENDPOINTS]
        self.assertEqual(messages[1:], messages[:2])
        self.assertEqual(stats(rows)["completed"], 3)
        self.assertEqual(stats(rows)["citation_accuracy"], 1)
        self.assertEqual(stats(rows)["value_unit_accuracy"], 1)
        self.assertEqual(stats(rows)["usd_per_completion"], .001)

    async def test_single_fallback_on_rate_limit(self):
        calls = []
        def handler(request):
            calls.append(request.url.host)
            if len(calls) == 1:
                return httpx.Response(429)
            self.assertIn("Groq", json.loads(request.content)["provider"]["ignore"])
            return stream({"value":None,"unit":None,"page":None})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await run_demo(client, "auto", "missing")
        self.assertEqual([r["status"] for r in result["attempts"]], ["http_429", "ok"])
        self.assertEqual(len(calls), 2)

    async def test_interruption_and_wrong_evidence(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: stream({},False))) as client:
            result = await run_demo(client, "auto", "sample_b")
        self.assertEqual(len(result["attempts"]), 1)
        self.assertNotEqual(result["attempts"][0]["status"], "ok")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: stream(
                {"value":"6","unit":"mL","page":2}))) as client:
            row = await measure(client, "groq", "sample_b")
        self.assertTrue(row["value_unit_correct"])
        self.assertFalse(row["citation_correct"])

    async def test_groq_chat_stream_and_truncation(self):
        from .providers import groq_answer
        for reason in ("stop", "length"):
            events = [{"choices":[{"delta":{"content":"6 mL \u30102\u3011"}}]},
                      {"choices":[{"delta":{}, "finish_reason":reason}]}]
            wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=wire)))
            with patch("server.providers.httpx.AsyncClient", return_value=client):
                if reason == "stop":
                    self.assertEqual([t async for t in groq_answer("fixture")], ["6 mL [2]"])
                else:
                    with self.assertRaises(RuntimeError):
                        _ = [t async for t in groq_answer("fixture")]

    async def test_unconfigured_no_network(self):
        with patch.dict(os.environ, {}, clear=True):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: self.fail("Network call"))) as client:
                rows = [await measure(client, p, "sample_b") for p in ENDPOINTS]
        self.assertTrue(all(row["status"] == "unconfigured" for row in rows))


class SummaryChecks(unittest.TestCase):
    def test_short_and_long_reports(self):
        with patch.dict(os.environ, {"EMBEDDING_MODEL":"fixture","CHROMA_PERSIST_DIR":"fixture",
                                     "OLLAMA_MODEL":"fixture","OLLAMA_BASE_URL":"http://localhost:1"}):
            from .docs import vector
        fixture = SimpleNamespace(page_content=("x" * 50 + "\nSample B volume was 6 mL. " * 4), metadata={"page": 1})
        chunks = list(vector.page_chunks([fixture], size=68, overlap=12))
        self.assertEqual(chunks[0], ("x" * 50, 1))
        self.assertTrue(any("Sample B volume was 6 mL." in text for text, _ in chunks))
        with self.assertRaises(ValueError):
            list(vector.page_chunks([fixture], size=12, overlap=12))
        pages = [SimpleNamespace(page_content="sample",metadata={"page":0}) for _ in range(3)]
        with patch.object(vector, "invoke_llm", return_value="summary") as model:
            self.assertEqual(vector.summarize_pdf(pages), "summary")
            model.assert_called_once()
            model.reset_mock()
            for i, page in enumerate(pages):
                page.page_content = str(i)*1500
            vector.summarize_pdf(pages)
            self.assertEqual(model.call_count, 4)
            self.assertIn("Partial summaries",model.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
