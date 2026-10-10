"""A made-up Paperless library and Ollama, served through httpx.MockTransport."""
import json

import httpx

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
]
NAMES = {
    "correspondents": [{"id": 1, "name": "Gadget Store"}, {"id": 2, "name": "Example Mutual"},
                       {"id": 3, "name": "AcmePower"}],
    "document_types": [{"id": 1, "name": "Receipt"}, {"id": 2, "name": "Insurance"}, {"id": 3, "name": "Bill"}],
    "tags": [{"id": 1, "name": "electronics"}, {"id": 2, "name": "car"}],
}
TOKEN = "0123456789abcdef0123456789abcdef01234567"


def page(results):
    return {"count": len(results), "next": None, "results": results}


class Fake:
    def __init__(self, answer="Your car insurance expires on October 22, 2026 [1].", ollama_up=True):
        self.answer, self.ollama_up, self.chats = answer, ollama_up, []

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
            if path == "/api/chat":
                self.chats.append(json.loads(request.content))
                words = self.answer.split(" ")
                lines = [json.dumps({"message": {"content": w + ("" if i == len(words) - 1 else " ")}})
                         for i, w in enumerate(words)]
                lines.append(json.dumps({"done": True, "eval_count": len(words)}))
                return httpx.Response(200, content="\n".join(lines).encode())
            return httpx.Response(404)
        return httpx.Response(404)
