"""Draft LinkedIn outreach from your resume and a person's profile.

You supply the person's profile (paste the text, or at least their headline); the tool never fetches LinkedIn.
It checks for a shared USC background in code (so the claim isn't left to the model), writes a short connection
note and a longer follow-up message, and shows the evidence behind every personal detail so you can check it
before sending. Nothing is sent from here: you copy and send on LinkedIn yourself.
"""
import re

from . import config, llm, outreach, resume

NOTE_MAX = 200          # LinkedIn caps invitation notes at 200 characters on free accounts (300 on Premium)
MESSAGE_MAX = 1400

# ---------------------------------------------------------------- USC check (code, not the model)

_USC_STRONG = re.compile(
    r"university of southern california|usc (viterbi|marshall|annenberg|dornsife|price|keck|roski|iovine|leventhal|gould|rossier)|"
    r"\btrojan(s)?\b|\busc\b[’']?\s*'?’?\d{2}\b|\bviterbi\b|southern california.{0,25}(alum|alumn)", re.I)
_USC_BARE = re.compile(r"\busc\b", re.I)
_NOT_USC = re.compile(r"university of south carolina|south carolina|usc upstate|usc aiken|usc beaufort", re.I)


def detect_usc(text: str) -> dict:
    """yes = clear USC (Los Angeles) signal; maybe = a bare 'USC' (could be South Carolina), so confirm; no = none."""
    text = text or ""
    strong = [m.group(0) for m in _USC_STRONG.finditer(text)]
    if strong and not _NOT_USC.search(text):
        return {"status": "yes", "evidence": sorted(set(s.strip() for s in strong), key=str.lower)[:4]}
    bare = _USC_BARE.findall(text)
    if bare and not _NOT_USC.search(text):
        return {"status": "maybe", "evidence": ["mentions “USC” (could be another USC; confirm before using)"]}
    if _NOT_USC.search(text):
        return {"status": "no", "evidence": ["mentions University of South Carolina, not USC Los Angeles"]}
    return {"status": "no", "evidence": []}


_SUFFIXES = {"phd", "ph", "md", "mba", "jr", "sr", "ii", "iii", "iv", "cpa", "pmp", "pe", "dr", "ms", "msc", "bs", "mph"}


def name_from_slug(slug: str) -> str:
    """'timothy-greer-ph-d-28630671' -> 'Timothy Greer' (credential and id tokens dropped)."""
    words = [w for w in slug.split("-") if w.isalpha()]
    while words and (words[-1] in _SUFFIXES or len(words[-1]) == 1):
        words.pop()
    return " ".join(w.capitalize() for w in words[:3])


def clean_profile_url(url: str) -> tuple[str, str]:
    """(canonical profile URL without tracking params, slug) or ('', '')."""
    slug = outreach.slug_from_url(url)
    return (f"https://www.linkedin.com/in/{slug}", slug) if slug else ("", "")


# ---------------------------------------------------------------- the drafting prompt

_PROMPT = """You help a student/job seeker write short, genuine LinkedIn outreach to someone whose work interests them.

SENDER_RESUME (the only source of facts about the sender):
{resume}

SENDER_GOAL: {goal}
SENDER_FIRST_NAME: {first_name}

RECIPIENT (the only source of facts about the recipient; may be only a headline):
Name: {name}
Details pasted by the sender:
{profile}

USC CHECK (computed by code, authoritative): {usc_line}

RULES
- Use ONLY facts present above. Never invent employers, projects, papers, mutual connections, events or shared experiences.
- If the recipient details are thin, stay general and add a warning asking the sender to paste more of their profile.
- Pick ONE or TWO specific hooks that connect the sender's real background to the recipient's real work. Each hook needs
  a short quote from the source text as evidence.
- If the USC check says yes, mention the shared USC / Trojan connection naturally in both messages. If maybe or no, do not claim it.
- Ask for something small (a short chat or a bit of advice). Do not ask for a referral or a job.
- Never state or imply the sender's availability, schedule, location, visa status or any commitment that is not in the resume.
  Do not propose specific times; just offer to work around their schedule.
- Respect tense: a degree or job marked expected, in progress or present is NOT finished (say "pursuing", "currently", "graduating in 2027").
- Copy numbers, titles and names exactly as written in the sources; never round, upgrade or paraphrase them.
- Warm, direct, specific. First person. No emojis, no flattery clichés, no "I hope this finds you well", no buzzwords.
- "note": a LinkedIn connection note, at most {note_max} characters INCLUDING spaces. Greet by first name.
- "message": a follow-up to send once they accept, 4 to 6 sentences, at most {message_max} characters, signed with the sender's first name.

Return ONLY this JSON:
{{"recipient": {{"name": "...", "role": "...", "company": "...", "education": ["..."]}},
  "hooks": [{{"claim": "...", "source": "resume|profile", "quote": "..."}}],
  "note": "...", "message": "...", "warnings": ["..."]}}"""


