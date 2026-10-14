#!/usr/bin/env python3
"""Structura local server.

Serves index.html and forwards chat requests to an AI provider so the API key
stays on this computer and is never sent to the browser.

Providers (picked automatically from the key):  xAI Grok ("xai-..." keys) and Groq ("gsk_..." keys).
Usage:  python server.py         (Python 3.8+, no packages to install)
Config: put your key in a .env file next to this script (see .env.example).
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read_text(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16"):  # Notepad and PowerShell can save with a BOM or as UTF-16
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")  # .env wins over stale system variables


load_env()
PROVIDERS = {
    "xai": {"name": "xAI Grok", "base": "https://api.x.ai/v1", "model": "grok-4.7"},
    "groq": {"name": "Groq", "base": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
}
# Accept the key under any of these names; strip stray spaces or line breaks from pasting.
KEY = "".join((os.environ.get("AI_API_KEY") or os.environ.get("XAI_API_KEY") or os.environ.get("GROQ_API_KEY") or "").split())
_forced = os.environ.get("AI_PROVIDER", "").strip().lower()
PROVIDER = _forced if _forced in PROVIDERS else ("groq" if KEY.startswith("gsk_") else "xai")
P = PROVIDERS[PROVIDER]
MODEL = (os.environ.get("AI_MODEL") or os.environ.get(PROVIDER.upper() + "_MODEL") or P["model"]).strip()
BASE = (os.environ.get("AI_BASE_URL") or os.environ.get(PROVIDER.upper() + "_BASE_URL") or P["base"]).strip().rstrip("/")
MODEL_EXPLICIT = bool(os.environ.get("AI_MODEL") or os.environ.get(PROVIDER.upper() + "_MODEL"))
PREFER = {
    "groq": ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"],
    "xai": ["grok-4.7", "grok-4", "grok-3", "grok-3-mini"],
}
NOT_CHAT = ("whisper", "tts", "guard", "embed", "playai", "orpheus", "transcribe", "moderation", "image", "imagine", "safeguard")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

TOPIC_NAMES = {
    "array": "Array", "linked-list": "Linked List", "stack": "Stack", "queue": "Queue",
    "bst": "Binary Search Tree", "heap": "Heap", "hash": "Hash Table", "graph": "Graph",
}

SYSTEM = (
    "You are Structura, a friendly data structures tutor for an undergraduate computer engineering student. "
    "Teach with a small concrete example first, then the formal idea. State time and space complexity whenever you "
    "discuss operations. Use Python unless asked otherwise, keep code short, and walk through one example by hand. "
    "For practice problems, give a hint first and the full solution only if asked. If the student shares code, check "
    "correctness, state its time and space complexity, and suggest at most two improvements. Point out common mistakes. "
    "Be concise: short paragraphs, no filler. Use simple markdown only: **bold**, `inline code`, fenced code blocks "
    "with a language tag, and plain lists. Do not use tables or headings."
)


def has_key():
    return bool(KEY) and not KEY.lower().startswith(("your", "paste", "xai-xxxx"))


def system_prompt(topic):
    name = TOPIC_NAMES.get(topic)
    if name:
        return SYSTEM + " The student is currently viewing the " + name + " topic in the app."
    return SYSTEM


def clean_messages(raw):
    if not isinstance(raw, list):
        raise ValueError("messages must be a list")
    out = []
    for m in raw[-20:]:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content[:6000]})
    if not out or out[-1]["role"] != "user":
        raise ValueError("the last message must come from the user")
    return out


def _post(payload):
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + KEY,
            "User-Agent": "Structura/1.0",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=300)


def http_detail(e):
    if not hasattr(e, "_detail"):
        try:
            e._detail = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            e._detail = ""
    return e._detail


def err_text(detail):
    """Pull the human message out of a provider's JSON error body."""
    try:
        d = json.loads(detail)
        err = d.get("error", d) if isinstance(d, dict) else d
        if isinstance(err, dict):
            err = err.get("message") or err.get("error") or ""
        return str(err)[:300]
    except ValueError:
        return detail[:300]


def model_problem(e):
    low = http_detail(e).lower()
    words = ("not found", "does not exist", "decommission", "not supported", "no access", "terms", "deprecated", "restricted", "permission")
    return e.code == 404 or ("model" in low and any(w in low for w in words))


