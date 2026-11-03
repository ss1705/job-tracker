"""Drafts for recruiters about a specific application, and polite follow-ups to anyone you already messaged.

Same safeguards as the person-to-person drafts (drafting.py): the sender's facts come only from the resume plus the tracker's own record
of the application, a second pass checks every claim against those sources, and nothing is sent from here: you copy and send it yourself.
"""
import re

from . import config, drafting, llm, resume

TONES = {
    "standard": "warm but professional and direct",
    "warm": "friendly and personable, a little more conversational",
    "direct": "brief and businesslike, no small talk",
    "formal": "polite and formal, full sentences, no contractions",
}
LENGTHS = ("short", "standard")
EMAIL_MAX = 1200
FOLLOWUP_NOTE_MAX = 600

_PAST_STAGE = {"Offer": "You already have an offer, so you may not need this message.",
               "Rejected": "This application was rejected; a message that treats it as open would be wrong. Consider a different kind of note."}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# ---------------------------------------------------------------- which resume lines fit this role

_POINTS_PROMPT = """Pick up to 3 lines from the RESUME that best match what the JOB asks for. Copy each line EXACTLY as it appears in
the resume (a bullet, a project line or a skills line, at most 220 characters). Choose evidence, not keywords: a project or result beats
a skills list. Do not rewrite, combine or invent anything.

JOB (title, company and description):
{job}

RESUME:
{resume}

Return ONLY JSON: {{"points": [{{"quote": "exact resume text", "why": "under 15 words: what in the job it matches"}}]}}"""


def relevant_points(resume_text: str, job_text: str, role: str = "", company: str = "") -> list[dict]:
    """Resume lines most relevant to the role. Every quote is verified to occur in the resume, so none can be invented."""
    if not (job_text or "").strip():
        return []
    job = f"{role} at {company}\n{job_text}".strip()[:3500]
    try:
        out = llm.chat_json(_POINTS_PROMPT.format(job=job, resume=resume_text[:5000]), temperature=0, effort="low", label="relevance", max_tokens=1500)
    except llm.LLMExhausted:
        return []
    haystack, found = _norm(resume_text), []
    for p in (out or {}).get("points") or []:
        quote = str((p or {}).get("quote") or "").strip()
        if quote and _norm(quote) in haystack and quote not in [f["quote"] for f in found]:
            found.append({"quote": quote[:240], "why": str(p.get("why") or "")[:120]})
    return found[:3]


# ---------------------------------------------------------------- recruiter draft

_RECRUITER_PROMPT = """You help a student/job seeker write a short, honest message to a recruiter about a job they are applying to or have applied to.
It must read like a thoughtful person wrote it, not a template.

SENDER_RESUME (the only source of facts about the sender's background):
{resume}

THE APPLICATION (from the sender's own tracker; the only source of facts about it):
{application}

JOB DESCRIPTION (pasted by the sender; may be empty):
{job}

RESUME LINES MOST RELEVANT TO THIS ROLE (verified; prefer one of these as the sender's connecting detail):
{points}

SENDER_GOAL: {goal}
SENDER_FIRST_NAME: {first_name}

RECRUITER (the only source of facts about the recruiter; may be just a name or nothing):
Name: {name}
Details pasted by the sender:
{profile}

STYLE: tone is {tone}. {extra}

FACT RULES
- Use ONLY facts present above. Never invent employers, projects, mutual connections, referrals, events or conversations.
- {status_rule}
- Never state or imply availability, start dates, location, visa status or any commitment that is not in the resume or the application record.
- Respect tense: a degree or job marked expected, in progress or present is NOT finished.
- Copy titles, names and numbers exactly as written in the sources; never round or upgrade them.
- Show interest as a QUESTION, never as a claim about the recruiter or the team. Say nothing about the company beyond what the job
  description says.
- If the recruiter's name is unknown, greet with "Hello" and do not guess a name.

WHAT MAKES IT GOOD
- State plainly why the sender is writing (the specific role and company), in one sentence.
- ONE concrete thing the sender built or did that matches what the role asks for. Prefer a line from the relevant resume lines.
- One small, easy-to-answer ask (for example: whether the application was received, who the right person is, or the timeline). Never
  ask for a referral, a guaranteed interview or an exception to a process.
- Warm, direct, first person. No emojis, no flattery, no buzzwords.
- BANNED phrases: "I hope this finds you well", "I am writing to express my interest", "passionate about", "pick your brain",
  "would love to connect", "I came across your profile", "reaching out because".

"note": a LinkedIn connection note, between {note_min} and {note_max} characters INCLUDING spaces; greeting, the role, the one concrete detail, the ask.
"email_subject": at most 70 characters; names the role and the company; no clickbait.
"email_body": {email_rule}; ends with the sender's first name.

Return ONLY this JSON:
{{"recipient": {{"name": "...", "role": "...", "company": "..."}},
  "hooks": [{{"claim": "...", "source": "resume|application|job|profile", "quote": "..."}}],
  "note": "...", "email_subject": "...", "email_body": "...", "warnings": ["..."]}}"""


