"""Turn one email (subject, sender, body) into a structured application event.

Cheap deterministic rules run first (ATS sender domains, subject templates, status phrases).
The LLM is only called when the rules can't produce company + role + status, and the whole
thing still works (flagging rows for review) when no API key is configured.
"""
import json
import re
import time
from dataclasses import dataclass, field
from email.utils import parseaddr

from bs4 import BeautifulSoup

from . import config

# ---------------------------------------------------------------- sender handling

BLOCKED_DOMAINS = (
    "lensa.com", "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "handshake.com", "joinhandshake.com", "business.amazon.com", "datacamp.com",
    "fireflies.ai", "extern.com", "jobright.ai", "jobrightai.com", "simplify.jobs", "ripplematch.co",
    # job-posting / recommendation feeds (they email *postings*, not your applications)
    "wellfound.com", "angel.co", "otta.com", "welcometothejungle.com", "builtin.com", "dice.com",
    "monster.com", "careerbuilder.com", "hiring.cafe", "levels.fyi", "wayup.com", "talent.com",
    "adzuna.com", "jooble.org", "snagajob.com", "joinhandshake.com", "nodesk.co", "weworkremotely.com",
    # communities / programs that send newsletters and opportunity blasts
    "rewritingthecode",
)
ATS_DOMAINS = (
    "greenhouse-mail.io", "greenhouse.io", "myworkday.com", "icims.com", "lever.co",
    "ashbyhq.com", "smartrecruiters.com", "jobvite.com", "taleo.net", "successfactors",
    "workable.com", "breezy.hr", "bamboohr.com", "recruitee.com", "hirevue.com", "ultipro.com", "ukg.com",
    "dayforcehcm.com", "paylocity.com", "adp.com", "oraclecloud.com", "eightfold.ai", "workday.com", "jobalerts.career",
)
# assessment/scheduling platforms: they send on behalf of the employer, so their name is never the company
PLATFORM_DOMAINS = ("hackerrank", "codesignal.com", "codility.com", "hirevue.com", "calendly.com",
                    "goodtime.io", "modernhire.com", "karat.com", "testgorilla.com", "criteriacorp.com",
                    "arcticshores.com", "pymetrics.com", "shl.com", "plum.io", "vervoe.com", "harver.com",
                    "cappfinity.com", "mettl.com", "glider.ai", "coderpad.io", "qualified.io", "devskiller.com", "candidate.fyi")
_GENERIC_NAMES = re.compile(
    r"\b(talent acquisition|talent|recruiting|recruitment|recruiter|careers?|hiring team|hiring|"
    r"human resources|hr|no[- ]?reply|do not reply|noreply|notifications?|workday|myworkday|"
    r"greenhouse|team|jobs|support|assessments?|early career|emerging talent|student programs|rs)\b",
    re.I,
)


def _sender_parts(sender: str) -> tuple[str, str]:
    name, addr = parseaddr(sender)
    return name.strip(), addr.strip().lower()


def _domain(addr: str) -> str:
    return addr.split("@")[-1]


def blockable_pattern(sender: str) -> str | None:
    """The domain to block for "delete & block sender", or None when the domain is shared by many
    employers (ATS, assessment platforms, free mail), where a block would hide real applications."""
    dom = _domain(_sender_parts(sender)[1])
    if not dom or any(d in dom for d in (*ATS_DOMAINS, *PLATFORM_DOMAINS, *_GENERIC_MAIL, "linkedin.com", "indeed.com")):
        return None
    return dom


def is_ats(sender: str) -> bool:
    return any(d in _domain(_sender_parts(sender)[1]) for d in ATS_DOMAINS)


def is_blocked(sender: str, subject: str, extra: tuple = ()) -> bool:
    _, addr = _sender_parts(sender)
    dom = _domain(addr)
    # LinkedIn / Indeed *do* send real application confirmations; everything else from them is noise
    if "linkedin.com" in dom and re.search(r"your application was sent", subject, re.I):
        return False
    if addr.startswith("indeedapply@"):
        return False
    return any(d in dom for d in BLOCKED_DOMAINS) or any(x in addr for x in (*config.EXTRA_BLOCKED_SENDERS, *extra))


