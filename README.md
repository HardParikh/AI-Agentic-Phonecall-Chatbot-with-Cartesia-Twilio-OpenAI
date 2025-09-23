# AI Barber Agent (Twilio + LangChain + RAG + Cartesia Sonic‑2)

## 1) Prereqs
- Python 3.11+
- Twilio account with a Programmable Voice phone number
- OpenAI API key (for embeddings + LLM)
- Cartesia API key (for Sonic‑2 TTS)
- (Optional) ngrok for public webhook URL

## 2) Local setup
```bash
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r app/requirements.txt
cp .env.example .env
# fill your keys in .env
```

## 3) Initialize DB and KB
The app auto‑seeds barbers, services, availability, and builds a small RAG index on startup.

## 4) Run server
```bash
uvicorn app.main:app --reload --port 8000
```

If using ngrok:
```bash
ngrok http 8000
# copy the https URL to PUBLIC_BASE_URL in .env and restart uvicorn
```

## 5) Twilio configuration
- Buy a phone number in Twilio Console.
- Set Voice webhook (A CALL COMES IN) to `POST` → `https://<PUBLIC_BASE_URL>/voice`.
- This app uses TwiML `<Gather input="speech">` to capture caller speech and Sonic‑2 TTS audio via `<Play>` URLs hosted by the app.

## 6) Test the flow
- Call your Twilio number. The agent greets the caller, collects service + name + phone, proposes a slot, and books on confirmation.

## 7) Data
- SQLite file at `barber.db` with `barbers`, `services`, `availability`, and `appointments`.
- Audio files saved under `static/audio/` per call turn; you may periodically clean them via a cron.

## 8) Extending to real‑time streaming (optional)
- Switch from `<Gather>` to Twilio Media Streams for bidirectional audio. Create a WebSocket endpoint and stream audio into Sonic‑2 streaming and Whisper/ASR if desired. See Twilio Docs.

## 9) Security & Ops
- Validate Twilio signatures on webhooks if exposing publicly.
- Rotate API keys; never hardcode in code.
- Add rate limits and logging (exclude PII).