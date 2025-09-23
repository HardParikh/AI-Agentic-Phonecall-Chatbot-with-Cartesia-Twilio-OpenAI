# app/main.py
from dotenv import load_dotenv
load_dotenv()  # must be first so settings/env are ready for imports

from typing import Dict, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
import os
import json
import threading
from datetime import datetime

from app.config import settings
from app.seeds import seed_db
from app.tts import save_tts_file
from app.nlp import normalize_service
from app.agent import confirm_booking  # agent unused now; tools called directly
from app.rag import RAG

# ──────────────────────────────
# Results logger (JSONL)
# ──────────────────────────────
RESULTS_FILE = "results.json"
_results_lock = threading.Lock()

def log_event(call_sid: str, stage: str, state: dict | None = None, extra: dict | None = None) -> None:
    """
    Append a single JSON record to results.json (JSONL format).
    stage: "start" | "service_captured" | "name_captured" | "phone_captured"
           | "proposed" | "no_slot" | "confirmed" | "declined" | "error"
    """
    rec = {
        "call_sid": call_sid or "unknown",
        "stage": stage,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "state": state or {},
        "extra": extra or {},
    }
    try:
        with _results_lock:
            with open(RESULTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        # Never let logging crash the flow
        print("LOG ERROR:", e)

# ──────────────────────────────
# FastAPI app
# ──────────────────────────────
app = FastAPI(title="AI Barber Agent")

# Serve audio files to Twilio
os.makedirs("static/audio", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Build/load local RAG index at startup
_rag = RAG(settings.rag_index_path)

@app.on_event("startup")
async def startup() -> None:
    seed_db()
    _rag.load()

WELCOME = (
    "Welcome to Riverside Cuts. I can book you with Alex, Brook, or Casey. "
    "We offer Haircut, Beard Trim, Hot Towel Shave, Kids Haircut, Wash and Style, and Color Touch-up. "
    "What can I help you with today?"
)

def _tts_or_say(text: str) -> str:
    """
    Return a TwiML fragment: <Play>...wav</Play> when TTS succeeds,
    else <Say>text</Say> as a safe fallback (prevents 500s / Application Error).
    """
    try:
        rel = save_tts_file(text, settings.voice_id, settings.cartesia_api_key)  # e.g. /static/audio/xxx.wav
        return f"<Play>{settings.base_url}{rel}</Play>"
    except Exception as e:
        # Log to server console so you can see exact TTS failure reason
        print("TTS ERROR:", e)
        return f"<Say>{text}</Say>"

# Friendly GET for browser sanity-checks
@app.get("/voice")
async def voice_debug() -> PlainTextResponse:
    return PlainTextResponse("Voice endpoint is alive. Twilio will POST here during a call.")

@app.post("/voice")
async def voice_root(request: Request) -> Response:
    # Try to capture CallSid if present
    try:
        form = await request.form()
        call_sid = form.get("CallSid") or "unknown"
    except Exception:
        call_sid = "unknown"

    # Start event
    st = STATE.setdefault(call_sid, {"customer_name": None, "phone": None, "service_code": None, "preferred_time": None})
    log_event(call_sid, "start", state=st)

    play_or_say = _tts_or_say(WELCOME)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {play_or_say}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
    return Response(content=xml, media_type="text/xml")

# Conversation state keyed by CallSid (simple in-memory store)
STATE: Dict[str, Dict[str, Optional[str]]] = {}

@app.post("/gather")
async def handle_gather(request: Request) -> Response:
    form = await request.form()
    call_sid = form.get("CallSid")
    speech = form.get("SpeechResult") or ""

    st = STATE.setdefault(
        call_sid,
        {"customer_name": None, "phone": None, "service_code": None, "preferred_time": None},
    )

    # Step 1: determine service
    if st["service_code"] is None:
        code = normalize_service(speech)
        if code:
            st["service_code"] = code
            log_event(call_sid, "service_captured", state=st, extra={"heard": speech})
            reply = f"Got it. You want {code}. What's your name?"
        else:
            reply = (
                "I can book Haircut, Beard Trim, Hot Towel Shave, Kids Haircut, "
                "Wash and Style, or Color Touch-up. Which one would you like?"
            )
        fragment = _tts_or_say(reply)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")

    # Step 2: ask name
    if st["customer_name"] is None:
        st["customer_name"] = speech.strip()
        log_event(call_sid, "name_captured", state=st, extra={"heard": speech})
        fragment = _tts_or_say("Thanks! What phone number should I use for your appointment?")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")

    # Step 3: ask phone
    if st["phone"] is None:
        st["phone"] = "".join(c for c in speech if c.isdigit())
        log_event(call_sid, "phone_captured", state=st, extra={"heard": speech})
        fragment = _tts_or_say("Do you have a preferred time, or should I find the next available slot?")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")

    # Step 4: propose a booking deterministically (NO agent in the loop)
    if st["preferred_time"] is None:
        utter = speech.lower().strip()
        next_synonyms = ("next", "soon", "asap", "earliest", "any", "first available", "next available")
        user_wants_next = any(k in utter for k in next_synonyms)

        if user_wants_next:
            st["preferred_time"] = "__NEXT__"  # sentinel (not a real time)
            # keep track of how many times we've tried "next available"
            st["next_attempts"] = str(int(st.get("next_attempts") or "0") + 1)
        else:
            st["preferred_time"] = speech

        # Decide days_ahead based on attempts (widen search)
        attempts = int(st.get("next_attempts") or "0")
        days_ahead = 3 if attempts <= 1 else 7 if attempts == 2 else 14

        # call the proposal tool directly
        from app.agent import propose_booking
        try:
            raw = propose_booking.run({
                "service_code": st["service_code"],
                "preferred_time": None if user_wants_next else st["preferred_time"],
                "days_ahead": days_ahead
            })
        except Exception as e:
            print("PROPOSE ERROR:", e)
            log_event(call_sid, "error", state=st, extra={"where": "propose_booking.run", "message": str(e)})
            raw = "ERROR"

        info = None
        if raw and isinstance(raw, str) and raw.startswith("{"):
            try:
                info = json.loads(raw)
            except Exception:
                info = None

        if info and all(k in info for k in ("barber", "barber_id", "service_id", "start")):
            # ✅ Real offer → ask for confirmation
            st["barber_id"] = str(info["barber_id"])
            st["service_id"] = str(info["service_id"])
            st["start"] = info["start"]
            st["awaiting_confirm"] = True
            st["next_attempts"] = "0"  # reset
            log_event(call_sid, "proposed", state=st, extra={"proposal": info})
            speak = f"I can book you with {info['barber']} at {info['start']}. Shall I confirm?"
            fragment = _tts_or_say(speak)
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/confirm" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

        # ❌ Still no slot — avoid loop by asking for a concrete time after 2 tries
        if user_wants_next and attempts >= 2:
            msg = ("I couldn't find any openings soon. "
                   "Please say a day and time, for example: "
                   "‘Friday at 3 PM’, ‘tomorrow afternoon’, or ‘October 2nd at 10 AM’.")
        else:
            msg = ("I couldn't find a match yet. "
                   "Say ‘next available’ to widen the search, or say a specific day and time.")
        log_event(call_sid, "no_slot", state=st, extra={"reason": raw, "days_ahead": days_ahead})
        fragment = _tts_or_say(msg)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")

    # Shouldn't reach here, but keep collecting
    fragment = _tts_or_say("What time works for you?")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
    return Response(content=xml, media_type="text/xml")

@app.post("/confirm")
async def handle_confirm(request: Request) -> Response:
    form = await request.form()
    call_sid = form.get("CallSid")
    speech = (form.get("SpeechResult") or "").lower()

    st = STATE.get(call_sid, {}) or {}

    # If user says "no", loop back to gather
    if any(w in speech for w in ["no", "later", "change"]):
        log_event(call_sid, "declined", state=st, extra={"heard": speech})
        fragment = _tts_or_say("No problem. What time works for you?")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")

    # Only attempt to book on an affirmative AND complete state
    if any(w in speech for w in ["yes", "book", "confirm", "sure"]):
        required = ["customer_name", "phone", "barber_id", "service_id", "start"]
        missing = [k for k in required if not st.get(k)]
        if missing:
            log_event(call_sid, "error", state=st, extra={"reason": "missing_fields_before_confirm", "missing": missing})
            fragment = _tts_or_say(
                "I don't have a time locked in yet. Tell me your preferred time, or say next available."
            )
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

        try:
            barber_id = int(st["barber_id"])
            service_id = int(st["service_id"])
            _ = confirm_booking.run({
                "customer_name": st.get("customer_name"),
                "phone": st.get("phone"),
                "barber_id": barber_id,
                "service_id": service_id,
                "start_iso": st.get("start"),
            })
            log_event(call_sid, "confirmed", state=st)
            fragment = _tts_or_say("Your appointment is confirmed. See you soon!")
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
</Response>"""
            STATE.pop(call_sid, None)
            return Response(content=xml, media_type="text/xml")
        except Exception as e:
            print("BOOKING ERROR:", e)
            log_event(call_sid, "error", state=st, extra={"where": "confirm_booking.run", "message": str(e)})
            fragment = _tts_or_say(
                "I hit a snag confirming that. Should I try the next available time?"
            )
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

    # If not an affirmative, keep collecting or clarify
    fragment = _tts_or_say("Would you like me to confirm this appointment?")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/confirm" method="POST" speechTimeout="auto"/>
</Response>"""
    return Response(content=xml, media_type="text/xml")

@app.get("/")
async def root():
    return {"ok": True, "msg": "AI Barber Agent is running. Use /voice for Twilio webhook."}

@app.get("/health")
async def health():
    return {"ok": True}

# Optional: quick tail endpoint to view recent results without opening the file
@app.get("/results")
def results_tail(limit: int = 50):
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        tail = [json.loads(x) for x in lines[-limit:]]
        return {"count": len(tail), "results": tail}
    except FileNotFoundError:
        return {"count": 0, "results": []}