# ---------------------------------------------------------------- text helpers

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "head"]):
        t.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[​-‏⁠­͏﻿]", "", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def clean_body(text: str, limit: int = 3500) -> str:
    text = re.sub(r"https?://\S+", "", text)           # tracking URLs eat the budget
    text = re.sub(r"[​-‏⁠­͏﻿]", "", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit]


# ---------------------------------------------------------------- rules

_STRONG_REJECT = re.compile(
    r"(not (to )?(be )?(move|moving|proceed|proceeding|go|going|advance|advancing|continue|continuing) (forward|further|with your)|"
    r"difficult decision|decision not to|will not be (moving|proceeding)|won'?t be (moving|proceeding)|"
    r"decided to (move|go|proceed|pursue) (forward )?with (other|another)|pursu\w+ other (candidates|applicants)|"
    r"position (has been|was|is) (now )?(filled|closed)|no longer (being )?consider|not (been )?selected|"
    r"not a (good )?(match|fit)|we regret|decided not to (move|proceed|pursue|advance)|"
    r"will not (be )?(advance|advancing|continu)|not (be )?(advance|advancing) (your|you)|"
    r"(only able to|unable to|not able to|will not be able to) consider|(cannot|can.?t) consider|unable to (offer|move forward|proceed)|(not|n't) (be )?able to (move|proceed) forward|"
    r"decided to (move|go|proceed|pursue)( forward)? with (candidates|applicants|those|a candidate)|"
    r"(move|moving) forward with (candidates|applicants)|(closer|better) (fit|match))",
    re.I,
)
_WEAK_REJECT = re.compile(r"(unfortunately|other candidates|other applicants|after (careful|further|thorough) (consideration|review))", re.I)
_OFFER = re.compile(r"(pleased to (offer|extend)|offer letter|offer of employment|extend (you )?an offer|formal offer)", re.I)
_OA = re.compile(
    r"(online assessment|coding (challenge|assessment|test)|hackerrank|codesignal|codility|screening (process|test|round|assessment|questionnaire)|"
    r"(complete|take) (the|this|your) (following )?assessment|take[- ]home|hirevue|technical assessment)", re.I)
_INTERVIEW = re.compile(
    r"(schedule (an?|your|the) [\w\- ]{0,25}interview|invit\w+ you to (an? )?(interview|a call|speak)|"
    r"interview (invitation|confirmation|scheduled|availability)|phone screen|next round|"
    r"(book|select) a time|calendly)", re.I)
_APPLIED = re.compile(
    r"(thank(s| you) (you )?for (applying|your application|submitting|your (online )?submission)|"
    r"(application|resume) (has been |was )?(successfully )?(received|submitted)|"
    r"we('ve| have) received your (\w+ ){0,3}(application|resume)|received your (\w+ ){0,3}application|"
    r"application (confirmation|received)|your application (was sent|to .{1,80} (was|has been) (sent|submitted|received))|"
    r"thanks for applying|successfully (applied|submitted)|applying to)", re.I)
# words that show up in decisions but not in plain receipts; seeing them next to an "Applied" guess means "ask the LLM"
_OUTCOME_HINT = re.compile(
    r"(decision|candidacy|careful (review|consideration)|move forward|moving forward|not (to )?(proceed|advance|continue)|"
    r"other candidates|regret|unfortunately|no longer|not selected)", re.I)
_APPLIED_SUBJECT = re.compile(r"\byour application (for|to|with)\b|\bwe('ve| have) got (it|your (application|resume))\b", re.I)
# a "still under consideration" holding email is not a rejection
_HOLD = re.compile(r"(still under consideration|await to share next steps|remain under review)", re.I)


