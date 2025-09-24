# app/main.py
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()  # Ensure .env is loaded before imports that read env

from typing import Dict, Optional
from datetime import datetime
import json
import os
import threading

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.seeds import seed_db
from app.twilio_xml import GATHER_TMPL, PLAY_TMPL  # Keep if you still use templates elsewhere
from app.tts import save_tts_file
from app.nlp import normalize_service
from app.rag import RAG

# Agent bits
from app.agent import agent_decide_and_answer
from app.agent import propose_booking, confirm_booking  # tools (we'll call .invoke with dict input)


app = FastAPI(title="AI Barber Agent")

# Serve audio for Twilio
os.makedirs("static/audio", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Build/load local RAG index at startup
_rag = RAG(settings.rag_index_path)

@app.on_event("startup")
async def startup() -> None:
    seed_db()
    _rag.load()


# ----------------- results.json logging -----------------
RESULTS_FILE = "results.json"
_results_lock = threading.Lock()

def log_event(call_sid: str, stage: str, state: dict | None = None, extra: dict | None = None) -> None:
    rec = {
        "call_sid": call_sid or "unknown",
        "stage": stage,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "state": state or {},
        "extra": extra or {},
    }
    with _results_lock:
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ----------------- TTS helper with fallback -----------------
def _tts_or_say(text: str) -> str:
    """
    Prefer Cartesia TTS (served via /static/audio/*.mp3) and fallback to Twilio <Say> on any error,
    so calls never fail with 500/Application Error.
    """
    try:
        rel = save_tts_file(text, settings.voice_id, settings.cartesia_api_key)  # returns /static/audio/xxx.mp3
        return f"<Play>{settings.base_url}{rel}</Play>"
    except Exception as e:
        print("TTS ERROR:", e)
        return f"<Say>{text}</Say>"


# ----------------- Conversation state -----------------
STATE: Dict[str, Dict[str, Optional[str]]] = {}

INTRO = (
    "Welcome to Riverside Cuts. Are you calling to book an appointment, "
    "or would you like information like hours, pricing, or our cancellation policy?"
)


# ----------------- Routes -----------------
@app.get("/voice")
async def voice_debug() -> PlainTextResponse:
    return PlainTextResponse("Voice endpoint is alive. Twilio will POST here during a call.")


@app.post("/voice")
async def voice_root(request: Request) -> Response:
    # Grab CallSid if present and init state
    try:
        form = await request.form()
        call_sid = form.get("CallSid") or "unknown"
    except Exception:
        call_sid = "unknown"

    st = STATE.setdefault(call_sid, {
        "mode": None,  # None|FAQ|BOOK
        "customer_name": None,
        "phone": None,
        "service_code": None,
        "preferred_time": None,
        "next_attempts": "0",
    })
    log_event(call_sid, "start", state=st)

    # Agent-first prompt
    fragment = _tts_or_say(INTRO)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
    return Response(content=xml, media_type="text/xml")


@app.post("/gather")
async def handle_gather(request: Request) -> Response:
    form = await request.form()
    call_sid = form.get("CallSid")
    speech = (form.get("SpeechResult") or "").strip()

    st = STATE.setdefault(call_sid, {
        "mode": None,
        "customer_name": None,
        "phone": None,
        "service_code": None,
        "preferred_time": None,
        "next_attempts": "0",
    })

# app/main.py (inside handle_gather, replace the whole "if st['mode'] is None" block)

    # 1) If mode not chosen, ask the agent to decide (FAQ vs BOOK) or answer FAQ
    if st["mode"] is None:
        try:
            reply = agent_decide_and_answer(speech, chat_history=[])
        except Exception as e:
            # Extreme fallback (should not trigger because agent_decide_and_answer already guards)
            reply = ("I’m having trouble with that right now. "
                     "Would you like me to book an appointment?")

        # Route to booking?
        if isinstance(reply, str) and reply.startswith("ROUTE:BOOK"):
            st["mode"] = "BOOK"
            log_event(call_sid, "route_to_booking", state=st, extra={"utterance": speech})
            fragment = _tts_or_say(
                "Great. What service would you like? Haircut, Beard Trim, Hot Towel Shave, "
                "Kids Haircut, Wash and Style, or Color Touch-up?"
            )
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

        # FAQ answer path (safe TwiML no matter what)
        log_event(call_sid, "faq_answered", state=st, extra={"answer": reply, "utterance": speech})
        fragment = _tts_or_say(reply or "Here’s our information. Would you like me to make an appointment?")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/faq_followup" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")


    # 2) BOOK mode → deterministic flow
    if st["mode"] == "BOOK":
        # (a) service
        if st["service_code"] is None:
            code = normalize_service(speech)
            if code:
                st["service_code"] = code
                log_event(call_sid, "service_captured", state=st, extra={"speech": speech})
                reply = "Got it. What's your name?"
            else:
                reply = ("I can book Haircut, Beard Trim, Hot Towel Shave, Kids Haircut, "
                         "Wash and Style, or Color Touch-up. Which one would you like?")
            fragment = _tts_or_say(reply)
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

        # (b) name
        if st["customer_name"] is None:
            st["customer_name"] = speech
            log_event(call_sid, "name_captured", state=st)
            fragment = _tts_or_say("Thanks! What phone number should I use for your appointment?")
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

        # (c) phone
        if st["phone"] is None:
            st["phone"] = "".join(c for c in speech if c.isdigit())
            log_event(call_sid, "phone_captured", state=st)
            fragment = _tts_or_say("Do you have a preferred time, or should I find the next available slot?")
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

        # (d) propose
        if st["preferred_time"] is None:
            utter = speech.lower()
            next_syns = ("next", "soon", "asap", "earliest", "any", "first available", "next available")
            wants_next = any(k in utter for k in next_syns)
            st["preferred_time"] = None if wants_next else speech

            attempts = int(st.get("next_attempts") or "0")
            if wants_next:
                st["next_attempts"] = str(attempts + 1)

            days_ahead = 3 if attempts <= 0 else 7 if attempts == 1 else 14

            try:
                # IMPORTANT: use .invoke with a dict because tools expect a single 'tool_input'
                raw = propose_booking.invoke({
                    "service_code": st["service_code"],
                    "preferred_time": st["preferred_time"],
                    "days_ahead": days_ahead
                })
                raw = raw if isinstance(raw, str) else str(raw)
            except Exception as e:
                print("PROPOSE ERROR:", e)
                raw = "ERROR"

            info = None
            if raw and isinstance(raw, str) and raw.startswith("{"):
                try:
                    info = json.loads(raw)
                except Exception:
                    info = None

            if info and all(k in info for k in ("barber", "barber_id", "service_id", "start")):
                st["barber_id"] = str(info["barber_id"])
                st["service_id"] = str(info["service_id"])
                st["start"] = info["start"]
                st["awaiting_confirm"] = True
                st["next_attempts"] = "0"
                log_event(call_sid, "proposed", state=st, extra={"proposal": info})
                speak = f"I can book you with {info['barber']} at {info['start']}. Shall I confirm?"
                fragment = _tts_or_say(speak)
                xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/confirm" method="POST" speechTimeout="auto"/>
