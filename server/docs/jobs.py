"""Persistent patient report processing. Each stage claims its work atomically."""
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from uuid import uuid4

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..config.db import reports_collection
from ..services import UPLOAD_DIR
from .vector import load_vectorstore, parse_pdf, summarize_pdf

STORAGE = Path(UPLOAD_DIR).resolve()
LEASE_SECONDS = 90
MAX_ATTEMPTS = 3
MAX_BYTES = 32 * 1024 * 1024
STAGES = {"index": ("queued", "indexing"), "summary": ("indexed", "summarizing")}
WAKE = {stage: Event() for stage in STAGES}
logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def enqueue(file, owner, summary_provider="ollama"):
    digest = hashlib.sha256(json.dumps(["report-v2", owner]).encode())
    if summary_provider != "ollama":
        digest.update(summary_provider.encode())
    size = 0
    with NamedTemporaryFile(dir=STORAGE, delete=False) as tmp:
        path = Path(tmp.name)
    try:
        with path.open("wb") as target:
            while block := file.file.read(64 * 1024):
                if size == 0 and b"%PDF-" not in block[:1024]:
                    raise HTTPException(422, "Please upload a PDF document.")
                size += len(block)
                if size > MAX_BYTES:
                    raise HTTPException(413, "PDF exceeds the 32 MiB limit.")
                digest.update(block)
                target.write(block)
        if not size:
            raise HTTPException(422, "The PDF is empty.")
        doc_id = digest.hexdigest()
        # An atomic link publishes only a fully written file. A retry can reuse it
        # even while another worker has the original open on Windows.
        try:
            os.link(path, STORAGE / f"{doc_id}.pdf")
        except FileExistsError:
            pass
        record = dict(doc_id=doc_id, owner=owner, filename=file.filename, role="patient",
                      type="patient_report", summary_provider=summary_provider, status="queued", searchable=False, summary="",
                      attempts=0, uploaded_at=now().isoformat())
        try:
            reports_collection.update_one({"_id": doc_id}, {"$setOnInsert": record}, upsert=True)
        except DuplicateKeyError:
            pass  # A concurrent upload inserted this owner's identical report.
        WAKE["index"].set()
        return reports_collection.find_one({"_id": doc_id}, {"_id": 0, "lease": 0, "until": 0})
    finally:
        path.unlink(missing_ok=True)


def process_one(stage):
    pending, active = STAGES[stage]
    token = uuid4().hex
    job = reports_collection.find_one_and_update(
        {"$or": [{"status": pending}, {"status": active, "until": {"$lte": now()}}]},
        {"$set": {"status": active, "lease": token, "until": now() + timedelta(seconds=LEASE_SECONDS)},
         "$inc": {"attempts": 1}}, sort=[("uploaded_at", 1)], return_document=ReturnDocument.AFTER)
    if not job:
        return False

    claim = {"_id": job["_id"], "lease": token}
    done = Event()

    def heartbeat():
        while not done.wait(LEASE_SECONDS / 3):
            try:
                if not reports_collection.update_one(claim, {"$set": {
                    "until": now() + timedelta(seconds=LEASE_SECONDS)}}).matched_count:
                    return
            except Exception:
                logger.exception("Could not renew report job lease")

    pulse = Thread(target=heartbeat, daemon=True)
    pulse.start()
    doc_id = job["doc_id"]
    pages_file = STORAGE / f"{doc_id}.pages.json"
    temp = STORAGE / f"{doc_id}.{token}.tmp"
    try:
        if job["attempts"] > MAX_ATTEMPTS:
            raise RuntimeError("Interrupted too many times")
        if stage == "index":
            pages = parse_pdf(STORAGE / f"{doc_id}.pdf")
            if not any(page.page_content.strip() for page in pages):
                raise ValueError("No readable text")
            temp.write_text(json.dumps([p.page_content for p in pages]), encoding="utf-8")
            os.replace(temp, pages_file)
            load_vectorstore(pages, job["filename"], job["role"], doc_id)
            result = dict(status="indexed", searchable=True, indexed_at=now().isoformat(), attempts=0)
        else:
            from langchain_core.documents import Document
            pages = [Document(page_content=t) for t in json.loads(pages_file.read_text(encoding="utf-8"))]
            result = dict(status="complete", summary=(summarize_pdf(pages, "groq") if job.get("summary_provider") == "groq" else summarize_pdf(pages)), completed_at=now().isoformat())
    except Exception as exc:
        logger.warning("Report %s %s attempt %s failed (%s)", doc_id, stage, job["attempts"], type(exc).__name__)
        result = {"status": pending if job["attempts"] < MAX_ATTEMPTS else f"{stage}_failed"}
    finally:
        done.set()
        pulse.join()
        temp.unlink(missing_ok=True)
    updated = reports_collection.update_one(claim, {"$set": result, "$unset": {"lease": "", "until": ""}})
    if updated.matched_count and result["status"] == "indexed":
        WAKE["summary"].set()
    if updated.matched_count and result["status"] == "complete":
        pages_file.unlink(missing_ok=True)
    return True


def run_worker(stage, stop):
    wake = WAKE[stage]
    while not stop.is_set():
        wake.clear()  # Clear before checking Mongo so a concurrent enqueue cannot be lost.
        try:
            if process_one(stage):
                continue
        except Exception:
            logger.exception("Report worker could not process its queue")
        wake.wait(0.5)
