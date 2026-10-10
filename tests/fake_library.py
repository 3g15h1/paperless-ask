"""A made-up Paperless library and Ollama, served through httpx.MockTransport."""
import json
from datetime import date, timedelta

import httpx

SOON = date.today() + timedelta(days=10)
SOON_US = SOON.strftime("%m/%d/%Y")

DOCS = [
    {"id": 1, "title": "Electronics receipt", "created": "2024-10-03T00:00:00Z", "correspondent": 1,
     "document_type": 1, "tags": [1], "modified": "2024-10-04T00:00:00Z",
     "content": "Gadget Store Main Street\nOctober 03, 2024 07:45 PM\nPhone X Pro 256GB\nTotal $ 1,099.00\nVisa ending 0000"},
    {"id": 2, "title": "Auto insurance ID card", "created": "2026-04-22T00:00:00Z", "correspondent": 2,
     "document_type": 2, "tags": [2], "modified": "2026-04-23T00:00:00Z",
     "content": "Example Mutual Insurance\nAutomobile Insurance Identification Card\nPolicy Number: 123456789\n"
                "Effective Date: 04/22/2026 Expiration Date: 10/22/2026\nVehicle: 2020 Sedan"},
    {"id": 3, "title": "Auto insurance ID card", "created": "2025-04-22T00:00:00Z", "correspondent": 2,
     "document_type": 2, "tags": [2], "modified": "2025-04-23T00:00:00Z",
     "content": "Example Mutual Insurance\nAutomobile Insurance Identification Card\nPolicy Number: 123456789\n"
                "Effective Date: 04/22/2025 Expiration Date: 10/22/2025\nVehicle: 2020 Sedan"},
    {"id": 4, "title": "Utility statement August", "created": "2025-08-15T00:00:00Z", "correspondent": 3,
     "document_type": 3, "tags": [], "modified": "2025-08-16T00:00:00Z",
     "content": "AcmePower Electric Company\nStatement date August 15, 2025\nAmount due $ 142.37\nDue by 09/05/2025"},
    {"id": 5, "title": "Patient receipt", "created": "2025-03-10T00:00:00Z", "correspondent": 4,
     "document_type": None, "tags": [], "modified": "2025-03-11T00:00:00Z",
     "content": "Riverside Family Clinic\nPatient receipt\nDate of service 03/10/2025\nAmount paid $100.65"},
    {"id": 6, "title": "Clinic statement", "created": "2025-03-20T00:00:00Z", "correspondent": 4,
     "document_type": None, "tags": [], "modified": "2025-03-21T00:00:00Z",
     "content": "Riverside Family Clinic\nStatement\nPayment received 03/12/2025 $100.65 thank you"},
    {"id": 7, "title": "Pharmacy receipt", "created": "2024-06-01T00:00:00Z", "correspondent": None,
     "document_type": None, "tags": [], "modified": "2024-06-02T00:00:00Z",
     "content": "Corner Pharmacy\nPrescription\n06/01/2024\nTotal $ 50.00"},
    {"id": 8, "title": "Payment due notice", "created": "2026-01-01T00:00:00Z", "correspondent": None,
     "document_type": None, "tags": [], "modified": "2026-01-02T00:00:00Z",
     "content": f"Northwind Card Services\nMinimum payment $ 250.00\nPayment due date: {SOON_US}\nAccount 4000123412341234"},
    {"id": 9, "title": "Water bill", "created": "2026-01-01T00:00:00Z", "correspondent": None,
     "document_type": None, "tags": [3], "modified": "2026-01-02T00:00:00Z",
     "content": f"City Water Department\nAmount due $ 61.20\nDue by {SOON_US}"},
    {"id": 10, "title": "Photo of a medical form", "created": "2025-04-01T00:00:00Z", "correspondent": None,
     "document_type": None, "tags": [], "modified": "2025-04-02T00:00:00Z", "content": ""},  # OCR found no text
]
NAMES = {
    "correspondents": [{"id": 1, "name": "Gadget Store"}, {"id": 2, "name": "Example Mutual"},
                       {"id": 3, "name": "AcmePower"}],
    "document_types": [{"id": 1, "name": "Receipt"}, {"id": 2, "name": "Insurance"}, {"id": 3, "name": "Bill"}],
    "tags": [{"id": 1, "name": "electronics"}, {"id": 2, "name": "car"}, {"id": 3, "name": "Paid"}],
}
NAMES["correspondents"].append({"id": 4, "name": "Riverside Family Clinic"})