_ACTION = re.compile(
    r"(additional info(rmation)?\s+(is\s+)?(needed|required|requested)|(need|require|request)(s|ed|ing)?\s+(some\s+|an?\s+)?(additional|more|further)\s+(info|documentation|documents)|"
    r"action\s+(is\s+)?(required|needed)|(application|profile)\s+(is\s+)?(incomplete|not complete)|incomplete\s+application|"
    r"to complete your application|finish your application|"
    r"(please\s+)?(complete|fill out|submit|finish)\s+(the|this|your|our)\s+(\w+\s+){0,2}(form|questionnaire|survey|profile|application|self[- ]identification)|"
    r"(please\s+)?(verify|confirm)\s+your\s+e-?mail|"
    r"(please\s+)?(upload|provide|submit|send)\s+(your|the|a)\s+(\w+\s+){0,2}(transcripts?|references?|resume|cv|documents?|work authorization|passport|portfolio)|"
    r"self[- ]identification|voluntary self|background check (form|authorization)|we (still )?need (you to|some|additional|more)|"
    r"(confirm|verify) your candidate)", re.I)
# informational mentions in receipts ("check your status in the candidate portal"), so these only count when the subject isn't a receipt
_ACTION_PORTAL = re.compile(r"(confirm|verify) your identity|candidate (portal|account)|magic link", re.I)
_ACTION_HEDGE = re.compile(
    r"(\bif\b|\bshould\b|\bin case\b|\bwhenever\b)[^.]{0,80}\b(need|require|request|necessary|additional)|\bmay (contact|reach|request|ask)|\bmight (need|contact|ask)|"
    r"\bwill (contact|reach out|be in touch|notify)\b", re.I)


_JOB_CONTEXT = re.compile(
    r"(\b(your|job|the)\s+application\b|\bapplication\s*:|\bcandidate\b|\bapplicant\b|\brecruit(er|ment|ing)?\b|\bhiring\b|"
    r"talent (acquisition|team)|\bcareers?\b|\bposition\b|\brequisition\b|\bresume\b|\bcv\b|\bjob (opening|posting|opportunity)\b|\binterview\b)", re.I)


def action_needed(subject: str, body: str, sender: str = "") -> bool:
    """They're asking you for something (a form, documents, verification). A subject match is strong; in the body only an
    un-hedged sentence near the top counts, so "if we need more information we'll contact you" doesn't."""
    if re.search(r"password|welcome to", subject, re.I):
        return False                 # account housekeeping (password resets, welcome emails) is not a to-do for an application
    # "verify your email" / "action required" are everywhere (banks, cloud consoles, newsletters): it only counts as an
    # application to-do when the email is about a job application, or comes from a recruiting system
    if not (is_ats(sender) or _JOB_CONTEXT.search(f"{subject}\n{body[:1500]}")):
        return False
    receipt = is_receipt_subject(subject)
    patterns = (_ACTION,) if receipt else (_ACTION, _ACTION_PORTAL)
    if any(p.search(subject) for p in patterns):
        return True
    for sent in re.split(r"(?<=[.!?])\s+|\n+", body[:1500]):
        if any(p.search(sent) for p in patterns) and not _ACTION_HEDGE.search(sent):
            return True
    return False


def action_summary(subject: str, body: str) -> str:
    """One line on what they're asking for: the sentence that triggered it, else the subject."""
    for sent in re.split(r"(?<=[.!?])\s+|\n+", re.sub(r"https?://\S+", "", body[:1500])):
        if _ACTION.search(sent) and not _ACTION_HEDGE.search(sent) and 15 < len(sent.strip()) < 220:
            return " ".join(sent.split())[:200]
    return " ".join(subject.split())[:200]


def is_receipt_subject(subject: str) -> bool:
    """True for genuine application-receipt subjects ("Thank you for applying…"), not reminders/updates."""
    return (bool(_APPLIED.search(subject)) or bool(_APPLIED_SUBJECT.search(subject))) and not re.search(r"reminder|verify|incomplete|update", subject, re.I)


_HEDGE = re.compile(r"\b(if|should|in case|once|whenever|unless|provided that|may|might|who (move|moves|are moved|advance)|"
                    r"(is|are) selected|selected for|not selected)\b", re.I)
_CLAUSE_SPLIT = re.compile(r"[.!?;\n]+|,\s+(?=(?:but |however |and )?(?:if|should|once|whenever)\b)|\s+but\s+|\s+however\s*,?\s+", re.I)