</Response>"""
                return Response(content=xml, media_type="text/xml")

            # No slot → avoid loops
            msg = ("I couldn't find a matching time yet. "
                   "Say ‘next available’ to widen the search, or say a specific day and time.")
            if wants_next and attempts >= 2:
                msg = ("I couldn't find any openings soon. "
                       "Please say a day and time, for example: ‘Friday at 3 PM’ or ‘tomorrow at 10 AM’.")
            log_event(call_sid, "no_slot", state=st, extra={"attempts": attempts})
            fragment = _tts_or_say(msg)
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

    # Fallback
    fragment = _tts_or_say("Sorry, I didn't catch that. Are you here to book an appointment, or would you like information?")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
    return Response(content=xml, media_type="text/xml")


@app.post("/faq_followup")
async def faq_followup(request: Request) -> Response:
    form = await request.form()
    call_sid = form.get("CallSid")
    speech = (form.get("SpeechResult") or "").lower()

    st = STATE.setdefault(call_sid, {
        "mode": None,
        "customer_name": None,
        "phone": None,
        "service_code": None,
        "preferred_time": None,
        "next_attempts": "0",
    })

    if any(w in speech for w in ["yes", "book", "sure", "ok", "okay", "yep", "yeah"]):
        st["mode"] = "BOOK"
        log_event(call_sid, "faq_to_booking", state=st)
        fragment = _tts_or_say("Great. What service would you like? Haircut, Beard Trim, Hot Towel Shave, Kids Haircut, Wash and Style, or Color Touch-up?")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")

    if any(w in speech for w in ["no", "not now", "later"]):
        log_event(call_sid, "faq_done", state=st)
        fragment = _tts_or_say("No worries. Thanks for calling Riverside Cuts. Have a great day!")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
</Response>"""
        STATE.pop(call_sid, None)
        return Response(content=xml, media_type="text/xml")

    # Treat it as another FAQ question
    reply = agent_decide_and_answer(speech, chat_history=[])
    if reply.startswith("ROUTE:BOOK"):
        st["mode"] = "BOOK"
        fragment = _tts_or_say("Happy to help you book. What service would you like?")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")

    log_event(call_sid, "faq_answered", state=st, extra={"answer": reply})
    fragment = _tts_or_say(reply)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/faq_followup" method="POST" speechTimeout="auto"/>
</Response>"""
    return Response(content=xml, media_type="text/xml")


@app.post("/confirm")
async def handle_confirm(request: Request) -> Response:
    form = await request.form()
    call_sid = form.get("CallSid")
    speech = (form.get("SpeechResult") or "").lower()
    st = STATE.get(call_sid, {}) or {}

    if any(w in speech for w in ["no", "later", "change"]):
        fragment = _tts_or_say("No problem. What time works for you?")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
        return Response(content=xml, media_type="text/xml")

    if any(w in speech for w in ["yes", "book", "confirm", "sure"]):
        required = ["customer_name", "phone", "barber_id", "service_id", "start"]
        missing = [k for k in required if not st.get(k)]
        if missing:
            fragment = _tts_or_say("I don't have a time locked in yet. Tell me your preferred time, or say next available.")
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

        try:
            barber_id = int(st["barber_id"])
            service_id = int(st["service_id"])

            # Use tool .invoke with dict input
            _ = confirm_booking.invoke({
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
            log_event(call_sid, "error", state=st, extra={"err": str(e)})
            fragment = _tts_or_say("I hit a snag confirming that. Should I try the next available time?")
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {fragment}
  <Gather input="speech" action="{settings.base_url}/gather" method="POST" speechTimeout="auto"/>
</Response>"""
            return Response(content=xml, media_type="text/xml")

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
