"""Draft LinkedIn outreach from your resume and a person's profile.

You supply the person's profile (paste the text, or at least their headline); the tool never fetches LinkedIn.
It checks for a shared USC background in code (so the claim isn't left to the model), writes a short connection
note and a longer follow-up message, and shows the evidence behind every personal detail so you can check it
before sending. Nothing is sent from here: you copy and send on LinkedIn yourself.
"""
import re

from . import config, llm, outreach, resume

MESSAGE_MAX = 1400

def note_max() -> int:
    return config.LINKEDIN_NOTE_MAX


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

_PROMPT = """You help a student/job seeker write LinkedIn outreach that reads like a thoughtful person wrote it, not a template.

SENDER_RESUME (the only source of facts about the sender):
{resume}

SENDER_GOAL: {goal}
SENDER_FIRST_NAME: {first_name}

RECIPIENT (the only source of facts about the recipient; may be only a headline):
Name: {name}
Details pasted by the sender:
{profile}

USC CHECK (computed by code, authoritative): {usc_line}

FACT RULES
- Use ONLY facts present above. Never invent employers, projects, papers, mutual connections, events or shared experiences.
- Never state or imply the sender's availability, schedule, location, visa status or any commitment that is not in the resume.
  Do not propose specific times; offering to work around their schedule is fine.
- Respect tense: a degree or job marked expected, in progress or present is NOT finished ("pursuing", "currently", "graduating in 2027").
- Copy numbers, titles and names exactly as written in the sources; never round, upgrade or paraphrase them.
- Show interest as QUESTIONS, never as claims. Do not write that the sender is "exploring", "working on", "interested in" or "focused on"
  anything unless the resume or SENDER_GOAL says so, and do not attribute focus areas, publications or opinions to the recipient
  beyond their own text. A fact about the sender must be traceable to a specific line of the resume.
- If the recipient details are thin, stay general and add a warning asking the sender to paste more of their profile.
- If the USC check says yes, mention the shared USC / Trojan connection naturally in both messages. If maybe or no, do not claim it.

WHAT MAKES IT GOOD
- SPECIFIC beats polite. Name ONE concrete detail from the recipient's own text (a named project, system, paper, talk, product,
  or the particular problem they work on), not just their employer or job title. Connect it to ONE concrete thing the sender
  actually built or studied (a named project, technique or result from the resume).
- A small, easy-to-answer ask (one question, or a 15-minute chat). Never ask for a referral or a job.
- Warm, direct, first person, like a person. No emojis, no flattery clichés, no buzzwords.
- BANNED phrases: "quick chat about your work at", "pick your brain", "would love to connect", "I hope this finds you well",
  "I came across your profile", "reaching out because", "passionate about", "I'd be honored".

"note" (connection note): between {note_min} and {note_max} characters INCLUDING spaces. Use nearly the whole allowance (aim for about
{note_target}); a note much shorter than {note_min} wastes space that could carry the specific detail. Shape: greeting by first name;
USC tie if confirmed; their specific detail; the sender's connecting detail; the small ask.
"message" (sent after they accept): {message_min} to {message_max} characters, 5 to 7 sentences, signed with the sender's first name.
Shape: one sentence on who the sender is (school, program, expected graduation, focus); a specific paragraph linking a concrete
project/experience of the sender to a concrete piece of their work, using details from BOTH sources; what the sender is looking for
(from SENDER_GOAL, briefly); one specific question that is easy to answer; thanks.

STYLE REFERENCE ONLY (fictional people; never reuse their facts or wording):
note (about 190 chars): "Hi Priya, fellow Trojan here. Your last-mile routing work at Zeta (the cost-per-stop model) caught my eye; I built a demand-forecasting pipeline during my internship. Could I ask you two questions?"
message opening: "Hi Priya, I'm Sam, an M.S. student at USC graduating in 2027 and focused on applied ML for logistics. ..."

Return ONLY this JSON:
{{"recipient": {{"name": "...", "role": "...", "company": "...", "education": ["..."]}},
  "hooks": [{{"claim": "...", "source": "resume|profile", "quote": "..."}}],
  "note": "...", "message": "...", "warnings": ["..."]}}"""


_VERIFY_PROMPT = """You are a strict fact-checker for outreach messages. Compare every factual claim in the DRAFT about the sender or the
recipient with the two SOURCES. A claim is unsupported if the sources don't state it, or state something weaker or different
(e.g. "leads" when the source says "builds", "under 50 ms" when the source says "50 ms p95", "earned a degree" when it is
marked expected, any invented availability, event, mutual connection or number).

SENDER SOURCE (resume, plus the sender's own statement of what they are looking for):
{resume}
Sender's stated goal: {goal}

RECIPIENT SOURCE (their profile text):
{profile}

DRAFT:
Note: {note}
Message: {message}

Return ONLY JSON: {{"unsupported": [{{"claim": "exact words from the draft", "issue": "what the sources actually say, in under 20 words"}}]}}
Return an empty list if everything is supported. Ignore greetings, the request itself, and opinions. DO flag any statement about the sender's availability, schedule,
location, visa status or plans, and any claim about the recipient that is stronger than their profile text."""


