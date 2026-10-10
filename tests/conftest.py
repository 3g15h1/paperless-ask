import os

# The app reads its configuration at import time.
os.environ.setdefault("PAPERLESS_URL", "http://paperless.test")
os.environ.setdefault("PAPERLESS_TOKEN", "0123456789abcdef0123456789abcdef01234567")
os.environ.setdefault("OLLAMA_URL", "http://ollama.test")
os.environ.setdefault("OLLAMA_WARM", "false")
os.environ.setdefault("ASK_SYNONYMS", "hydro=acmepower")
