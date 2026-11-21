from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
import json
from typing import Literal
from ..providers import configured, groq_answer

from ..auth.routes import authentication
from ..services import get_llm, interactive_generation
from .chat_query import answer_query, prepare_answer, NO_EVIDENCE

router = APIRouter(prefix="/chat", tags=["Chat"])


class ChatRequest(BaseModel):
    provider: Literal["ollama", "groq"] = "ollama"
    query: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    top_k: int = Field(default=5, ge=1, le=20)


@router.post("/")
async def chat_endpoint(req: ChatRequest, user=Depends(authentication)):
    if req.provider == "ollama":
        return await run_in_threadpool(answer_query, req.query, req.top_k, user["role"], user["username"])
    prompt, sources = await run_in_threadpool(prepare_request, req, user)
    if not sources:
        return {"answer": NO_EVIDENCE, "sources": []}
    try:
        answer = "".join([part async for part in groq_answer(prompt)])
    except Exception:
        raise HTTPException(502, "Groq answer unavailable.")
    return {"answer": answer, "sources": sources}


def prepare_request(req, user):
    if req.provider == "groq" and not configured("groq"):
        raise HTTPException(503, "Groq chat requires a server key and model ID.")
    try:
        return prepare_answer(req.query, req.top_k, user["role"], user["username"])
    except Exception:
        raise HTTPException(503, "Document search unavailable.")


def event(kind, **fields):
    return json.dumps({"type": kind, **fields}) + "\n"


@router.post("/stream")
def stream_chat(req: ChatRequest, user=Depends(authentication)):
    prompt, sources = prepare_request(req, user)

    async def events():
        yield event("sources", sources=sources)
        if sources:
            try:
                if req.provider == "groq":
                    async for token in groq_answer(prompt):
                        yield event("text", text=token)
                else:
                    with interactive_generation():
                        model = await run_in_threadpool(get_llm)
                        async for token in model.astream(prompt):
                            if token:
                                yield event("text", text=token)
            except Exception:
                yield event("error", message="Answer interrupted. Please retry.")
                return
        else:
            yield event("text", text=NO_EVIDENCE)
        yield event("done")

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/demo/{route}/{case}")
async def provider_demo(route: str, case: str, user=Depends(authentication)):
    import httpx
    from ..providers import CASES, ENDPOINTS, run_demo
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access required.")
    if route not in (*ENDPOINTS, "auto") or case not in CASES:
        raise HTTPException(404, "Unknown demo selection.")
    async with httpx.AsyncClient(timeout=12) as client:
        return await run_demo(client, route, case)