def card(kind, category, vendor, total=None, dates=(), ids=(), summary="A document."):
    return {"kind": kind, "category": category, "vendor": vendor, "summary": summary, "total": total,
            "dates": [{"label": label, "date": d} for label, d in dates], "ids": [{"label": label, "value": v} for label, v in ids]}


# What the fake model "extracts" for each document, including mistakes the grounding must catch
CARDS = {
    1: card("receipt", "shopping", "Gadget Store", "$1,099.00", [("issued", "2024-10-03")]),
    2: card("insurance", "insurance", "Example Mutual", "null", [("effective", "2026-04-22"), ("expires", "2026-10-22")],
            [("Policy Number", "123456789"), ("member", "JOHN DOE")]),
    3: card("insurance", "insurance", "Example Mutual", None, [("expires", "2025-10-22")], [("Policy Number", "123456789")]),
    4: card("bill", "utilities", "AcmePower", "142.37", [("issued", "2025-08-15"), ("due", "2025-09-05"), ("due", "2025-09-30")]),
    5: card("receipt", "medical", "Riverside Family Clinic", "$100.65", [("service", "2025-03-10")]),
    6: card("statement", "medical", "Riverside Family Clinic", "$100.65", [("paid", "2025-03-12")]),
    7: card("receipt", "medical", "Corner Pharmacy", "$ 50.00", [("paid", "2024-06-01")]),
    8: card("bill", "finance", "Northwind Card Services", "250.00", [("due", SOON.isoformat())],
            [("account", "4000123412341234")]),
    9: card("bill", "utilities", "Imaginary Vendor", "61.20", [("due", SOON.isoformat())]),
}
TOKEN = "0123456789abcdef0123456789abcdef01234567"


def page(results):
    return {"count": len(results), "next": None, "results": results}


class Fake:
    def __init__(self, answer="Your car insurance expires on October 22, 2026 [1].", ollama_up=True):
        self.answer, self.ollama_up, self.chats, self.extractions = answer, ollama_up, [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "paperless.test":
            if request.headers.get("authorization") != f"Token {TOKEN}":
                return httpx.Response(401)
            if path == "/api/documents/":
                q = request.url.params
                if q.get("query"):
                    return httpx.Response(200, json=page([]))
                if q.get("page_size") == "1":
                    top = max(DOCS, key=lambda d: d["modified"])
                    return httpx.Response(200, json={"count": len(DOCS), "next": None, "results": [top]})
                return httpx.Response(200, json=page(DOCS))
            for key, rows in NAMES.items():
                if path == f"/api/{key}/":
                    return httpx.Response(200, json=page(rows))
            if path.endswith("/thumb/"):
                return httpx.Response(200, content=b"RIFFthumb", headers={"content-type": "image/webp"})
            return httpx.Response(404)
        if host == "ollama.test":
            if not self.ollama_up:
                raise httpx.ConnectError("ollama down", request=request)
            if path == "/api/ps":
                return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b-instruct"}]})
            if path == "/api/chat" and json.loads(request.content).get("format"):
                body = json.loads(request.content)
                self.extractions.append(body)
                text = body["messages"][1]["content"]
                doc = next(d for d in DOCS if " ".join(d["content"].split())[:60] in text)
                return httpx.Response(200, json={"message": {"content": json.dumps(CARDS[doc["id"]])}, "done": True})
            if path == "/api/chat":
                self.chats.append(json.loads(request.content))
                words = self.answer.split(" ")
                lines = [json.dumps({"message": {"content": w + ("" if i == len(words) - 1 else " ")}})
                         for i, w in enumerate(words)]
                lines.append(json.dumps({"done": True, "eval_count": len(words)}))
                return httpx.Response(200, content="\n".join(lines).encode())
            return httpx.Response(404)
        return httpx.Response(404)
