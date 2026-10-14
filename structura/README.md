# Structura

A data structures tutor: explanations, step-by-step visualizers, Python code, quizzes, and an AI chat.

## Start it

- **Windows:** double-click `Start Structura.bat`
- **Mac / Linux:** run `./start.sh` (or `python3 server.py`)

Your browser opens at http://127.0.0.1:8000. Needs Python 3.8+ and nothing else.

## Your key

Put your key in the `.env` file as `AI_API_KEY=...`. The provider is detected from the key:
`xai-...` uses xAI Grok, `gsk_...` uses Groq. Without a key, everything except the AI features still works.

## What uses the AI

- **Tutor chat** with streaming replies. It knows which topic you are on.
- **Ask AI about this** under every visualizer, on answer cards and on the Learn page.
- **Review with AI** on the Code tab.
- **New questions from AI** at the end of every quiz.

## Keep the key safe

The key lives only in `.env` and is used by `server.py`. It is never sent to the page, and the server listens on
127.0.0.1 only. Do not share this folder with `.env` inside it, commit it, or paste the key into a chat.
If a key is ever exposed, revoke it in the provider's console and create a new one.

## Settings (.env)

`AI_MODEL` overrides the model. `PORT` changes the port. `AI_PROVIDER=xai` or `groq` forces a provider.
