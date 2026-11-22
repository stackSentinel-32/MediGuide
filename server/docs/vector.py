"""
Document vectorization and PDF summarization utilities.
Uses shared services for embeddings, vectorstore, and LLM access.
"""

from pathlib import Path

from ..services import (
    get_vectorstore,
    invoke_llm,
)


def parse_pdf(file_path: Path):
    from langchain_core.documents import Document
    from pypdf import PdfReader

    with file_path.open("rb") as source:
        reader = PdfReader(source)
        return [Document(page_content=page.extract_text() or "", metadata={"page": i})
                for i, page in enumerate(reader.pages)]


def page_chunks(documents, size, overlap=100):
    """Keep page provenance and prefer whitespace near each chunk boundary."""
    if size < 1 or not 0 <= overlap < size:
        raise ValueError("Chunk overlap must be nonnegative and smaller than the chunk size")
    for page in documents:
        text = page.page_content.strip()
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                midpoint = start + max(size // 2, overlap)
                boundary = text.rfind("\n", midpoint, end)
                if boundary < midpoint:
                    boundary = text.rfind(" ", midpoint, end)
                if boundary >= midpoint:
                    end = boundary + 1
            chunk = text[start:end].strip()
            if chunk:
                yield chunk, page.metadata.get("page", 0)
            if end == len(text):
                break
            start = end - overlap


def load_vectorstore(documents, filename: str, role: str, doc_id: str):
    from tqdm.auto import tqdm

    chunks = list(page_chunks(documents, 500))
    if not chunks:
        return

    texts = [text for text, _ in chunks]
    ids = [f"{doc_id}-{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "source": filename,
            "doc_id": doc_id,
            "role": role,
            "page": page
        }
        for _, page in chunks
    ]

    print(f"Processing {filename}")
    print(f"Total chunks: {len(texts)}")

    try:
        with tqdm(total=len(texts), desc="Uploading to ChromaDB") as progress:
            get_vectorstore().add_texts(
                texts=texts,
                metadatas=metadatas,
                ids=ids
            )
            progress.update(len(texts))

        print(f"Upload complete for {filename}")
    except Exception as exc:
        raise RuntimeError(f"Failed to save document vectors: {exc}") from exc


def summarize_section(text, generate):
    prompt = f"""
You are a medical report summarization assistant.
Read the following excerpt from a medical report and create a concise summary in plain language.
Do not hallucinate. Preserve the meaning exactly.

Excerpt:
{text}
"""
    return generate(prompt)


def summarize_pdf(documents, provider="ollama") -> str:
    generate = invoke_llm
    if provider == "groq":
        import asyncio
        from ..providers import groq_answer

        async def collect(prompt):
            return "".join([token async for token in groq_answer(prompt)])

        def generate(prompt):
            return asyncio.run(collect(prompt))
    elif provider != "ollama":
        raise ValueError("Unknown summary provider")

    from concurrent.futures import ThreadPoolExecutor

    text = "\n\n".join(page.page_content.strip() for page in documents if page.page_content.strip())
    if not text:
        return "Unable to extract text from the uploaded PDF."
    if len(text) <= 3600:
        return summarize_section(text, generate)

    sections = (section for section, _ in page_chunks(documents, 2400))
    with ThreadPoolExecutor(max_workers=2) as pool:
        summaries = list(pool.map(lambda section: summarize_section(section, generate), sections))
    if len(summaries) == 1:
        return summaries[0]

    joined = "\n\n".join(summaries)
    final_prompt = f"""
You have the following partial summaries of a medical report. Combine them into one clear and concise summary.

Partial summaries:
{joined}
"""

    return generate(final_prompt)
