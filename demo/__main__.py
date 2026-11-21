"""Local recruiter walkthrough. Run python -m demo or python -m demo --check."""
import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from unittest.mock import patch

import uvicorn
from fastapi.testclient import TestClient
from langchain_chroma import Chroma
from langchain_core.embeddings import DeterministicFakeEmbedding
from pymongo.errors import ConnectionFailure

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "demo" / "specimen-log.pdf"
QUESTION = "What volume was recorded for sample B?"


class DocumentReplay:
    """Extract only the two demo measurements from the actual retrieved context."""
    def invoke(self, prompt):
        evidence, separator, question = prompt.rpartition("\n\nQuestion: ")
        if not separator:
            return " ".join(re.findall(r"Sample [AB] volume was [0-9]+ mL\.", prompt))
        sample = re.search(r"\bsample ([ab])\b", question, re.I)
        if sample and "volume" in question.lower():
            for number, excerpt in re.findall(r"\n\[(\d+)\] (.*?)(?=\n\[\d+\] |\Z)", evidence, re.S):
                fact = re.search(rf"Sample {sample[1].upper()} volume was [0-9]+ mL\.", excerpt)
                if fact:
                    return f"{fact[0]} [{number}]"
        return "The supplied pages do not contain that measurement."

    async def astream(self, prompt):
        for word in self.invoke(prompt).split():
            yield word + " "
            await asyncio.sleep(0)


@contextmanager
def local_app():
    with TemporaryDirectory() as directory, patch.dict(os.environ, {
        "UPLOAD_DIR": directory, "CHROMA_PERSIST_DIR": directory, "EMBEDDING_MODEL": "demo",
        "MONGODB_URI": "mongodb://127.0.0.1", "MEDIGUIDE_MOCK_DB": "1", "OLLAMA_MODEL": "demo",
        "OLLAMA_BASE_URL": "http://127.0.0.1:1", "GROQ_API_KEY": "", "OPENROUTER_API_KEY": "", "GEMINI_API_KEY": ""
    }):
        with patch("pymongo.MongoClient", side_effect=ConnectionFailure("temporary demo database")):
            from server.main import app
        from server import services
        index = Chroma(collection_name="recruiter_demo", embedding_function=DeterministicFakeEmbedding(size=32))
        with patch.object(services, "_vectorstore", index), patch.object(services, "_llm", DocumentReplay()):
            yield app


def check(app):
    auth = ("reviewer", "demo-session")
    with TestClient(app) as api:
        api.post("/signup", json={"username": auth[0], "password": auth[1], "role": "patient"}).raise_for_status()
        response = api.post("/docs/report", files={"file": (SAMPLE.name, SAMPLE.read_bytes(), "application/pdf")}, auth=auth)
        response.raise_for_status()
        report = response.json()["doc_id"]
        deadline = monotonic() + 30
        while not api.get(f"/docs/reports/{report}", auth=auth).json()["searchable"]:
            if monotonic() >= deadline:
                raise RuntimeError("Demo report did not become searchable")
            sleep(.05)
        reply = api.post("/chat/stream", auth=auth, json={"query": QUESTION})
        reply.raise_for_status()
        events = [json.loads(line) for line in reply.iter_lines()]
        answer = "".join(item["text"] for item in events if item["type"] == "text").strip()
        match = re.fullmatch(r"Sample B volume was 6 mL\. \[(\d+)\]", answer)
        assert match and events[-1]["type"] == "done", events
        source = events[0]["sources"][int(match[1]) - 1]
        assert source["doc_id"] == report and source["page"] == 1
        page = api.get(f"/docs/reports/{report}/pages/2", auth=auth)
        page.raise_for_status()
        assert "Sample B volume was 6 mL." in page.json()["text"]
        print(f"{QUESTION}\n{answer}\nVerified against {SAMPLE.name}, page 2.")


@contextmanager
def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        worker = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        worker.start()
        try:
            deadline = monotonic() + 15
            while not server.started:
                if not worker.is_alive() or monotonic() > deadline:
                    raise RuntimeError("Local demo API failed to start")
                sleep(.05)
            yield url
        finally:
            server.should_exit = True
            worker.join(10)


def browse(app, port):
    with serve(app) as url:
        env = dict(os.environ, BASE_URL=url, MEDIGUIDE_WALKTHROUGH="1")
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(ROOT / "client/main.py"),
                    "--server.address=127.0.0.1", f"--server.port={port}",
                    "--browser.gatherUsageStats=false"], env=env, check=True)


def check_groq(app, config):
    """Exercise an actual streamed provider call using only the bundled PDF."""
    import requests

    with serve(app) as url, patch.dict(os.environ, config), requests.Session() as api:
        api.auth = ("reviewer", "demo-session")
        api.post(url + "/signup", json={"username": api.auth[0], "password": api.auth[1]}, timeout=5).raise_for_status()
        upload = api.post(url + "/docs/report", files={"file": (SAMPLE.name, SAMPLE.read_bytes())}, timeout=10)
        upload.raise_for_status()
        report = url + "/docs/reports/" + upload.json()["doc_id"]
        deadline = monotonic() + 30
        while not api.get(report, timeout=5).json()["searchable"]:
            if monotonic() >= deadline:
                raise RuntimeError("Index timeout")
            sleep(.05)

        start = monotonic()
        first = None
        words = []
        sources = []
        with api.post(url + "/chat/stream", json={"provider":"groq", "query":QUESTION},
                      stream=True, timeout=40) as response:
            response.raise_for_status()
            completed = False
            for line in response.iter_lines():
                event = json.loads(line)
                if event["type"] == "sources":
                    sources = event["sources"]
                elif event["type"] == "text":
                    if first is None:
                        first = monotonic() - start
                    words.append(event["text"])
                elif event["type"] == "done":
                    completed = True
        answer = "".join(words)
        print(json.dumps({"answer": answer, "model": config["GROQ_MODEL"], "first_text_s": first,
                          "total_s": monotonic() - start}), flush=True)
        citations = re.findall(r"\[(\d+)\]", answer)
        assert completed and citations and re.search(r"6\s*mL", answer, re.I), "Unverified model answer"
        for citation in citations:
            assert 1 <= int(citation) <= len(sources)
            page = sources[int(citation) - 1]["page"] + 1
            text = api.get(f"{report}/pages/{page}", timeout=5).json()["text"]
            if page == 2 and "Sample B volume was 6 mL." in text:
                print("Verified live citation against specimen-log.pdf, page 2.")
                return
        raise AssertionError("Cited pages did not support the answer")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", action="store_true")
    checks.add_argument("--check-groq", action="store_true", help="Send the fictional specimen question to Groq")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()
    if args.check_groq:
        from dotenv import dotenv_values
        settings = {**dotenv_values(ROOT / "server" / ".env"), **os.environ}
        config = {k: settings.get(k) for k in ("GROQ_API_KEY", "GROQ_MODEL")}
        if not all(config.values()):
            parser.error("Configure GROQ_API_KEY and GROQ_MODEL for the live check")
    with local_app() as application:
        if args.check_groq:
            check_groq(application, config)
        elif args.check:
            check(application)
        else:
            browse(application, args.port)