def list_models():
    req = urllib.request.Request(BASE + "/models", headers={"Authorization": "Bearer " + KEY, "User-Agent": "Structura/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and isinstance(m.get("id"), str)]
    return [i for i in ids if not any(w in i.lower() for w in NOT_CHAT)]


def choose_model(avail, exclude=()):
    for m in PREFER.get(PROVIDER, []):
        if m in avail and m not in exclude:
            return m
    for m in sorted(avail):
        if m not in exclude:
            return m
    return None


def upstream(payload):
    """Call the provider. If the default model is refused, switch to one the key can use and retry once."""
    global MODEL
    try:
        return _post(payload)
    except urllib.error.HTTPError as e:
        if MODEL_EXPLICIT or not model_problem(e):
            raise
        try:
            alt = choose_model(list_models(), exclude=(payload["model"],))
        except Exception:
            alt = None
        if not alt:
            raise
        sys.stderr.write("Model '%s' was refused; switching to '%s'.\n" % (payload["model"], alt))
        MODEL = alt
        payload["model"] = alt
        return _post(payload)


def key_report():
    """Safe description of the key: never the key itself."""
    if not has_key():
        return "MISSING (add your key to the .env file)", []
    notes = []
    if not KEY.startswith(("xai-", "gsk_")) and not os.environ.get("AI_BASE_URL"):
        notes.append("Unrecognised key format. xAI Grok keys start with 'xai-' (console.x.ai) and Groq keys with 'gsk_' "
                     "(console.groq.com). Keys from other companies will be rejected.")
    if len(KEY) < 40:
        notes.append("The key looks too short. Copy the whole key; many sites show it only once when created.")
    return "found (%s, starts with '%s', %d characters)" % (P["name"], KEY[:4], len(KEY)), notes


def explain_http(e):
    code, detail = e.code, http_detail(e)
    why = err_text(detail).strip().rstrip(".")
    low = detail.lower()
    if code in (401, 403) and not model_problem(e) or "api key" in low:
        return "%s rejected the API key. Check the key in .env: it must be created for this provider, copied whole, and still active." % P["name"]
    if model_problem(e):
        return "%s refused the model '%s'. Reason: %s. Set AI_MODEL in .env to a model from the list printed when the server starts." % (P["name"], MODEL, why or "no reason given")
    if code == 429:
        return "Rate limit or usage limit reached on your %s account. Wait a moment and try again. (%s)" % (P["name"], why)
    return "%s returned an error (%d). %s" % (P["name"], code, why)


def parse_quiz(text, count):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        arr = json.loads(text[start:end + 1])
    except ValueError:
        return []
    good = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        q, o, a, w = it.get("q"), it.get("o"), it.get("a"), it.get("w")
        if (isinstance(q, str) and isinstance(o, list) and len(o) == 4
                and all(isinstance(x, str) and x.strip() for x in o)
                and isinstance(a, int) and not isinstance(a, bool) and 0 <= a <= 3 and isinstance(w, str)):
            good.append({"q": q.strip(), "o": [x.strip() for x in o], "a": a, "w": w.strip()})
    return good[:count]


class Handler(BaseHTTPRequestHandler):
    server_version = "Structura/1.0"

    def log_message(self, fmt, *args):  # request line only, never headers or bodies
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            page = ROOT / "index.html"
            if not page.exists():
                return self._json(404, {"error": "index.html not found next to server.py"})
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            self._json(200, {"ok": True, "hasKey": has_key(), "model": MODEL, "provider": P["name"]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/chat", "/api/quiz"):
            return self._json(404, {"error": "not found"})
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + (self.headers.get("Host") or ""):
            return self._json(403, {"error": "Cross-origin requests are not allowed."})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": "Send JSON."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return self._json(413, {"error": "Request body is empty or too large."})
        try:
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("body must be an object")
        except ValueError:
            return self._json(400, {"error": "Invalid JSON."})
        if not has_key():
            return self._json(503, {"error": "No API key found. Add your key to the .env file and restart the server."})
        if path == "/api/chat":
            self._chat(data)
        else:
            self._quiz(data)

    def _chat(self, data):
        try:
            msgs = clean_messages(data.get("messages"))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        payload = {
            "model": MODEL,
            "messages": [{"role": "system", "content": system_prompt(data.get("topic"))}] + msgs,
            "stream": True,
        }
        try:
            resp = upstream(payload)
        except urllib.error.HTTPError as e:
            return self._json(502, {"error": explain_http(e)})
        except Exception:
            return self._json(502, {"error": "Could not reach %s. Check your internet connection." % P["name"]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for line in resp:
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()

    def _quiz(self, data):
        name = TOPIC_NAMES.get(data.get("topic"))
        if not name:
            return self._json(400, {"error": "Unknown topic."})
        try:
            count = max(1, min(5, int(data.get("count") or 3)))
        except (TypeError, ValueError):
            count = 3
        avoid = [s[:200] for s in (data.get("avoid") or []) if isinstance(s, str)][:10]
        prompt = (
            "Write %d new multiple-choice questions about %s for an undergraduate student. Each has exactly 4 options "
            "and exactly one correct answer. Mix concept and small-trace questions. Vary which position is correct. "
            "Return ONLY a JSON array with no markdown fences. Each item must be "
            '{"q": string, "o": [4 strings], "a": index 0-3 of the correct option, "w": one-sentence explanation}.'
            % (count, name)
        )
        if avoid:
            prompt += " Do not repeat these questions: " + " | ".join(avoid)
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You write accurate, unambiguous quiz questions and reply with JSON only."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        try:
            body = json.loads(upstream(payload).read())
            text = body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            return self._json(502, {"error": explain_http(e)})
        except Exception:
            return self._json(502, {"error": "Could not get questions from %s. Try again." % P["name"]})
        qs = parse_quiz(text, count)
        if not qs:
            return self._json(502, {"error": "The AI's reply could not be turned into questions. Try again."})
        self._json(200, {"questions": qs})


def main():
    global MODEL
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        sys.exit("Could not start on %s:%d (%s). Set PORT in .env to use another port." % (HOST, PORT, e))
    srv.daemon_threads = True
    url = "http://%s:%d" % (HOST, PORT)
    print("Structura running at " + url)
    status, notes = key_report()
    print("Model: %s  |  API key: %s" % (MODEL, status))
    for n in notes:
        print("WARNING: " + n)
    if has_key():
        try:
            avail = list_models()
            print("Models your key can use: " + ", ".join(avail[:12]) + (" ..." if len(avail) > 12 else ""))
            if MODEL not in avail:
                if MODEL_EXPLICIT:
                    print("WARNING: AI_MODEL '%s' is not in that list." % MODEL)
                else:
                    pick = choose_model(avail)
                    if pick:
                        MODEL = pick
                        print("The default model is not available to this key. Using '%s' instead." % pick)
            print("Using model: " + MODEL)
        except Exception as ex:
            print("Could not fetch the model list (%s). Continuing with '%s'." % (getattr(ex, "code", None) or ex.__class__.__name__, MODEL))
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print("Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
