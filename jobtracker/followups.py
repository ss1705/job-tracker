"""Follow-up reminders and saved-posting deadlines (pure functions, easy to test).

Cold applications (no contact) never get automatic reminders: there's nobody to follow up with.
For applications with a contact (referral / coffee chat / recruiter), reminders fire for:
  * Applied with no response after FOLLOWUP_DAYS, until FOLLOWUP_MAX_DAYS (after that: ghosted, stop nagging)
  * OA / Interview with no new email for CHECKIN_DAYS
Any non-final application also reminds on its manual "remind me on" date.
Clicking "Followed up" resets the clock; a future reminder date snoozes the automatic ones.
"""
from datetime import date

from . import config

FINAL = ("Rejected", "Offer", "Withdrawn")


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def flags(app: dict, has_response: bool, last_event_local: str | None, today: date) -> dict:
    status = app["status"]
    applied, followed = _d(app.get("applied_at")), _d(app.get("followed_up_at"))
    remind, deadline = _d(app.get("follow_up_at")), _d(app.get("deadline"))
    out = {"follow_up_due": False, "follow_up_reason": "", "days_waiting": None, "deadline_days": None}

    if status == "Saved" and deadline:
        out["deadline_days"] = (deadline - today).days

    if status in FINAL:
        return out

    if remind:
        if remind <= today:
            out.update(follow_up_due=True, follow_up_reason=f"Reminder set for {remind:%b} {remind.day}")
        return out          # a set reminder date (future = snoozed) overrides the automatic rules

    if not app.get("contact_type"):
        return out          # cold application: only a manual reminder date can flag it (handled above)

    if status == "Applied" and not has_response and applied:
        ref = max(applied, followed) if followed else applied
        waited = (today - ref).days
        out["days_waiting"] = (today - applied).days
        if config.FOLLOWUP_DAYS <= waited <= config.FOLLOWUP_MAX_DAYS:
            what = "Followed up" if followed and followed >= applied else "Applied"
            out.update(follow_up_due=True, follow_up_reason=f"{what} {waited} days ago, no response")
    elif status in ("OA", "Interview"):
        ref = max(d for d in (_d(last_event_local), followed, applied) if d) if any((last_event_local, followed, applied)) else None
        if ref and (today - ref).days >= config.CHECKIN_DAYS:
            out.update(follow_up_due=True, follow_up_reason=f"{status}: no update for {(today - ref).days} days, check in")
    return out
