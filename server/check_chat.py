"""Offline regression suite. Run python -m server.check_chat from the repo root."""
import asyncio
import json
import os
import socket
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
from unittest.mock import patch

import requests
import uvicorn
from langchain_core.embeddings import DeterministicFakeEmbedding
from pymongo.errors import ConnectionFailure


class SyntheticModel:
    def __init__(self):
        self.release = Event()
        self.fail = False

    def invoke(self, prompt):
        return "6 mL [1]"

    async def astream(self, prompt):
        yield "6 mL"
        while not self.release.is_set():
            await asyncio.sleep(.01)
        if self.fail:
            raise RuntimeError("synthetic interruption")
        yield " [1]"


class ChatChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from langchain_chroma import Chroma
        from .benchmark_reports import sample
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        root = Path(stack.enter_context(TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {
            "MEDIGUIDE_MOCK_DB": "1", "MONGODB_URI": "mongodb://localhost",
            "EMBEDDING_MODEL": "fake", "CHROMA_PERSIST_DIR": str(root / "index"),
            "UPLOAD_DIR": str(root), "OLLAMA_MODEL": "fake", "OLLAMA_BASE_URL": "http://127.0.0.1:1"}))
        with patch("pymongo.MongoClient", side_effect=ConnectionFailure("offline test")):
            from .main import app
        from . import services
        from .config.db import reports_collection, users_collection
        from .auth.hash_utils import hash_password
        from .chat.chat_query import prepare_answer
        cls.prepare = staticmethod(prepare_answer)
        cls.model = SyntheticModel()
        cls.model.release.set()
        index = Chroma(collection_name="chat_checks", embedding_function=DeterministicFakeEmbedding(size=32))
        stack.enter_context(patch.object(services, "_vectorstore", index))
        stack.enter_context(patch.object(services, "_llm", cls.model))
        password = hash_password("fixture")
        for name in ("alice", "bob", "charlie"):
            users_collection.insert_one({"username": name, "role": "patient", "password": password})
        for key, owner, kind, role, ready in (
            ("own", "alice", "patient_report", "patient", True),
            ("other", "bob", "patient_report", "patient", True),
            ("pending", "alice", "patient_report", "patient", False),
            ("clinician", "staff", "reference", "doctor", True),
            ("shared", "staff", "reference", "patient", True)):
            reports_collection.insert_one(dict(doc_id=key, filename=key+".pdf", owner=owner,
                                          type=kind, role=role, searchable=ready))
            (root / (key + ".pdf")).write_bytes(sample(1))
            for n in range(2):
                index.add_texts([key + " volume is 6 mL"], [{"doc_id": key, "page": 1, "source": key+".pdf"}],
                                ids=[key+str(n)])
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()

        def stop():
            cls.model.release.set()
            server.should_exit = True
            thread.join(5)
            sock.close()

        stack.callback(stop)
        deadline = monotonic() + 10
        while not server.started:
            if monotonic() > deadline:
                raise RuntimeError("Test API startup timed out")
            sleep(.02)

    def post(self, route, **kwargs):
        return requests.post(self.url + route, auth=("alice", "fixture"),
                             json={"query": "Sample volume?", "top_k": 20}, timeout=10, **kwargs)

    def test_stream_flushes_before_model_finishes(self):
        self.model.release.clear()
        try:
            with self.post("/chat/stream", stream=True) as response:
                response.raise_for_status()
                events = (json.loads(line) for line in response.iter_lines())
                sources = next(events)["sources"]
                self.assertEqual({s["doc_id"] for s in sources}, {"own", "shared"})
                self.assertEqual(len(sources), 2)
                self.assertEqual(next(events), {"type": "text", "text": "6 mL"})
                self.model.release.set()
                self.assertEqual([item["type"] for item in events], ["text", "done"])
        finally:
            self.model.release.set()

    def test_missing_report_and_private_report_share_status(self):
        for doc_id in ("other", "missing"):
            result = requests.get(self.url + "/docs/reports/" + doc_id, auth=("alice", "fixture"), timeout=5)
            self.assertEqual(result.status_code, 404)

    def test_cloud_chat_uses_authorized_context(self):
        prompts = []
        async def model(prompt):
            prompts.append(prompt)
            yield "6 mL [1]"
        with patch("server.chat.routes.groq_answer", model), patch("server.chat.routes.configured", return_value=True):
            for path in ("/chat/", "/chat/stream"):
                result = requests.post(self.url + path, json={"provider":"groq","query":"volume?","top_k":20},
                                       auth=("alice", "fixture"), timeout=5)
                self.assertEqual(result.status_code, 200)
                self.assertIn("6 mL [1]", result.text)
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertIn("own volume", prompt)
            self.assertNotIn("other volume", prompt)

    def test_admin_demo_scope(self):
        from .config.db import users_collection
        from .auth.hash_utils import hash_password
        path = self.url + "/chat/demo/auto/sample_a"
        self.assertEqual(requests.post(path, auth=("alice", "fixture"), timeout=5).status_code, 403)
        users_collection.insert_one({"username": "demo-admin", "password": hash_password("fixture"), "role":"admin"})
        with patch.dict(os.environ, {"GROQ_API_KEY":"", "OPENROUTER_API_KEY":""}):
            result = requests.post(path, auth=("demo-admin", "fixture"), timeout=5)
        self.assertEqual(result.status_code, 200)
        self.assertEqual([a["status"] for a in result.json()["attempts"]], ["unconfigured", "unconfigured"])
        invalid = requests.post(self.url + "/chat/demo/groq/arbitrary", auth=("demo-admin", "fixture"), timeout=5)
        self.assertEqual(invalid.status_code, 404)

    def test_admin_demo_ui(self):
        from streamlit.testing.v1 import AppTest
        from .config.db import users_collection
        from .auth.hash_utils import hash_password
        users_collection.insert_one({"username": "ui-admin", "password": hash_password("fixture"), "role":"admin"})
        ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "client" / "main.py"), default_timeout=15)
        with patch.dict(os.environ, {"BASE_URL": self.url, "GROQ_API_KEY":"", "OPENROUTER_API_KEY":""}):
            for key, value in dict(username="ui-admin", password="fixture", role="admin", logged_in=True).items():
                ui.session_state[key] = value
            ui.run()
            next(button for button in ui.button if button.label == "Run provider demo").click().run()
            self.assertFalse(ui.exception)
            self.assertTrue(any("groq needs" in notice.value for notice in ui.info))

    def test_interruption(self):
        with patch.object(self.model, "fail", True):
            events = [json.loads(line) for line in self.post("/chat/stream").iter_lines()]
        self.assertEqual([e["type"] for e in events], ["sources", "text", "error"])

    def test_legacy_response_and_access(self):
        response = self.post("/chat/").json()
        self.assertEqual(response["answer"], "6 mL [1]")
        self.assertEqual({s["doc_id"] for s in response["sources"]}, {"own", "shared"})
        for key, status in (("own", 200), ("shared", 200), ("other", 404), ("pending", 404), ("clinician", 404)):
            page = requests.get(f"{self.url}/docs/reports/{key}/pages/2", auth=("alice", "fixture"), timeout=5)
            self.assertEqual(page.status_code, status)
            if status == 200:
                self.assertIn("page 2", page.json()["text"])
        self.assertEqual(self.prepare("test", 5, "nurse", "nobody")[1], [])
        self.assertEqual(requests.post(self.url+"/chat/stream",json={"query":"test"},timeout=5).status_code,401)

    def test_no_evidence_and_input_validation(self):
        from .config.db import users_collection
        from .auth.hash_utils import hash_password
        users_collection.insert_one({"username": "nurse", "role": "nurse", "password": hash_password("fixture")})
        reply = requests.post(self.url + "/chat/stream", auth=("nurse", "fixture"),
                              json={"query": "Sample volume?"}, timeout=5)
        events = [json.loads(line) for line in reply.iter_lines()]
        self.assertEqual(events[0]["sources"], [])
        self.assertIn("No relevant", events[1]["text"])
        self.assertEqual(events[2]["type"], "done")
        invalid = requests.post(self.url + "/chat/stream", auth=("alice", "fixture"),
                                json={"query": "  "}, timeout=5)
        self.assertEqual(invalid.status_code, 422)
        staff = requests.post(self.url + "/signup", json={
            "username": "untrusted", "password": "fixture", "role": "admin"}, timeout=5)
        self.assertEqual(staff.status_code, 422)

    def test_summary_yields_to_active_chat(self):
        from .services import interactive_generation, invoke_llm
        started, finished = Event(), Event()

        def summarize():
            started.set()
            invoke_llm("Synthetic summary")
            finished.set()

        with interactive_generation():
            worker = Thread(target=summarize, daemon=True)
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(.1))
        self.assertTrue(finished.wait(2))
        worker.join()

    def test_streamlit_workflow(self):
        from streamlit.testing.v1 import AppTest
        ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "client" / "main.py"), default_timeout=15)
        with patch.dict(os.environ, {"BASE_URL": self.url}):
            for key, value in dict(username="alice", password="fixture", role="patient", logged_in=True).items():
                ui.session_state[key] = value
            ui.run()
            next(field for field in ui.text_input if field.label=="Your query").set_value("Sample volume?")
            next(button for button in ui.button if button.label=="Send").click().run()
            self.assertFalse(ui.exception)
            self.assertTrue(ui.session_state["last_reply"]["complete"])
            self.assertIn("6 mL [1]", [text.value for text in ui.text])
            next(button for button in ui.button if button.label=="Read full page").click().run()
            self.assertTrue(any("page 2" in text.value for text in ui.text))
            next(button for button in ui.button if button.label=="Logout").click().run()
            self.assertNotIn("last_reply", ui.session_state)


if __name__ == "__main__":
    unittest.main()