def verify_issues(resume_text: str, profile: str, note: str, message: str, goal: str = "") -> list[dict]:
    """Second-pass check: claims the sources don't support. Failure here never blocks a draft."""
    try:
        out = llm.chat_json(_VERIFY_PROMPT.format(resume=resume_text[:5000], goal=goal or config.OUTREACH_GOAL,
                                                  profile=(profile or "(only a name was given)")[:3800], note=note, message=message),
                            temperature=0, effort="low", label="verify", max_tokens=2000)
    except llm.LLMExhausted:
        return []
    items = (out or {}).get("unsupported") or []
    return [i for i in items if isinstance(i, dict) and i.get("claim")][:5]


def _format_issues(items: list[dict]) -> list[str]:
    return [f"Check: “{str(i.get('claim', ''))[:140]}” — {str(i.get('issue', ''))[:160]}" for i in items]


def verify(resume_text: str, profile: str, note: str, message: str, goal: str = "") -> list[str]:
    return _format_issues(verify_issues(resume_text, profile, note, message, goal))


_REPAIR_PROMPT = """Fix this LinkedIn outreach draft. A fact-checker found claims that the sources do not support. Remove or rewrite ONLY those
claims: replace each with a supported fact from the sources or with a question. Keep everything else (tone, structure, the specific
supported details, the ask). The note must stay at most {note_max} characters and the message at most {message_max}.

SENDER SOURCE (resume): {resume}
Sender's stated goal: {goal}
RECIPIENT SOURCE (their profile text): {profile}

UNSUPPORTED CLAIMS TO FIX:
{issues}

CURRENT DRAFT
Note: {note}
Message: {message}

Return ONLY JSON: {{"note": "...", "message": "..."}}"""


def repair(resume_text: str, profile: str, note: str, message: str, issues: list[dict], goal: str) -> tuple[str, str] | None:
    try:
        out = llm.chat_json(_REPAIR_PROMPT.format(
            note_max=note_max() - 5, message_max=MESSAGE_MAX, resume=resume_text[:5000], goal=goal, profile=(profile or "")[:3800],
            issues="\n".join(f"- “{i.get('claim')}”: {i.get('issue')}" for i in issues), note=note, message=message),
            temperature=0.3, effort="low", label="repair", max_tokens=4000)
    except llm.LLMExhausted:
        return None
    n, m = _tidy(str((out or {}).get("note") or "")), _tidy(str((out or {}).get("message") or ""))
    return (n, m) if n and m else None          # length is fitted afterwards


def _tidy(text: str) -> str:
    """Models like non-breaking hyphens and odd spaces; they paste badly into LinkedIn."""
    for bad, good in (("\u2011", "-"), ("\u2010", "-"), ("\u2012", "-"), ("\u00a0", " "), ("\u202f", " "), ("\u200b", "")):
        text = text.replace(bad, good)
    return text.strip()


_SHORTEN_PROMPT = """Rewrite the LinkedIn {field} below so it is AT MOST {limit} characters including spaces (it is {n} now).
Remove filler words first. Keep the greeting, any shared-USC mention, the specific details about the recipient's work and the
sender's project, and the ask. Do not add new facts. Return ONLY JSON: {{"text": "..."}}

{field}: {text}"""


def fit(text: str, field: str, limit: int) -> str | None:
    """Ask for a shorter rewrite (cheap, targeted); None if it can't be made to fit. Models can't count characters by feel,
    so each retry says exactly how far over the previous attempt was."""
    for attempt in range(3):
        over = len(text) - (limit - 5)
        prompt = _SHORTEN_PROMPT.format(field=field, limit=limit - 5, n=len(text), text=text)
        if attempt:
            prompt += f"\n\nYour last version was still {over} characters too long. Cut at least {over + 15} more characters."
        if attempt == 2:
            prompt += " Keep ONLY: the greeting, the shared-USC mention if any, one specific detail of theirs, one of the sender's, and the ask."
        try:
            out = llm.chat_json(prompt, temperature=0.3, effort="low", label="shorten", max_tokens=2000)
        except llm.LLMExhausted:
            return None
        t = _tidy(str((out or {}).get("text") or ""))
        if t and len(t) <= limit:
            return t
        text = t or text
    return None


def _usc_line(usc: dict, override: str | None) -> str:
    status = override or usc["status"]
    ev = "; ".join(usc["evidence"]) or "no USC signal found"
    return {"yes": f"yes, the recipient attended USC ({ev})" if not override else "yes (confirmed by the sender)",
            "maybe": f"unclear ({ev})", "no": "no, do not mention USC" if not override else "no (confirmed by the sender)"}[status]