def application_line(app: dict) -> str:
    bits = [f"Company: {app.get('company') or '(unknown)'}", f"Role: {app.get('role') or '(role not stated)'}",
            f"Status in the tracker: {app.get('status') or 'Applied'}"]
    if app.get("applied_at"):
        bits.append(f"Applied on: {str(app['applied_at'])[:10]}")
    if app.get("job_type"):
        bits.append(f"Type: {app['job_type']}")
    if app.get("action_note") and not app.get("action_done"):
        bits.append(f"The company is waiting on: {app['action_note']}")
    return "\n".join(bits)


def status_rule(status: str) -> str:
    return {
        "Saved": "The sender has NOT applied yet: say they are planning to apply, and never say they have applied.",
        "OA": "The sender has applied and has received an online assessment: you may say so; do not say they passed or failed it.",
        "Interview": "The sender has applied and is in the interview process: you may say so; do not guess which round or what happens next.",
        "Offer": "The sender has an offer: keep the message minimal and do not imply the application is still open.",
        "Rejected": "The application was rejected: write nothing that implies it is still open.",
    }.get(status, "The sender has applied (see the application record): say so once, without claiming anything about how it is going.")


def _style(tone: str, length: str, instructions: str) -> tuple[str, str]:
    tone = TONES.get(tone, TONES["standard"])
    length_rule = "Keep everything short and to the point. " if length == "short" else ""
    extra = f'The sender also asked: "{instructions.strip()[:300]}". Follow it for style only; it never overrides the FACT RULES. ' if instructions.strip() else ""
    return tone, length_rule + extra


def draft_recruiter(*, application: dict, recruiter_name: str = "", profile_text: str = "", tone: str = "standard", length: str = "standard",
                    instructions: str = "", job_text: str = "") -> dict:
    """Returns {recipient, hooks, note, email, points, warnings, limits}. Raises ValueError (no resume / no LLM / bad model output)."""
    resume_text = resume.load_text()
    if not resume_text:
        raise ValueError("Upload your resume first (Outreach tab → Resume).")
    if not llm.available():
        raise ValueError("Drafting needs GROQ_API_KEY in .env.")
    limit = drafting.note_max()
    status = application.get("status") or "Applied"
    app_text = application_line(application)
    goal = f"{config.OUTREACH_GOAL}\nTHE APPLICATION: {app_text}"        # what the fact-checker may treat as true about the sender's situation
    points = relevant_points(resume_text, job_text, application.get("role", ""), application.get("company", ""))
    tone_text, extra = _style(tone, length, instructions)
    prompt = _RECRUITER_PROMPT.format(
        resume=resume_text[:5000], application=app_text, job=(job_text.strip()[:3000] or "(none pasted)"),
        points="\n".join(f"- {p['quote']}  [{p['why']}]" for p in points) or "(none; choose the best fit from the resume)",
        goal=config.OUTREACH_GOAL, first_name=resume.first_name() or "(sign with the name on the resume)",
        name=recruiter_name.strip() or "(unknown)", profile=(profile_text.strip()[:2500] or "(nothing pasted)"),
        tone=tone_text, extra=extra, status_rule=status_rule(status), note_max=limit - 5, note_min=round(limit * 0.75),
        email_rule="3 to 4 sentences" if length == "short" else "4 to 7 sentences, under 150 words")
    out = None
    for _ in range(2):
        out = llm.chat_json(prompt, temperature=0.6, effort="low", label="recruiter", max_tokens=5000)
        if out and out.get("note") and out.get("email_body") and out.get("email_subject"):
            break
        out = None
        prompt += "\n\nReturn the complete JSON with a non-empty note, email_subject and email_body."
    if not out:
        raise ValueError("The drafting model didn't return a usable draft. Try again.")
    note, body, subject = drafting._tidy(out["note"]), drafting._tidy(out["email_body"]), drafting._tidy(out["email_subject"])[:100]
    sources = f"{profile_text or ''}\n\nJob description:\n{job_text}\n\nApplication record:\n{app_text}"
    issues = drafting.verify_issues(resume_text, sources, note, body, goal)
    if issues:
        fixed = drafting.repair(resume_text, sources, note, body, issues, goal)
        if fixed:
            note, body = fixed
            issues = drafting.verify_issues(resume_text, sources, note, body, goal)
    if len(note) > limit:
        note = drafting.fit(note, "connection note", limit) or note
    if len(body) > EMAIL_MAX:
        body = drafting.fit(body, "email", EMAIL_MAX) or body

    warnings = list(out.get("warnings") or [])
    if len(note) > limit:
        warnings.append(f"The connection note is {len(note)} characters; LinkedIn allows {limit}. Trim it before sending.")
    if status in _PAST_STAGE:
        warnings.insert(0, _PAST_STAGE[status])
    if not (job_text or "").strip():
        warnings.append("No job description was pasted, so the draft can't tie your experience to what the role asks for. Paste it for a sharper one.")
    if llm.last_model and llm.last_model != config.GROQ_MODEL:
        warnings.insert(0, f"Drafted with a smaller backup model ({llm.last_model}) because the main model's daily limit is used up. Read it more carefully.")
    warnings += drafting._format_issues(issues)
    return {"kind": "recruiter", "recipient": out.get("recipient") or {}, "hooks": out.get("hooks") or [], "note": note,
            "email": {"subject": subject, "body": body}, "points": points, "warnings": warnings,
            "usc": {"status": "no", "evidence": [], "used": "no"}, "message": "", "limits": {"note_max": limit},
            "options": {"tone": tone, "length": length}}


