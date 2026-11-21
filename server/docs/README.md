# Server Documents Module

This directory handles document upload, PDF processing, summarization, and metadata storage.

## Files

- `routes.py` – document and report endpoints
- `vector.py` – document parsing, chunking, embedding, and summarization utilities

## Endpoints

- `POST /docs` – upload role-specific reference PDFs (admin only)
- `POST /docs/report` – upload a patient medical report and generate a summary
- `GET /docs/reports` – list accessible saved reports
- `GET /docs/reports/{doc_id}` – retrieve a single saved report

## How it works

1. Uploaded file bytes are saved into `uploaded_docs/`.
2. The server parses the PDF and splits it into text chunks.
3. Each chunk is embedded using the configured HuggingFace model.
4. The vectors are stored in ChromaDB with metadata including source, role, and doc_id.
5. Patient report uploads are also summarized using the Ollama LLM.
6. Report metadata is saved in MongoDB under the `reports` collection.

## Notes

- Admin users upload reference documents for doctors, nurses, patients, or others.
- Patients can upload their own reports and receive a summary.
- Document vectors are filtered by role during chat search.

## Patient report processing

POST /docs/report returns HTTP 202 with a document ID and current status. Read
GET /docs/reports/{doc_id} or use Refresh reports in My Reports. A searchable
report can be used in chat while its summary runs. Reference uploads keep their
existing synchronous API.

Starting FastAPI starts separate indexing and summary workers. MongoDB stores
the queue in report records. Run the API with a persistent shared UPLOAD_DIR
on a filesystem supporting hard links and the same vector store across restarts. Use a single API process with
embedded Chroma. A worker renews its lease while processing. On restart, an
expired lease becomes eligible for recovery within 90 seconds. Three failed
attempts mark the stage as failed. The owner may retry that stage from My
Reports. Completed summary jobs remove their temporary extracted page text.

Identical uploads from the same owner reuse the report ID for this processing
version. Legacy reports retain their existing IDs. Patient uploads support
text PDFs up to 32 MiB. Image-only documents currently require a searchable
PDF export. Mock MongoDB is for ephemeral demos only.

For the synthetic integration check install reportlab and run
`python -m server.benchmark_reports` from the repository root. It uses ten
five-page fixtures, real PDF extraction and Chroma retrieval, deterministic test
embeddings, an in-memory MongoDB substitute, and simulated model responses.
