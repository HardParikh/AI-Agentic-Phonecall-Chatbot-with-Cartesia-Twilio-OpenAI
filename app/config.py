from pydantic import BaseModel
import os


class Settings(BaseModel):
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    cartesia_api_key: str = os.getenv("CARTESIA_API_KEY", "")
    twilio_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_number: str = os.getenv("TWILIO_PHONE_NUMBER", "")
    base_url: str = os.getenv("PUBLIC_BASE_URL", "")
    db_url: str = os.getenv("DB_URL", "sqlite:///./barber.db")
    rag_index_path: str = os.getenv("RAG_INDEX_PATH", "./vectorstore")
    voice_id: str = os.getenv("VOICE_ID", "sonic-en-US-001")


settings = Settings()