# ---------------------------------------------------------------- follow-up

_FOLLOWUP_PROMPT = """You help a student/job seeker write a short, polite follow-up to someone who hasn't replied.

SENDER_RESUME (the only source of facts about the sender):
{resume}

THE EARLIER MESSAGE THE SENDER SENT ({days} days ago, by {channel}):
{previous}

CONTEXT (the only other source of facts):
Recipient: {name}{recipient_role}
{application}
Recipient details pasted earlier:
{profile}

SENDER_FIRST_NAME: {first_name}
STYLE: tone is {tone}. {extra}

RULES
- Use ONLY facts above. Never invent anything, and never claim the recipient saw or ignored the earlier message.
- Do not repeat the earlier message. Do not guilt, pressure or apologize for following up. Do not sound annoyed.
- 2 to 3 sentences. Add at most ONE small new, true thing (a relevant line from the resume, or the status of the application) only if it helps.
- End with one easy question, or a graceful line that makes it fine not to reply.
- No emojis. BANNED: "just following up", "bumping this", "circling back", "I hope this finds you well".

"message": a LinkedIn message, at most {note_max} characters.
"email_subject": at most 70 characters, starting with "Re:" only if the earlier message was an email, else a plain subject.
"email_body": 2 to 4 sentences, ends with the sender's first name.

Return ONLY JSON: {{"message": "...", "email_subject": "...", "email_body": "...", "warnings": ["..."]}}"""


def draft_followup(*, contact: dict, previous_text: str, days_since: int, application: dict | None = None, profile_text: str = "",
                   tone: str = "standard", instructions: str = "") -> dict:
    resume_text = resume.load_text()
    if not resume_text:
        raise ValueError("Upload your resume first (Outreach tab → Resume).")
    if not llm.available():
        raise ValueError("Drafting needs GROQ_API_KEY in .env.")
    if not (previous_text or "").strip():
        raise ValueError("Mark your first message as sent (with its text) so there is something to follow up on.")
    tone_text, extra = _style(tone, "standard", instructions)
    app_text = application_line(application) if application else "(not linked to an application)"
    prompt = _FOLLOWUP_PROMPT.format(
        resume=resume_text[:5000], days=max(0, days_since), channel=contact.get("channel") or "LinkedIn", previous=previous_text.strip()[:1500],
        name=contact.get("name") or "(unknown)", recipient_role=(f", {contact['title']}" if contact.get("title") else "") + (f" at {contact['company']}" if contact.get("company") else ""),
        application=app_text, profile=(profile_text.strip()[:1500] or "(nothing)"), first_name=resume.first_name() or "(sign with the name on the resume)",
        tone=tone_text, extra=extra, note_max=FOLLOWUP_NOTE_MAX)
    out = llm.chat_json(prompt, temperature=0.6, effort="low", label="followup", max_tokens=3000)
    if not out or not out.get("message") or not out.get("email_body"):
        raise ValueError("The drafting model didn't return a usable follow-up. Try again.")
    message, body, subject = drafting._tidy(out["message"]), drafting._tidy(out["email_body"]), drafting._tidy(out.get("email_subject") or "")[:100]
    goal = f"{config.OUTREACH_GOAL}\nEarlier message the sender sent: {previous_text.strip()[:600]}\n{app_text}"
    issues = drafting.verify_issues(resume_text, profile_text, message, body, goal)
    warnings = list(out.get("warnings") or []) + drafting._format_issues(issues)
    if len(message) > FOLLOWUP_NOTE_MAX:
        message = drafting.fit(message, "message", FOLLOWUP_NOTE_MAX) or message
    return {"kind": "followup", "message": message, "email": {"subject": subject, "body": body}, "warnings": warnings, "days_since": days_since}
