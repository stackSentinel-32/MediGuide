"""Version 1 report-isolation fixtures; run python -m demo.check_reports."""
import json
from io import BytesIO
from time import monotonic, sleep

from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas
from .__main__ import QUESTION, local_app


def pdf(value, unit, page):
    output = BytesIO()
    canvas = Canvas(output, invariant=True)
    for n in range(1, page + 1):
        text = f"Sample B volume was {value} {unit}." if n == page else "Sample A volume was 1 mL."
        canvas.drawString(42, 740, text)
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def check():
    import re
    with local_app() as app, TestClient(app) as api:
        owner, other = ("owner", "fixture"), ("other", "fixture")
        for auth in (owner, other):
            api.post("/signup", json={"username":auth[0], "password":auth[1]}).raise_for_status()
        cases = [("6", "mL", 2), ("3", "L", 4)]
        records = []
        for value, unit, page in cases:
            reply = api.post("/docs/report", files={"file": (f"report-{page}.pdf", pdf(value, unit, page))}, auth=owner)
            reply.raise_for_status()
            records.append(reply.json()["doc_id"])
        for doc_id, (value, unit, page) in zip(records, cases):
            deadline = monotonic() + 15
            while not api.get(f"/docs/reports/{doc_id}", auth=owner).json()["searchable"]:
                assert monotonic() < deadline
                sleep(.03)
            payload = {"query": QUESTION, "doc_id": doc_id}
            reply = api.post("/chat/", json=payload, auth=owner)
            reply.raise_for_status()
            result = reply.json()
            match = re.fullmatch(r"Sample B volume was ([0-9]+) (\w+)\. \[(\d+)\]", result["answer"])
            assert match, result
            source = result["sources"][int(match[3])-1]
            scores = dict(value=match[1]==value, unit=match[2]==unit, page=source["page"]+1==page)
            assert all(scores.values()) and all(s["doc_id"]==doc_id for s in result["sources"])
            print("Report fixture:", json.dumps(scores))
            for route in ("/chat/", "/chat/stream"):
                assert api.post(route, json=payload, auth=other).status_code == 404
            payload["query"] = "What temperature was recorded for sample B?"
            assert "do not contain" in api.post("/chat/",json=payload,auth=owner).json()["answer"]


if __name__ == "__main__":
    check()
