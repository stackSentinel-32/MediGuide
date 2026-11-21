from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth.routes import router as auth_router
from .docs.routes import router as docs_router
from .chat.routes import router as chat_router

from contextlib import asynccontextmanager
from threading import Event, Thread
from .docs.jobs import STAGES, WAKE, run_worker


@asynccontextmanager
async def lifespan(app):
    stop = Event()
    workers = [Thread(target=run_worker, args=(stage, stop), daemon=True) for stage in STAGES]
    for worker in workers:
        worker.start()
    try:
        yield
    finally:
        stop.set()
        for wake in WAKE.values():
            wake.set()
        for worker in workers:
            worker.join(timeout=1)


app = FastAPI(
    lifespan=lifespan,
    title="MediGuide API",
    description="AI-powered medical assistant API",
    version="0.1.0"
)

# CORS — allow Streamlit client and local dev origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(docs_router)
app.include_router(chat_router)


@app.get("/")
def health_check():
    return {"status": "ok", "service": "MediGuide API"}
