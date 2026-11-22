"""Cloud provider routing for the built-in synthetic document demo."""
import asyncio
import json
import os
from time import perf_counter

import httpx

ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
DOCUMENT = "Page 2: Sample A volume was 4 mL. Page 7: Sample B volume was 6 mL."
CASES = {
    "sample_a": ("What volume was recorded for sample A?", "4", "mL", 2),
    "sample_b": ("What volume was recorded for sample B?", "6", "mL", 7),
    "missing": ("What temperature was recorded?", None, None, None),
}


def configured(provider):
    prefix = provider.upper()
    return all(os.getenv(f"{prefix}_{field}") for field in ("API_KEY", "MODEL"))


def require_allowed_provider(task, provider):
    from fastapi import HTTPException
    setting = os.getenv(task.upper() + "_PROVIDER", "choice")
    if setting not in ("choice", "ollama", "groq"):
        raise HTTPException(503, f"Invalid {task} provider configuration.")
    if setting != "choice" and setting != provider:
        raise HTTPException(422, f"This installation requires {setting} for {task}. Select it explicitly.")


async def measure(client, provider, case, budget=18):
    prefix = provider.upper()
    model = os.getenv(f"{prefix}_MODEL")
    result = {"provider": provider, "model": model, "status": "unconfigured", "first_text_s": None}
    if not configured(provider):
        return result
    question, value, unit, page = CASES[case]
    prompt = (f"Fictional document: {DOCUMENT}\nQuestion: {question}\n"
              'Return only a JSON object with value (string), unit (string), and page (integer). '
              'If absent, return null for all three fields.')
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "stream": True, "max_tokens": 160}
    if provider == "groq" and model.startswith("openai/gpt-oss-"):
        body.update(max_tokens=768, reasoning_effort="low", response_format={"type":"json_object"})
    if provider == "openrouter":
        body["provider"] = {"ignore": ["Groq"]}
        if upstream := os.getenv("OPENROUTER_UPSTREAM"):
            body["provider"]["only"] = [upstream]
    if provider == "gemini":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "extraction", "schema": {"type": "object", "properties": {
                "value": {"type": ["string", "null"]}, "unit": {"type": ["string", "null"]},
                "page": {"type": ["integer", "null"]}}, "required": ["value", "unit", "page"]}}}
    started = perf_counter()
    parts = []
    usage = {}
    complete = False
    try:
        async with asyncio.timeout(min(18, budget)):
            async with client.stream("POST", ENDPOINTS[provider], json=body, headers={
                "Authorization": "Bearer " + os.environ[f"{prefix}_API_KEY"]}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    event = json.loads(payload)
                    if event.get("error"):
                        raise ValueError("stream error")
                    usage = event.get("usage") or event.get("x_groq", {}).get("usage") or usage
                    result["model"] = event.get("model") or result["model"]
                    if event.get("provider"):
                        result["upstream"] = event["provider"]
                    for choice in event.get("choices", []):
                        token = choice.get("delta", {}).get("content")
                        if token:
                            if not parts:
                                result["first_text_s"] = round(perf_counter() - started, 4)
                            parts.append(token)
                        if choice.get("finish_reason") == "stop":
                            complete = True
        if not complete or not parts:
            raise ValueError("incomplete stream")
        answer = json.loads("".join(parts))
        if not isinstance(answer, dict) or not {"value", "unit", "page"} <= answer.keys():
            raise ValueError("invalid fields")
        if (any(answer[k] is not None and not isinstance(answer[k], str) for k in ("value", "unit"))
                or (answer["page"] is not None and type(answer["page"]) is not int)):
            raise ValueError("invalid field types")
        answer = {key: answer[key] for key in ("value", "unit", "page")}
        result.update(status="ok", answer=answer, value_correct=answer["value"] == value, unit_correct=answer["unit"] == unit, value_unit_correct=(answer["value"], answer["unit"]) == (value, unit),
                      citation_correct=answer["page"] == page)
    except httpx.HTTPStatusError as exc:
        result["status"] = f"http_{exc.response.status_code}"
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result["status"] = type(exc).__name__
    result["total_s"] = round(perf_counter() - started, 4)
    result["usage"] = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "cost") if k in usage}
    cost = usage.get("cost")
    if isinstance(cost, (int, float)):
        result["cost_usd"] = cost
    else:
        try:
            rates = [float(os.environ[f"{prefix}_{side}_USD_PER_MILLION"]) for side in ("INPUT", "OUTPUT")]
            result["estimated_cost_usd"] = (usage["prompt_tokens"] * rates[0] + usage["completion_tokens"] * rates[1]) / 1e6
        except (KeyError, TypeError, ValueError):
            pass
    return result


async def run_demo(client, route, case):
    providers = ("groq", "openrouter") if route == "auto" else (route,)
    attempts = []
    deadline = perf_counter() + 30
    for provider in providers:
        result = await measure(client, provider, case, deadline - perf_counter())
        attempts.append(result)
        if result["status"] == "ok" or result["first_text_s"] is not None:
            break
    return {"case": case, "document": DOCUMENT, "question": CASES[case][0], "attempts": attempts}

async def groq_answer(prompt):
    """Stream answer text from the explicitly selected Groq model."""
    if not configured("groq"):
        raise RuntimeError("Groq chat is unconfigured")
    model = os.environ["GROQ_MODEL"]
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "stream": True, "max_completion_tokens": 768}
    if model.startswith("openai/gpt-oss-"):
        body["reasoning_effort"] = "low"
    async with asyncio.timeout(35), httpx.AsyncClient(timeout=20) as client:
        async with client.stream("POST", ENDPOINTS["groq"], json=body, headers={
            "Authorization": "Bearer " + os.environ["GROQ_API_KEY"]}) as response:
            response.raise_for_status()
            received = False
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                item = json.loads(payload)
                if item.get("error"):
                    raise RuntimeError("Groq stream failed")
                for choice in item.get("choices", []):
                    token = choice.get("delta", {}).get("content")
                    if token:
                        received = True
                        yield token.translate({0x3010: "[", 0x3011: "]"})
                    if received and choice.get("finish_reason") == "stop":
                        return
    raise RuntimeError("Incomplete Groq answer")
