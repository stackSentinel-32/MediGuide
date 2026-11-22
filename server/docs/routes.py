from pathlib import Path
from typing import Literal
from ..providers import configured, require_allowed_provider
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from ..auth.routes import authentication
from ..config.db import reports_collection
from .vector import load_vectorstore, parse_pdf
from .jobs import WAKE, enqueue
from ..services import UPLOAD_DIR
from shutil import copyfileobj
import uuid
from datetime import datetime, timezone

router = APIRouter(prefix="/docs", tags=["Documents"])


class DocumentRequest(BaseModel):
    role: str


def save_report_metadata(doc_id: str, filename: str, owner: str, role: str, summary: str, report_type: str):
    reports_collection.insert_one({
        "doc_id": doc_id,
        "filename": filename,
        "owner": owner,
        "role": role,
        "summary": summary,
        "type": report_type,
        "uploaded_at": datetime.now(timezone.utc).isoformat()
    })


@router.post("/")
def upload_document(
    file: UploadFile = File(...),
    role: str = Form(...),
    user=Depends(authentication)
):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can upload reference documents.")

    doc_id = str(uuid.uuid4())
    save_path = Path(UPLOAD_DIR) / f"{doc_id}.pdf"
    save_path.parent.mkdir(parents=True, exist_ok=True)

    with save_path.open("wb") as destination:
        copyfileobj(file.file, destination)

    vector_warning = None
    try:
        load_vectorstore(parse_pdf(save_path), file.filename, role, doc_id)
    except Exception as e:
        vector_warning = str(e)

    save_report_metadata(
        doc_id=doc_id,
        filename=file.filename,
        owner=user["username"],
        role=role,
        summary="",
        report_type="reference"
    )

    response = {
        "message": "Document uploaded successfully",
        "doc_id": doc_id,
        "accessible_to": role
    }
    if vector_warning:
        response["warning"] = f"Vectorization failed: {vector_warning}"
    return response


@router.post("/report", status_code=202)
def upload_medical_report(
    summary_provider: Literal["ollama", "groq"] = Form("ollama"),
    file: UploadFile = File(...),
    user=Depends(authentication)
):
    if user["role"] != "patient":
        raise HTTPException(status_code=403, detail="Only patients can upload their medical reports.")

    require_allowed_provider("summary", summary_provider)
    if summary_provider == "groq" and not configured("groq"):
        raise HTTPException(503, "Groq summary requires a configured key and model.")
    return enqueue(file, user["username"], summary_provider)


@router.post("/reports/{doc_id}/retry", status_code=202)
def retry_report(doc_id: str, user=Depends(authentication)):
    for failed, pending in (("index_failed", "queued"), ("summary_failed", "indexed")):
        result = reports_collection.update_one(
            {"doc_id": doc_id, "owner": user["username"], "status": failed},
            {"$set": {"status": pending, "attempts": 0}},
        )
        if result.modified_count:
            WAKE["index" if pending == "queued" else "summary"].set()
            return {"doc_id": doc_id, "status": pending}
    raise HTTPException(409, "No failed processing stage is available to retry.")


@router.get("/reports")
def get_reports(user=Depends(authentication)):
    query = {} if user["role"] == "admin" else {"owner": user["username"]}
    documents = list(reports_collection.find(query))
    for doc in documents:
        doc.pop("_id", None)
        doc.pop("lease", None)
        doc.pop("until", None)
    return {"reports": documents}


@router.get("/reports/{doc_id}")
def get_report(doc_id: str, user=Depends(authentication)):
    query = {"doc_id": doc_id}
    if user["role"] != "admin":
        query["owner"] = user["username"]
    report = reports_collection.find_one(query)
    if not report:
        raise HTTPException(404, "Report unavailable.")

    report.pop("_id", None)
    report.pop("lease", None)
    report.pop("until", None)
    return report

@router.get("/reports/{doc_id}/pages/{page}")
def evidence_page(doc_id: str, page: int, user=Depends(authentication)):
    """Return the original text of one authorized page. Page numbers here start at one."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    from ..chat.chat_query import access_queries

    record = reports_collection.find_one({"doc_id": doc_id, "$or": access_queries(user["role"], user["username"])})
    if not record or page < 1:
        raise HTTPException(404, "Page unavailable.")
    root = Path(UPLOAD_DIR).resolve()
    # Earlier releases prefixed the original filename with the document ID.
    for name in (f"{doc_id}.pdf", f"{doc_id}_{record['filename']}"):
        path = (root / name).resolve()
        if path.parent == root and path.is_file():
            try:
                with path.open("rb") as pdf:
                    pages = PdfReader(pdf).pages
                    if page > len(pages):
                        break
                    return {"page": page, "text": pages[page - 1].extract_text() or ""}
            except (OSError, ValueError, PdfReadError):
                break
    raise HTTPException(404, "Page unavailable.")