_VERIFY_PROMPT = """You are a strict fact-checker for outreach messages. Compare every factual claim in the DRAFT about the sender or the
recipient with the two SOURCES. A claim is unsupported if the sources don't state it, or state something weaker or different
(e.g. "leads" when the source says "builds", "under 50 ms" when the source says "50 ms p95", "earned a degree" when it is
marked expected, any invented availability, event, mutual connection or number).

SENDER SOURCE (resume):
{resume}

RECIPIENT SOURCE (their profile text):
{profile}

DRAFT:
Note: {note}
Message: {message}

Return ONLY JSON: {{"unsupported": [{{"claim": "exact words from the draft", "issue": "what the sources actually say, in under 20 words"}}]}}
Return an empty list if everything is supported. Ignore greetings, the request itself, and opinions. DO flag any statement about the sender's availability, schedule,
location, visa status or plans, and any claim about the recipient that is stronger than their profile text."""


def verify(resume_text: str, profile: str, note: str, message: str) -> list[str]:
    """Second-pass check: warnings for claims the sources don't support. Failure here never blocks a draft."""
    try:
        out = llm.chat_json(_VERIFY_PROMPT.format(resume=resume_text[:7000], profile=(profile or "(only a name was given)")[:5000],
                                                  note=note, message=message), temperature=0, effort="low", label="verify")
    except llm.LLMExhausted:
        return []
    items = (out or {}).get("unsupported") or []
    return [f"Check: “{str(i.get('claim', ''))[:140]}” — {str(i.get('issue', ''))[:160]}" for i in items if isinstance(i, dict) and i.get("claim")][:5]


def _tidy(text: str) -> str:
    """Models like non-breaking hyphens and odd spaces; they paste badly into LinkedIn."""
    for bad, good in (("\u2011", "-"), ("\u2010", "-"), ("\u2012", "-"), ("\u00a0", " "), ("\u202f", " "), ("\u200b", "")):
        text = text.replace(bad, good)
    return text.strip()


def _usc_line(usc: dict, override: str | None) -> str:
    status = override or usc["status"]
    ev = "; ".join(usc["evidence"]) or "no USC signal found"
    return {"yes": f"yes, the recipient attended USC ({ev})" if not override else "yes (confirmed by the sender)",
            "maybe": f"unclear ({ev})", "no": "no, do not mention USC" if not override else "no (confirmed by the sender)"}[status]


def build_prompt(resume_text: str, name: str, profile: str, usc: dict, override: str | None, goal: str, first_name: str,
                 feedback: str = "") -> str:
    p = _PROMPT.format(resume=resume_text[:7000], goal=goal, first_name=first_name or "(sign with the name on the resume)",
                       name=name or "(unknown; infer from the details)", profile=(profile or "(nothing pasted)")[:5000],
                       usc_line=_usc_line(usc, override), note_max=NOTE_MAX - 10, message_max=MESSAGE_MAX)
    return p + (f"\n\nFIX THIS FROM THE LAST ATTEMPT: {feedback}" if feedback else "")


def draft(*, name: str, profile_text: str, url: str = "", usc_override: str | None = None, goal: str | None = None) -> dict:
    """Returns {recipient, usc, hooks, note, message, warnings}. Raises ValueError (no resume / no LLM)."""
    resume_text = resume.load_text()
    if not resume_text:
        raise ValueError("Upload your resume first (Outreach tab → Resume).")
    if not llm.available():
        raise ValueError("Drafting needs GROQ_API_KEY in .env.")
    usc = detect_usc(f"{name}\n{profile_text}")
    goal = goal or config.OUTREACH_GOAL
    feedback, out = "", None
    for attempt in range(2):
        out = llm.chat_json(build_prompt(resume_text, name, profile_text, usc, usc_override, goal, resume.first_name(), feedback),
                            temperature=0.5, effort="medium", label="drafting")
        if not out or not out.get("note") or not out.get("message"):
            out = None
            feedback = "Return the complete JSON with non-empty note and message."
            continue
        problems = []
        if len(out["note"]) > NOTE_MAX:
            problems.append(f'"note" is {len(out["note"])} characters; it must be at most {NOTE_MAX - 10}. Shorten it.')
        if len(out["message"]) > MESSAGE_MAX:
            problems.append(f'"message" is {len(out["message"])} characters; it must be at most {MESSAGE_MAX}.')
        if problems:
            feedback = " ".join(problems)
            if attempt == 0:
                continue
        break
    if not out:
        raise ValueError("The drafting model didn't return a usable draft. Try again.")
    warnings = list(out.get("warnings") or [])
    if len(out["note"]) > NOTE_MAX:
        warnings.append(f"The connection note is {len(out['note'])} characters; LinkedIn allows {NOTE_MAX}. Trim it before sending.")
    if len((profile_text or "").strip()) < 120:
        warnings.append("Very little profile text was provided, so the draft can only be general. Paste more of their profile for a specific one.")
    status = usc_override or usc["status"]
    if status == "yes" and "usc" not in (out["note"] + out["message"]).lower() and "trojan" not in (out["note"] + out["message"]).lower():
        warnings.append("They look like a USC alum but the draft doesn't mention it; add a line or regenerate.")
    warnings += verify(resume_text, profile_text, out["note"], out["message"])
    return {"recipient": out.get("recipient") or {}, "usc": {**usc, "used": status}, "hooks": out.get("hooks") or [],
            "note": _tidy(out["note"]), "message": _tidy(out["message"]), "warnings": warnings}
