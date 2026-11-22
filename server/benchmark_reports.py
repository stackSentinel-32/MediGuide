"""Run an isolated synthetic upload benchmark: python -m server.benchmark_reports."""
import os
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter, sleep
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from langchain_core.embeddings import DeterministicFakeEmbedding
from pymongo.errors import ConnectionFailure
from reportlab.pdfgen.canvas import Canvas


def sample(number):
    buffer = BytesIO()
    pdf = Canvas(buffer, invariant=True)
    for page in range(5):
        pdf.drawString(50, 760, f"Synthetic report {number}, page {page + 1}. Sample volume is 6 mL.")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def run(directory):
    os.environ.update(CHAT_PROVIDER="choice", SUMMARY_PROVIDER="choice", MEDIGUIDE_MOCK_DB="1", MONGODB_URI="mongodb://localhost", EMBEDDING_MODEL="synthetic",
                      CHROMA_PERSIST_DIR=str(directory / "index"), OLLAMA_MODEL="synthetic",
                      OLLAMA_BASE_URL="http://127.0.0.1:1")
    with patch("pymongo.MongoClient", side_effect=ConnectionFailure("isolated benchmark")):
        from .main import app
    from .auth.routes import authentication
    from .config.db import reports_collection
    from .docs import jobs
    from . import services
    from langchain_chroma import Chroma

    # Ephemeral Chroma avoids persistent database file handles on Windows.
    index = Chroma(collection_name="synthetic_reports", embedding_function=DeterministicFakeEmbedding(size=32))
    release = Event()
    started = Event()

    def summary(pages):
        started.set()
        if not release.wait(120):
            raise RuntimeError("Benchmark summary gate timed out")
        return "Synthetic summary"

    app.dependency_overrides[authentication] = lambda: {"username": "fixture", "role": "patient"}
    times = []
    with patch.object(jobs, "STORAGE", directory), patch.object(services, "_vectorstore", index), \
         patch.object(jobs, "summarize_pdf", side_effect=summary), \
         patch.object(services, "_llm", Mock(invoke=Mock(return_value="6 mL"))), TestClient(app) as api:
        try:
            for number in range(10):
                data = sample(number)
                start = perf_counter()
                files = {"file": ("fixture.pdf", data, "application/pdf")}
                response = api.post("/docs/report", files=files)
                assert response.status_code == 202, response.text
                doc_id = response.json()["doc_id"]
                ack = perf_counter() - start
                while not api.get(f"/docs/reports/{doc_id}").json()["searchable"]:
                    assert perf_counter() - start < 180, "Index timed out"
                    sleep(.02)
                elapsed = perf_counter() - start
                times.append((ack, elapsed))
                if number == 0:
                    assert started.wait(10)
                    chat = api.post("/chat/", json={"query": "Sample volume?"}).json()
                    assert chat["sources"][0]["doc_id"] == doc_id
                    assert api.get(f"/docs/reports/{doc_id}").json()["status"] == "summarizing"
                    assert api.post("/docs/report", files=files).json()["doc_id"] == doc_id
                    assert reports_collection.count_documents({}) == 1
                    release.set()
                print(f"Fixture {number + 1}: accepted {ack:.3f}s, searchable {elapsed:.3f}s", flush=True)
            end = perf_counter() + 20
            while reports_collection.count_documents({"status": "complete"}) != 10:
                assert perf_counter() < end, "Summary timed out"
                sleep(.05)
        finally:
            release.set()
            app.dependency_overrides.clear()
    from datetime import timedelta
    reports_collection.update_one({"doc_id": doc_id}, {"$set": {
        "status": "indexing", "until": jobs.now() - timedelta(seconds=1), "attempts": 1}})
    with patch.object(jobs, "STORAGE", directory), patch.object(services, "_vectorstore", index):
        assert jobs.process_one("index")
    assert reports_collection.find_one({"doc_id": doc_id})["status"] == "indexed"
    print("Verified chat during summarization, duplicate upload, and expired lease recovery.")


if __name__ == "__main__":
    with TemporaryDirectory() as tmp:
        run(Path(tmp))