def _unhedged(pattern: re.Pattern, subject: str, body: str) -> bool:
    """The phrase counts if it is in the subject, or in a body clause that isn't conditional or hypothetical.
    Receipts are full of "if you are selected we may schedule an interview"; that is not an interview."""
    if pattern.search(subject):
        return True
    return any(pattern.search(c) and not _HEDGE.search(c) for c in _CLAUSE_SPLIT.split(body[:2500]))


def rule_status(subject: str, body: str, sender: str = "") -> str | None:
    head = f"{subject}\n{body[:2500]}"
    if _unhedged(_OFFER, subject, body):
        return "Offer"
    if _unhedged(_STRONG_REJECT, subject, body):
        return "Rejected"
    if _HOLD.search(head):
        return None
    if "hirevue.com" in sender.lower():
        return "OA"
    if _unhedged(_OA, subject, body):
        return "OA"
    if _unhedged(_INTERVIEW, subject, body):
        return "Interview"
    if action_needed(subject, body, sender):
        return "Action"
    if _APPLIED_SUBJECT.search(subject) and not re.search(r"update|status|decision|unfortunately", subject, re.I):
        return "Applied"             # the Greenhouse-style receipt: "Your application for <role> at <company>"
    if _APPLIED.search(subject) or _APPLIED.search(body[:800]):
        return "Applied"
    if _HOLD.search(head):
        return None
    if _unhedged(_WEAK_REJECT, subject, body):
        return "Rejected"
    return None


def _clean_company(name: str) -> str:
    name = re.sub(r"\s*@.*$", "", name)          # "Cotiviti @ icims" -> "Cotiviti"
    name = _GENERIC_NAMES.sub(" ", name)
    name = re.sub(r"\([^)]*\)", " ", name)
    name = re.sub(r"[|:–,]+|\s-\s", " ", name)
    return re.sub(r"\s+", " ", name).strip(" -.")