def build_prompt(resume_text: str, name: str, profile: str, usc: dict, override: str | None, goal: str, first_name: str,
                 feedback: str = "", style: str = "", points: str = "", short: bool = False) -> str:
    limit = note_max()
    p = _PROMPT.format(resume=resume_text[:5000], goal=goal, first_name=first_name or "(sign with the name on the resume)",
                       name=name or "(unknown; infer from the details)", profile=(profile or "(nothing pasted)")[:3800],
                       usc_line=_usc_line(usc, override), note_max=limit - 5, note_min=round(limit * 0.82), note_target=round(limit * 0.93),
                       message_min=350 if short else 650, message_max=750 if short else MESSAGE_MAX - 400)
    p += f"\n\nSTYLE: {style}" if style else ""
    p += f"\n\nRESUME LINES MOST RELEVANT TO THE ROLE THE SENDER IS TARGETING (verified; prefer one as the connecting detail):\n{points}" if points else ""
    return p + (f"\n\nFIX THIS FROM THE LAST ATTEMPT: {feedback}" if feedback else "")


def draft(*, name: str, profile_text: str, url: str = "", usc_override: str | None = None, goal: str | None = None,
          tone: str = "standard", length: str = "standard", instructions: str = "", job_text: str = "") -> dict:
    """Returns {recipient, usc, hooks, note, message, warnings}. Raises ValueError (no resume / no LLM).
    tone / length / instructions shape the style only; job_text (a role the sender is targeting) selects the resume lines to lean on."""
    from . import recruiter                      # imported here: recruiter.py builds on this module
    resume_text = resume.load_text()
    if not resume_text:
        raise ValueError("Upload your resume first (Outreach tab → Resume).")
    if not llm.available():
        raise ValueError("Drafting needs GROQ_API_KEY in .env.")
    usc = detect_usc(f"{name}\n{profile_text}")
    goal = goal or config.OUTREACH_GOAL
    feedback, out, held, limit = "", None, None, note_max()
    tone_text, extra = recruiter._style(tone, length, instructions)
    style = f"tone is {tone_text}. {extra}".strip() if (tone != "standard" or extra) else ""
    pts = recruiter.relevant_points(resume_text, job_text)
    points = "\n".join(f"- {p['quote']}  [{p['why']}]" for p in pts)
    for attempt in range(2):
        out = llm.chat_json(build_prompt(resume_text, name, profile_text, usc, usc_override, goal, resume.first_name(), feedback,
                                         style, points, length == "short"),
                            temperature=0.6, effort="low", label="drafting", max_tokens=6000)
        if not out or not out.get("note") or not out.get("message"):
            out, feedback = None, "Return the complete JSON with non-empty note and message."
            continue
        if len(out["note"]) < round(limit * 0.78) and attempt == 0:      # too short: one full retry asking for the specifics
            held = out
            feedback = (f'"note" is only {len(out["note"])} characters; use {round(limit * 0.85)} to {limit - 5} by adding the specific '
                        f"detail from their work and the sender's connecting project.")
            continue
        break
    out = out or held
    if not out:
        raise ValueError("The drafting model didn't return a usable draft. Try again.")
    out["note"], out["message"] = _tidy(out["note"]), _tidy(out["message"])
    issues = verify_issues(resume_text, profile_text, out["note"], out["message"], goal)
    if issues:                                   # one automatic repair, then re-check what's left
        fixed = repair(resume_text, profile_text, out["note"], out["message"], issues, goal)
        if fixed:
            out["note"], out["message"] = fixed
            issues = verify_issues(resume_text, profile_text, out["note"], out["message"], goal)
    if len(out["note"]) > limit:                 # lengths last: facts first, then squeeze to fit
        out["note"] = fit(out["note"], "connection note", limit) or out["note"]
    if len(out["message"]) > MESSAGE_MAX:
        out["message"] = fit(out["message"], "message", MESSAGE_MAX) or out["message"]

    warnings = list(out.get("warnings") or [])   # all warnings describe the FINAL text
    if len(out["note"]) > limit:
        warnings.append(f"The connection note is {len(out['note'])} characters; LinkedIn allows {limit}. Trim it before sending.")
    if len((profile_text or "").strip()) < 120:
        warnings.append("Very little profile text was provided, so the draft can only be general. Paste more of their profile for a specific one.")
    status = usc_override or usc["status"]
    final = (out["note"] + out["message"]).lower()
    if status == "yes" and "usc" not in final and "trojan" not in final:
        warnings.append("They look like a USC alum but the draft doesn't mention it; add a line or regenerate.")
    if llm.last_model and llm.last_model != config.GROQ_MODEL:
        warnings.insert(0, f"Drafted with a smaller backup model ({llm.last_model}) because the main model's daily limit is used up. "
                           "Read this one more carefully, or regenerate tomorrow.")
    warnings += _format_issues(issues)
    return {"recipient": out.get("recipient") or {}, "usc": {**usc, "used": status}, "hooks": out.get("hooks") or [],
            "note": _tidy(out["note"]), "message": _tidy(out["message"]), "warnings": warnings,
            "limits": {"note_max": note_max()}, "points": pts, "options": {"tone": tone, "length": length}}
