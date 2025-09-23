from rapidfuzz import process, fuzz

CANON = {
    "HAIRCUT": ["haircut", "cut", "trim", "hair styling and cut", "style and cut", "men's cut", "ladies cut"],
    "BEARD": ["beard trim", "beard", "line up", "shape up"],
    "SHAVE": ["shave", "hot towel shave", "razor shave"],
    "KIDS": ["kids haircut", "child cut", "children haircut"],
    "STYLE": ["wash & style", "wash and style", "blow dry", "styling"],
    "COLOR": ["color", "color touch up", "coloring"]
}

CANON_LIST = [(code, alias) for code, aliases in CANON.items() for alias in aliases]

def normalize_service(user_text: str) -> str | None:
    match = process.extractOne(user_text.lower(), [a for _, a in CANON_LIST], scorer=fuzz.WRatio)
    if match and match[1] >= 80:  # confidence threshold
        for code, alias in CANON_LIST:
            if alias == match[0]:
                return code
    # try direct contains
    for code, aliases in CANON.items():
        if any(a in user_text.lower() for a in aliases):
            return code
    return None