_SUBJECT_COMPANY = [
    r"^(?P<c>[^|:–]{2,40}?)\s*\|",                                                    # "Ripple | Update on..."
    r"^(?P<c>[A-Z][^|:–]{1,40}?) - (?i:update)",                                        # "Lightmatter - Update about..."
    r"(?i:update (?:on|from|about|regarding) (?:your )?)(?P<c>[A-Z][\w&.' -]{1,40}?)(?i: application| status| position)",
    r"^(?:An )?(?i:update from) (?P<c>[A-Z][\w&.' ]{1,40})$",
    r"^(?P<c>[A-Z][\w&.,' -]{1,40}?) (?i:application update|interview update|talent acquisition|application status)",
    r"(?i:applic\w+ (?:to|at|with)) (?P<c>[A-Z][\w&.' ]{1,40}?)(?: for| -|,|!|:|$)",
    r"(?i:thank(?:s| you) for applying (?:to|at|with)) (?P<c>[A-Z][\w&.' ]{1,40}?)(?:!|\.|,| -|$)",
    r"(?i:your application was sent to) (?P<c>.+)$",
]
_ROLE_WORDS = re.compile(r"\b(intern|internship|engineer|analyst|developer|scientist|manager|position|role|opening|software|data)\b", re.I)
_GENERIC_MAIL = ("gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "icloud.com")


def _plausible_company(c: str) -> bool:
    return bool(c) and 1 < len(c) <= 45 and c[0].isalnum() and "@" not in c and not _ROLE_WORDS.search(c) and not re.match(r"^(your|the|an?|application|update|thank|thanks)\b", c, re.I)


def rule_company(subject: str, sender: str) -> str:
    name, addr = _sender_parts(sender)
    dom = _domain(addr)
    if "linkedin.com" in dom:
        m = re.search(r"your application was sent to (.+)$", subject, re.I)
        return m.group(1).strip() if m else ""
    for pat in _SUBJECT_COMPANY:
        m = re.search(pat, subject.strip())
        if m:
            c = _clean_company(m.group("c"))
            if _plausible_company(c):
                return c
    cand = "" if any(d in dom for d in PLATFORM_DOMAINS) else _clean_company(name)
    looks_like_person = bool(re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", cand))
    if _plausible_company(cand) and not re.search(r"\d", cand) and not looks_like_person:
        return cand
    # last resort: the sender's own domain (skip ATS / assessment platforms / free-mail hosts)
    if not is_ats(sender) and not any(g in dom for g in _GENERIC_MAIL + PLATFORM_DOMAINS):
        parts = dom.split(".")
        sld = parts[-2] if len(parts) >= 2 else ""
        if sld and sld not in ("edu", "co") and len(sld) > 2:
            return sld.upper() if len(sld) <= 3 else sld.title()      # imc.com -> "IMC", but stripe.com -> "Stripe"
    return ""


_ROLE_PATTERNS = [
    r"for (?:the )?(?P<r>[^|]{5,90}?) (?:position|role|opportunity)\b",
    r"applying to (?:the )?(?P<r>[^|]{5,90}?) (?:position|role)\b",
    r"application (?:to|for) (?:the )?(?P<r>[^|!]{5,90}?)(?: at | with | - |!|$)",
    r"application update\s*[:\-–]\s*(?P<r>.{5,90})$",
    r"indeed application:\s*(?P<r>.{3,90})$",
    r"your (?P<r>[^|]{5,70}?) application\b",
]


def clean_role(role: str, company: str = "") -> str:
    role = re.sub(r"^(applying (to|for)|application (to|for)|thank you for applying (to|for))\s+(the\s+)?", "", role.strip(), flags=re.I)
    role = re.sub(r"[,\s]*\d{4,}$", "", role).strip(" -–:,.")        # trailing req number: "Software Engineer,2132"
    c = company.lower().strip()
    if c and role.lower() in (c, f"{c} job", f"{c} role", f"{c} position", f"{c} application"):
        return ""
    if re.fullmatch(r"(?i)(\w+ )?(job|application|position|role)", role):
        return ""
    return role


def rule_role(subject: str) -> str:
    for pat in _ROLE_PATTERNS:
        m = re.search(pat, subject, re.I)
        if m:
            r = re.sub(r"\s*\((?:req|job)[^)]*\)", "", m.group("r")).strip(" -–:,.")
            if r and not re.match(r"^(your|the|this)$", r, re.I):
                return r
    return ""


def find_req_id(text: str) -> str:
    m = re.search(r"\b(?:req(?:uisition)?(?:\s*id)?|job\s*(?:id|#|number))\s*[:#]?\s*([A-Za-z]?\d{4,})", text, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(R-?\d{5,})\b", text)
    return m.group(1).upper() if m else ""


def guess_job_type(*texts: str) -> str:
    t = " ".join(texts).lower()
    if re.search(r"\bco-?op\b", t):
        return "Co-op"
    if re.search(r"new[- ]?grad|new graduate|university grad|early career|recent grad|"
                 r"graduate (software|developer|engineer|programme|program|scheme|analyst|systems|technology)|"
                 r"graduate .{0,25}engineer", t):
        return "New Grad"
    if re.search(r"\bintern(ship)?\b", t):
        return "Internship"
    return ""


# any hint of recruiting language; without one, the LLM can't add anything and we skip the call (saves quota)
_JOB_SIGNAL = re.compile(
    r"(applica|applied|applying|candidate|candidacy|interview|assessment|recruit|hiring|position|job|career|"
    r"resume|offer letter|internship|co-?op|new grad|graduate program|screening|talent|requisition|\breq\b)", re.I)


# ---------------------------------------------------------------- LLM fallback

_PROMPT = """You parse emails sent to a job seeker about their job applications (internships, co-ops,
new-grad and full-time roles). Extract structured info.

Email:
- Subject: {subject}
- From: {sender}
- Body:
{body}

Rules:
- "company" = the HIRING company (not the ATS such as Greenhouse/Workday/iCIMS, not a job board, not an assessment
  platform such as HackerRank/CodeSignal/HireVue: for those, use the employer that requested the assessment).
  Never use a person's name as the company. Use the company's common name, e.g. "University of Pittsburgh".
- "role" = the job title, e.g. "Software Engineer, New Grad". Empty string if absent.
- "status": exactly one of
  Applied   - application received / thank you for applying / confirmation
  OA        - online assessment, coding challenge, HackerRank/CodeSignal/HireVue invite
  Interview - invitation to schedule, or confirmation of, any interview or recruiter call
  Action    - the company needs something from the candidate: an additional-information form, documents, profile completion,
              candidate-account or email verification for an application, a work-authorization or self-identification form (NOT a plain receipt that merely says it may ask later)
  Rejected  - not moving forward, position filled, other candidates selected, etc.
  Offer     - job offer
  None      - cannot be determined (e.g. generic "we have an update" with no outcome)
- is_job_related=false for job postings or job recommendations sent to the user (Wellfound, Otta, "new roles for you"),
  community/program/club newsletters (e.g. Rewriting the Code, hackathons, scholarships), job-board digests, marketing, newsletters, networking, school/admin mail,
  or anything that is not about a specific application the user submitted.
- A message saying the candidate is "still under consideration" is status None, not Rejected.

Return ONLY JSON: {{"is_job_related": bool, "company": str, "role": str, "status": "Applied|OA|Interview|Rejected|Offer|Action|None"}}"""

from . import llm                      # noqa: E402  (shared Groq helper)
from .llm import LLMExhausted          # noqa: E402,F401  (re-exported: sync.py imports it from here)


def llm_available() -> bool:
    return llm.available()


def llm_parse(subject: str, sender: str, body: str) -> dict | None:
    """Parsed email fields, None on a non-quota failure (caller falls back to rules); raises LLMExhausted
    when no model has daily quota left."""
    return llm.chat_json(_PROMPT.format(subject=subject, sender=sender, body=clean_body(body, 2200)), label="parser")


# ---------------------------------------------------------------- public entry point

@dataclass
class Parsed:
    is_job: bool
    company: str = ""
    role: str = ""
    status: str | None = None
    req_id: str = ""
    job_type: str = ""
    via: str = "rules"
    notes: list[str] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return bool(self.company and self.status)


def parse_email(subject: str, sender: str, body: str, use_llm: bool = True, extra_blocked: tuple = ()) -> Parsed:
    if is_blocked(sender, subject, extra_blocked):
        return Parsed(is_job=False, via="blocklist")

    status = rule_status(subject, body, sender)
    company = rule_company(subject, sender)
    role = rule_role(subject)
    ats = is_ats(sender)
    update_subject = bool(re.search(
        r"application (update|status)|update (on|about|regarding|for) (your|the|our)? ?(application|position|candidacy)|"
        r"update on position|an update (from|on)", subject, re.I))
    p = Parsed(
        is_job=bool(status) or (ats and bool(company)) or (update_subject and bool(company)),
        company=company, role=role, status=status,
        req_id=find_req_id(f"{subject}\n{body[:1500]}"),
    )

    if not p.is_job and not _JOB_SIGNAL.search(f"{subject}\n{body[:1500]}"):
        return p   # nothing recruiting-related anywhere: don't spend an LLM call on it
    doubtful_applied = status == "Applied" and bool(_OUTCOME_HINT.search(f"{subject}\n{body[:2500]}"))
    if doubtful_applied and not (use_llm and llm_available()):
        p.status = None   # without the LLM, don't guess "Applied" for something that reads like a decision
    if use_llm and llm_available() and (doubtful_applied or not (status and company and role)):
        out = llm_parse(subject, sender, body)
        if out:
            p.via = "rules+llm"
            if not out.get("is_job_related") and status != "Action":      # (an Action found by a rule already passed the job-context check)
                p.is_job = False
                return p
            p.is_job = True
            # rules win on status when they fired, except the weak "Applied" guess, which the LLM may overrule
            llm_status = out.get("status") if out.get("status") in config.EMAIL_STATUSES else None
            if p.status in (None, "Applied"):
                p.status = llm_status or p.status
            p.company = out.get("company") or p.company
            p.role = p.role or out.get("role", "")

    sender_name = _sender_parts(sender)[0]
    if p.company and not ats and p.company.lower() == sender_name.lower() and re.fullmatch(r"[A-Z][\w.'-]+( [A-Z][\w.'-]+){1,2}", p.company):
        # the "company" is just the sender's personal name (a recruiter replying in a thread): can't trust it
        p.company, p.is_job = "", False
    if p.company and (p.company.startswith("@") or "@" in p.company):
        p.company = ""
    p.role = clean_role(p.role, p.company)
    p.job_type = guess_job_type(subject, p.role)
    return p
