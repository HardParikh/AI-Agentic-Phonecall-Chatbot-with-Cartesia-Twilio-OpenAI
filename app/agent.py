# app/agent.py
from datetime import timedelta
from typing import Optional
import json

from dateutil.parser import parse as parse_dt  # robust ISO/date parsing

from langchain_openai import ChatOpenAI
from langchain.tools import tool
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.db import SessionLocal, Service
from app.scheduler import find_slot, book
from app.rag import RAG
from app.config import settings

# Initialize RAG once
rag = RAG(settings.rag_index_path)
rag.load()


@tool
def kb_search(query: str) -> str:
    """Search the shop knowledge base (services, policies)."""
    return rag.query(query)


from typing import Optional
from datetime import timedelta
from dateutil.parser import parse as parse_dt

@tool
def propose_booking(service_code: str, preferred_time: Optional[str] = None, days_ahead: int = 14) -> str:
    """
    Find next available slot for a service. If preferred_time is given, try that day first,
    otherwise search from now forward up to days_ahead days. Returns JSON on success or
    'NO_SLOT:reason' on failure.
    """
    session = SessionLocal()
    try:
        svc = session.query(Service).filter(Service.code == service_code).first()
        if not svc:
            return "SERVICE_NOT_FOUND"

        start_ts = None
        if preferred_time:
            try:
                start_ts = parse_dt(preferred_time)
            except Exception:
                start_ts = None

        # Try preferred day first (if provided), then roll forward up to days_ahead
        for d in range(0, max(1, days_ahead)):
            ts = (start_ts or None)
            if ts:
                ts = ts + timedelta(days=d)
            else:
                # None signals "from now" to find_slot; if your find_slot needs a timestamp, pass now
                from datetime import datetime
                ts = datetime.now() + timedelta(days=d)

            slot, msg = find_slot(session, svc.id, ts)
            if slot:
                out = {
                    "barber": slot.barber.name,
                    "barber_id": slot.barber_id,
                    "service_id": svc.id,
                    "start": slot.start.isoformat(),
                    "end": (slot.start + timedelta(minutes=svc.duration_min)).isoformat(),
                }
                import json
                return json.dumps(out)

        return "NO_SLOT:No openings in next {} days".format(days_ahead)
    finally:
        session.close()

@tool
def confirm_booking(customer_name: str, phone: str, barber_id: int, service_id: int, start_iso: str) -> str:
    """Write the appointment to DB."""
    session = SessionLocal()
    try:
        appt = book(session, customer_name, phone, barber_id, service_id, parse_dt(start_iso))
        return f"BOOKED#{appt.id} for {appt.customer_name} with {appt.barber.name} at {appt.start}"
    finally:
        session.close()


# ---- LLM & modern Tools Agent ----
llm = ChatOpenAI(model="gpt-4o-mini")

tools = [kb_search, propose_booking, confirm_booking]

prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a friendly barber shop phone agent.\n"
            "Service catalog is FIXED: HAIRCUT, BEARD, SHAVE, KIDS, STYLE, COLOR.\n"
            "Infer flexibly (e.g., 'hair styling and cut' -> HAIRCUT or STYLE).\n"
            "Collect: customer_name, phone, service_code, preferred_time (optional).\n"
            "Use kb_search for policies; use propose_booking to find a slot; "
            "after caller confirms, use confirm_booking.\n"
            "Ask concise follow-ups if information is missing."
        ),
        # Optional but useful for multi-turn:
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
        # Required for tools agents:
        MessagesPlaceholder("agent_scratchpad"),
    ]
)

agent_graph = create_openai_tools_agent(llm=llm, tools=tools, prompt=prompt)
agent = AgentExecutor(agent=agent_graph, tools=tools, verbose=False)