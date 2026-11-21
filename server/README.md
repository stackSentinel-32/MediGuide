# Server / FastAPI Backend

This folder contains the backend API for MediGuide.

## What it does

- Hosts FastAPI application in `server/main.py`
- Registers authentication, document, and chat routers
- Handles PDF uploads, summary generation, vector storage, and chat responses
- Connects to MongoDB and ChromaDB

## Key components

- `server/main.py` – application entry point
- `server/auth/routes.py` – signup/login and authentication logic
- `server/docs/routes.py` – document upload and retrieval endpoints
- `server/chat/routes.py` – chat query endpoint
- `server/config/db.py` – MongoDB connection and fallback mock database
- `server/services.py` – embeddings, ChromaDB, and LLM singleton initialization

## Environment

The server loads `server/.env` for:

- `MONGODB_URI`
- `MONGODB_DB_NAME`
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- `EMBEDDING_MODEL`
- `CHROMA_PERSIST_DIR`

## Run locally

```bash
pip install -r server/requirements.txt
uvicorn server.main:app --reload --port 8001
```

## Connection overview

- The client communicates with this backend using REST calls.
- The backend uses HTTP Basic auth for login and protected endpoints.
- Document uploads are saved in `uploaded_docs/`.
- Vector embeddings are stored in ChromaDB using the configured `CHROMA_PERSIST_DIR`.
- Chat queries run a similarity search filtered by user role.

## Notes

- Isolated demos may enable an in-memory fallback with MEDIGUIDE_MOCK_DB=1.
- Verify that the client `BASE_URL` points to the same URL and port used for the backend.

## Synthetic provider comparison

The admin dashboard includes three fictional extraction questions. The demo API
accepts only built-in case and route names. Patient summaries and default chat use Ollama. Selecting Groq in Chat sends
the question and authorized excerpts to that service. Set replacement credentials and model IDs on the server using
GROQ_API_KEY / GROQ_MODEL, GEMINI_API_KEY / GEMINI_MODEL, and
OPENROUTER_API_KEY / OPENROUTER_MODEL. Select a streaming model for each
provider and a structured-output-capable Gemini model.

Auto routing tries Groq and can try OpenRouter once before any text arrives.
OpenRouter excludes Groq as an upstream. Set OPENROUTER_UPSTREAM to pin an
eligible alternative. Each call has an 18-second budget within a 30-second
route budget. A partial or truncated response counts as a failure.

Run `python -m server.provider_check all --repeats 5` to compare each service
on identical inputs, or replace all with auto to measure fallback behavior.
Five repeats make 15 requests per configured service. Each JSON line includes
either a failure status or the resolved model, answer and timings. Summaries
show completed-call latency percentiles, exact value-and-unit accuracy, page
accuracy, and cost where available. First-text timing is measured at the server.
Cost uses provider-reported usage.cost, or token counts and optional
<PROVIDER>_INPUT_USD_PER_MILLION and <PROVIDER>_OUTPUT_USD_PER_MILLION rates.
Missing usage or rates produce an unknown cost. Run larger samples before
choosing a default model based on P95.

Public signup creates patient accounts. Provision staff roles in the trusted
database and review existing privileged users before deployment. Chat resolves
permitted document IDs from MongoDB on each request, including legacy vectors.
MongoDB is required by default. MEDIGUIDE_MOCK_DB=1 enables the in-memory
fallback only for isolated development.
