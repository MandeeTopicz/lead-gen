"""The dated outreach sequence and the automatic checks every draft must pass. Pure functions.

Sequence (PRD defaults, counts and spacing from icp.yaml): 10 LinkedIn touches at 2 per week with a new angle
each, then email and calls only when business contact info exists. The whole thing stops the moment the lead
replies; that's tracked by you in the review screen, not here.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from pipeline.config import Config, SenderConfig

LINKEDIN_ANGLES = [
    "Strongest hook: a recent post or timing trigger",
    "Why you, why now, one-line intro",
    "Their recent post: a real reaction or question",
    "Timing trigger: hiring, growth, or news",
    "Affinity: shared connection, employer, school, or community",
    "Something useful, no ask: an insight or resource for their segment",
    "{meet}",
    "A short question asking for their opinion",
    "Relevant proof: a result for a similar logistics company",
    "Polite close-the-loop",
]
EMAIL_ANGLES = ["Standalone intro that mentions LinkedIn, with one clear ask", "Short follow-up with a new angle"]
CALL_ANGLES = ["Call opener and voicemail, using the talking points", "Second call: opener and voicemail, new angle"]

LIMITS = {
    "linkedin_connect": ("chars", 300),
    "linkedin_message": ("chars", 500),
    "inmail": ("chars", 500),
    "email": ("words", 120),
    "call": ("sentences", 3),
}
EMAIL_SUBJECT_WORDS = 6
PLACEHOLDER = re.compile(r"\[[^\]]{1,40}\]|\{[^}]{1,40}\}|<[^>]{1,40}>|\bTODO\b|\bXX+\b|lorem ipsum", re.I)


@dataclass
class PlannedStep:
    code: str
    channel: str
    planned_date: date
    angle: str

    @property
    def limit(self) -> str:
        unit, n = LIMITS[self.channel]
        if self.channel == "email":
            return f"body ≤ {n} words, subject ≤ {EMAIL_SUBJECT_WORDS} words"
        if self.channel == "call":
            return f"opener ≤ {n} sentences, then a short voicemail"
        return f"≤ {n} characters"


def plan_sequence(start: date, config: Config, *, connected: bool, local: bool, has_email: bool,
                  has_phone: bool) -> list[PlannedStep]:
    seq = config.icp.sequence
    steps: list[PlannedStep] = []
    meet = "Coffee in Austin" if local else "A 15-minute call"
    # L1 on day 0; L2 once the connection is accepted, or as an InMail on the fallback day; then two per week.
    days = [0, seq.linkedin.inmail_fallback_after_days]
    week = 7
    while len(days) < seq.linkedin.touches:
        days += [week, week + 3]
        week += 7
    for i in range(seq.linkedin.touches):
        if i == 0:
            channel = "linkedin_message" if connected else "linkedin_connect"
        elif i == 1 and not connected:
            channel = "inmail"  # sent as a message instead if the connection is accepted first
        else:
            channel = "linkedin_message"
        angle = LINKEDIN_ANGLES[i % len(LINKEDIN_ANGLES)].format(meet=meet)
        steps.append(PlannedStep(f"L{i + 1}", channel, _business_day(start + timedelta(days=days[i])), angle))

    follow_up_start = start + timedelta(weeks=seq.email.start_week - 1)
    if has_email:
        for i in range(seq.email.touches):
            steps.append(PlannedStep(f"E{i + 1}", "email", _business_day(follow_up_start + timedelta(weeks=i)),
                                     EMAIL_ANGLES[i % len(EMAIL_ANGLES)]))
    call_start = start + timedelta(weeks=seq.calls.start_week - 1, days=1)
    if has_phone:
        for i in range(seq.calls.touches):
            steps.append(PlannedStep(f"C{i + 1}", "call", _business_day(call_start + timedelta(weeks=i)),
                                     CALL_ANGLES[i % len(CALL_ANGLES)]))
    return steps


def next_business_day(today: date) -> date:
    return _business_day(today + timedelta(days=1))


def check_draft(step: PlannedStep, subject: str | None, body: str, cited: list[int], valid_ids: set[int]) -> list[str]:
    """Problems with one draft. Empty means it passes."""
    problems = []
    unit, limit = LIMITS[step.channel]
    text = call_opener(body) if step.channel == "call" else body
    size = {"chars": len(text), "words": len(text.split()), "sentences": _sentences(text)}[unit]
    if size > limit:
        problems.append(f"{step.code}: {size} {unit}, limit {limit}")
    if step.channel == "email":
        if not subject:
            problems.append(f"{step.code}: email has no subject")
        elif len(subject.split()) > EMAIL_SUBJECT_WORDS:
            problems.append(f"{step.code}: subject is {len(subject.split())} words, limit {EMAIL_SUBJECT_WORDS}")
    for match in PLACEHOLDER.finditer(" ".join(filter(None, [subject, body]))):
        problems.append(f"{step.code}: unfilled placeholder {match.group(0)!r}")
    if not cited:
        problems.append(f"{step.code}: cites no evidence")
    unknown = [i for i in cited if i not in valid_ids]
    if unknown:
        problems.append(f"{step.code}: cites evidence that doesn't exist or isn't usable: {unknown}")
    return problems


def check_angles(angles: dict[str, str]) -> list[str]:
    seen: dict[str, str] = {}
    problems = []
    for code, angle in angles.items():
        key = " ".join(angle.lower().split())
        if key in seen:
            problems.append(f"{code}: repeats {seen[key]}'s angle")
        seen.setdefault(key, code)
    return problems


def email_footer(sender: SenderConfig) -> str:
    """CAN-SPAM: identify the sender, give a physical address, and a way to opt out. Added in code, never
    left to the model."""
    who = ", ".join(x for x in (sender.sender.name, sender.sender.title, sender.sender.company) if x)
    return f"\n\n--\n{who}\n{sender.sender.physical_address}\nNot relevant? Reply \"no thanks\" and I won't follow up."


def call_opener(body: str) -> str:
    """Call drafts are 'Opener: ...' then 'Voicemail: ...'; the length limit applies to the opener."""
    opener = re.split(r"\n\s*voicemail\s*:", body, flags=re.I)[0]
    return re.sub(r"^\s*opener\s*:\s*", "", opener, flags=re.I).strip()


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def _business_day(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day
