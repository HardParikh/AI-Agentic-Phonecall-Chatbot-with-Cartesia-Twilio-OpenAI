# app/tts.py
import os
import uuid
from typing import Optional, Final, Iterable, Union

# Official Cartesia SDK
from cartesia import Cartesia

# ─────────────────────────────────────────────────────────
# Config (override via .env)
# ─────────────────────────────────────────────────────────
CARTESIA_MODEL: Final[str] = os.getenv("CARTESIA_MODEL", "sonic-2")
CARTESIA_VERSION: Final[str] = os.getenv("CARTESIA_VERSION", "2025-04-16")
DEFAULT_SAMPLE_RATE: Final[int] = int(os.getenv("CARTESIA_SAMPLE_RATE", "24000"))
DEFAULT_LANGUAGE: Final[str] = os.getenv("CARTESIA_LANGUAGE", "en")
DEFAULT_CONTAINER: Final[str] = os.getenv("CARTESIA_CONTAINER", "wav")  # "wav" or "mp3"

AUDIO_DIR: Final[str] = "static/audio"


def _cartesia_client(api_key: Optional[str] = None) -> Cartesia:
    key = api_key or os.getenv("CARTESIA_API_KEY")
    if not key:
        raise RuntimeError(
            "CARTESIA_API_KEY missing. Set it in your environment or pass api_key explicitly."
        )
    return Cartesia(api_key=key)


def _normalize_audio_bytes(
    audio: Union[bytes, bytearray, memoryview, Iterable]
) -> bytes:
    """Cartesia SDK may return bytes or an iterable/stream of chunks; normalize to bytes."""
    if isinstance(audio, (bytes, bytearray, memoryview)):
        return bytes(audio)

    # Otherwise, iterate chunks and stitch together
    chunks: list[bytes] = []
    for part in audio:
        if isinstance(part, (bytes, bytearray, memoryview)):
            chunks.append(bytes(part))
        elif isinstance(part, dict) and "audio" in part and isinstance(part["audio"], str):
            import base64 as _b64
            chunks.append(_b64.b64decode(part["audio"]))
        elif hasattr(part, "read"):
            chunks.append(part.read())
        elif hasattr(part, "content"):
            chunks.append(part.content)
        else:
            raise TypeError(f"Unexpected TTS stream chunk type: {type(part)}")
    return b"".join(chunks)


def tts_bytes(
    text: str,
    voice_id: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    container: str = DEFAULT_CONTAINER,   # "wav" or "mp3"
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    language: str = DEFAULT_LANGUAGE,
) -> bytes:
    """
    Synthesize speech with Cartesia Sonic-2 and return raw audio bytes.
    Mirrors the working request shape from your Streamlit reference.
    """
    client = _cartesia_client(api_key)

    vid = voice_id or os.getenv("VOICE_ID")
    if not vid:
        raise RuntimeError("VOICE_ID missing. Set it in .env or pass voice_id explicitly.")

    # Cartesia output_format differs by container
    if container == "mp3":
        output_format = {"container": "mp3", "bit_rate": 128000, "sample_rate": sample_rate}
    elif container == "wav":
        # PCM S16LE is standard/widely compatible for Twilio <Play>
        output_format = {"container": "wav", "encoding": "pcm_s16le", "sample_rate": sample_rate}
    else:
        raise ValueError("Unsupported container. Use 'wav' or 'mp3'.")

    # The SDK accepts request_options to pass the required Cartesia-Version header
    audio = client.tts.bytes(
        model_id=CARTESIA_MODEL,
        transcript=text,
        voice={"mode": "id", "id": vid},
        output_format=output_format,
        language=language,
        request_options={"headers": {"Cartesia-Version": CARTESIA_VERSION}},
    )
    return _normalize_audio_bytes(audio)


def save_tts_file(
    text: str,
    voice_id: Optional[str] = None,
    api_key: Optional[str] = None,
    *,
    out_dir: str = AUDIO_DIR,
    container: str = DEFAULT_CONTAINER,   # "wav" or "mp3"
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """
    Synthesize and write audio to disk. Returns a *relative* URL path you can serve to Twilio:
      e.g. "/static/audio/<uuid>.wav"
    """
    os.makedirs(out_dir, exist_ok=True)
    audio = tts_bytes(
        text,
        voice_id,
        api_key,
        container=container,
        sample_rate=sample_rate,
        language=language,
    )
    fname = f"{uuid.uuid4().hex}.{container}"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "wb") as f:
        f.write(audio)

    # Twilio will fetch via PUBLIC_BASE_URL + this return value.
    # If out_dir is "static/audio", this returns "/static/audio/<file>"
    rel_root = out_dir.replace("\\", "/").lstrip("./")
    return f"/{rel_root}/{fname}"
