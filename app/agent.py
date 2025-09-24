# app/agent.py
from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Optional

from pydantic import BaseModel, Field
from dateutil.parser import parse as parse_dt, ParserError

# LangChain / OpenAI
from langchain_openai import ChatOpenAI
from langchain.tools import tool
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder

# App modules
from app.rag import RAG
from app.config import settings
from app.db import SessionLocal, Service
from app.scheduler import find_slot, book


# ============================================================
# RAG (FAQ) Tool — fail-safe with hard timeout
# ============================================================
_rag = RAG(settings.rag_index_path)
_rag.load()

def _rag_query_with_timeout(q: str, seconds: int = 6) -> str:
    """
    Run RAG query with a hard timeout and never raise.
    Returns a short string answer (or a friendly fallback).
    """
    result = {"text": "Sorry, I couldn't access the knowledge base right now. Please ask again or try booking."}
    done = threading.Event()

    def run():
        try:
            out = _rag.query(q)  # your RAG returns a str
            if out and out.strip():
                result["text"] = out.strip()
            else:
                result["text"] = "I don’t have that info yet."
        except Exception as e:
            print(f"[kb_search] RAG error: {e}", flush=True)
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    done.wait(seconds)
    return result["text"]

@tool
def kb_search(query: str) -> str:
    """Search the shop knowledge base (services, timing, pricing, policies, cancellation, location)."""
    try:
        print(f"[kb_search] query={query}", flush=True)
        ans = _rag_query_with_timeout(query, seconds=6)
        print(f"[kb_search] answer={ans[:160]!r}", flush=True)
        return ans
    except Exception as e:
        # Never raise from a tool; keep Twilio flow alive.
        print(f"[kb_search] fatal error: {e}", flush=True)
        return "Sorry, I couldn't access the knowledge base right now. Please ask again or try booking."


# ============================================================
# Deterministic booking helpers (also exposed as tools)
# ============================================================
class ProposeBookingInput(BaseModel):
    """Find next available slot for a service. Use preferred_time (ISO or natural language) when provided."""
    service_code: str = Field(..., description="Canonical code: HAIRCUT, BEARD, SHAVE, KIDS, STYLE, COLOR")
    preferred_time: Optional[str] = Field(None, description="Caller-preferred time (ISO or natural language). None = next available")
    days_ahead: int = Field(3, ge=1, le=30, description="Search window in days when preferred_time is None (widens if needed)")

@tool(args_schema=ProposeBookingInput)
def propose_booking(service_code: str, preferred_time: Optional[str] = None, days_ahead: int = 3) -> str:
    """
    Return JSON with a proposed slot or NO_SLOT/SERVICE_NOT_FOUND.
    Success example:
      {"barber":"Alex","barber_id":1,"service_id":2,"start":"2025-09-23T15:00:00","end":"2025-09-23T15:30:00"}
    """
    session = SessionLocal()
    try:
        svc = session.query(Service).filter(Service.code == service_code).first()
        if not svc:
            return "SERVICE_NOT_FOUND"

        ts = None
        if preferred_time:
            try:
                ts = parse_dt(preferred_time)
            except (ParserError, TypeError, ValueError):
                # Treat unparsable input as "next available"
                ts = None

        # If your find_slot supports a days_ahead kw, use it; otherwise call without it.
        try:
            if "days_ahead" in find_slot.__code__.co_varnames:  # type: ignore[attr-defined]
                slot, msg = find_slot(session, svc.id, ts, days_ahead=days_ahead)  # type: ignore[misc]
            else:
                slot, msg = find_slot(session, svc.id, ts)
        except TypeError:
            # Defensive: old signature
            slot, msg = find_slot(session, svc.id, ts)

        if not slot:
            return f"NO_SLOT:{msg}"

        out = {
            "barber": slot.barber.name,
            "barber_id": slot.barber_id,
            "service_id": svc.id,
            "start": slot.start.isoformat(),
            "end": (slot.start + timedelta(minutes=svc.duration_min)).isoformat(),
        }
        return json.dumps(out)
    except Exception as e:
        print(f"[propose_booking] error: {e}", flush=True)
        return "NO_SLOT:internal_error"
    finally:
        session.close()


class ConfirmBookingInput(BaseModel):
    customer_name: str
    phone: str
    barber_id: int
    service_id: int
    start_iso: str

@tool(args_schema=ConfirmBookingInput)
def confirm_booking(customer_name: str, phone: str, barber_id: int, service_id: int, start_iso: str) -> str:
    """Persist the appointment and return a confirmation string."""
    session = SessionLocal()
    try:
        try:
            start_dt = parse_dt(start_iso)
        except (ParserError, TypeError, ValueError):
            return "BOOKING_FAILED:invalid_start_time"

        appt = book(session, customer_name, phone, barber_id, service_id, start_dt)
        return f"BOOKED#{appt.id} for {appt.customer_name} with {appt.barber.name} at {appt.start.isoformat()}"
    except Exception as e:
        print(f"[confirm_booking] error: {e}", flush=True)
        return "BOOKING_FAILED:internal_error"
    finally:
        session.close()


# ============================================================
# Routing Agent (FAQ vs BOOK)
#   • The agent ONLY has the FAQ tool (kb_search)
#   • Booking is handled deterministically by your endpoints
# ============================================================
TOOLS = [kb_search]

SYSTEM = """You are Riverside Cuts' voice assistant.
You can do two things:
1) Answer FAQs (timing, services, pricing, cancellation, location) using the kb_search tool.
2) Route to booking when the caller wants to make or modify an appointment.

Rules:
- If the user asks for info/policies, call kb_search with their exact question, then reply briefly and ask:
  "Would you like me to make an appointment?"
- If the user wants to book (mentions booking/next available, or a service like haircut/beard/style/kids/color),
  reply exactly: ROUTE:BOOK
- If unsure, ask a concise clarification, then ask if they want to book.
- Keep answers short, friendly, and specific to the question.
"""

PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ]
)

# Tight timeouts so Twilio won't hang
llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
    timeout=15,     # network timeout (seconds)
    max_retries=1,  # fail fast
)

_agent = create_openai_tools_agent(
    llm=llm,
    tools=TOOLS,
    prompt=PROMPT,
)

agent = AgentExecutor(agent=_agent, tools=TOOLS, verbose=False)


def agent_decide_and_answer(user_text: str, chat_history: Optional[list] = None) -> str:
    """
    Returns either:
      - 'ROUTE:BOOK'
      - short FAQ answer + follow-up line
    Always returns a string; never raises.
    """
    chat_history = chat_history or []
    try:
        out = agent.invoke({"input": user_text, "chat_history": chat_history})
        reply = (out.get("output") or "").strip()
        if not reply:
            reply = "I can share our hours, pricing, or cancellation info. Would you like me to make an appointment?"
        return reply
    except Exception as e:
        # Fail-safe: never crash the caller flow
        print(f"[agent_decide_and_answer] error: {e}", flush=True)
        return "I’m having trouble fetching that info right now. Would you like me to make an appointment?"
