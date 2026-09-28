"""One dossier per digest lead: everything you need to decide and to reach out, with every claim sourced.

Sections follow the PRD: header, scores, why this lead, person, company, research, contact info, affinity,
talking points, the dated outreach plan with every touch drafted, and sources. Written as Markdown, DOCX, and PDF.
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlmodel import Session, col, select

from db.models import Dossier, Finding, OutreachStep, Post, Run
from pipeline.config import Config
from pipeline.context import Paths, RunContext
from pipeline.digest import Digest, DigestRow, build_digest, write_digest
from pipeline.documents import Block, Bullets, Heading, Para, Quote, Table, to_markdown, write_docx, write_pdf

CHANNEL_NAMES = {
    "linkedin_connect": "LinkedIn connection request",
    "linkedin_message": "LinkedIn message",
    "inmail": "InMail (or a message, if they've connected)",
    "email": "Email",
    "call": "Call",
}
RESEARCH_KINDS = ("platform", "talk", "article", "local_tie", "news", "hiring")
DEMO_BANNER = "**Demo sender profile (config/sender.yaml).** The sender, results, and address in these drafts are made up. Don't send them."


@dataclass
class DossierFiles:
    md: Path
    docx: Path
    pdf: Path


def build_blocks(session: Session, run: Run, rank: int, row: DigestRow, config: Config) -> list[Block]:
    score, lead, company = row.score, row.lead, row.company
    weights, demo = config.icp.weights.priority, config.sender.demo
    findings = {f.id: f for f in session.exec(select(Finding).where(Finding.lead_id == lead.id)).all()}
    steps = session.exec(
        select(OutreachStep).where(OutreachStep.run_id == run.id, OutreachStep.lead_id == lead.id)
        .order_by(col(OutreachStep.id))
    ).all()
    writeup = score.writeup or {}
    style = lead.communication_style or {}

    def cite(ids: list[int]) -> str:
        links = [f"[F{i}]({findings[i].source_url})" if findings.get(i) and findings[i].source_url else f"F{i}"
                 for i in ids]
        return f" ({', '.join(links)})" if links else ""

    blocks: list[Block] = [
        # 1. Header
        Heading(1, lead.full_name),
        *([Para(DEMO_BANNER)] if demo else []),
        Para(" · ".join(x for x in (lead.current_title, company.name if company else None, lead.location) if x)),
        Para(f"**Priority #{rank}** in this digest · [Open in Sales Navigator]({row.url}) · "
             f"run {run.id}, {run.started_at[:10]}"),
        # 2. Scores
        Heading(2, "Scores"),
        Para(f"**Priority score {score.priority_score:.0f}/100** = {weights.match:g} × match "
             f"{score.match_score:.0f} + {weights.response:g} × response likelihood {score.response_score or 0:.0f}. "
             "It ranks the digest: fit counts more than timing."),
        Para(f"**Match {score.match_score:.0f}%** (how well they fit the ICP) · **Response likelihood "
             f"{score.response_score or 0:.0f}% ({score.response_label or 'n/a'})** (how likely they are to reply now)"),
        Table(["Match criterion", "Level", "Points", "Evidence", "Source"], [
            [c["criterion"], c["level"], f"{c['points']}/{c['max']}", c["value"] or "", c["source"] or ""]
            for c in (score.match_breakdown or {}).get("criteria", [])
        ]),
        Table(["Response factor", "Points", "Why", "Sources"], [
            [f["factor"], f"{f['points']}/{f['max']}", f["detail"] or "", ", ".join(f"F{i}" for i in f["finding_ids"])]
            for f in (score.response_breakdown or {}).get("factors", [])
        ]),
        # 3. Why this lead
        Heading(2, "Why this lead"),
        Bullets([f"{r['text']}{cite(r['finding_ids'])}" for r in score.reasons or []]
                or ["No reasons written (drafting didn't run for this lead)."]),
        # 4. Person
        Heading(2, "Person"),
        Para(writeup.get("person_summary") or "No summary written."),
        Bullets([
            f"{r.get('title')} at {r.get('company_name')}, {r.get('start') or '?'} to {r.get('end') or 'present'}"
            for r in (lead.experience or [])[:6]
        ] or ["Career history not read yet (no deep read)."]),
    ]
    if style:
        blocks.append(Para(f"**Communication style:** {style.get('formality')}, {style.get('typical_length')} "
                           f"messages, {'uses' if style.get('uses_emoji') else 'no'} emoji. Posts about: "
                           f"{', '.join(style.get('recurring_topics') or []) or 'unknown'}."))
    posts = session.exec(select(Post).where(Post.lead_id == lead.id).order_by(col(Post.posted_at).desc())).all()[:3]
    if posts:
        blocks.append(Bullets([f"{(p.posted_at or '')[:10]}: {(p.text or '')[:220]}" for p in posts]))

    # 5. Company
    blocks += [Heading(2, "Company"), Para(writeup.get("company_snapshot") or _summary(findings) or "No snapshot yet.")]
    if company is not None:
        facts = [
            f"Industry: {company.industry}" if company.industry else None,
            f"LinkedIn employees: {company.headcount}" if company.headcount else None,
            f"Headcount change, 6 months: {company.headcount_growth_6mo:+.0%}"
            if company.headcount_growth_6mo is not None else None,
            f"Headquarters: {company.hq_location}" if company.hq_location else None,
            f"Website: https://{company.domain}" if company.domain else None,
        ]
        if any(facts):
            blocks.append(Bullets([f for f in facts if f]))

    # 6. Research findings
    research = [f for f in findings.values() if f.kind in RESEARCH_KINDS]
    blocks += [Heading(2, "Research findings"), Table(["", "Kind", "Finding", "Confidence", "Date", "Source"], [
        [f"F{f.id}", f.kind, f.value, f.confidence, f.event_date or "", _link(f.source_url)] for f in research
    ]) if research else Para("No research findings.")]

    # 7. Contact info
    contacts = [
        [f.kind, f.value, f.confidence + (", published" if (f.source_provider or "").endswith(":published")
                                          else ", guessed from the company's email format"
                                          if (f.source_provider or "").endswith(":pattern") else ""),
         _link(f.source_url)]
        for f in findings.values() if f.kind in ("email", "phone")
    ]
    contacts.append(["LinkedIn", f"{_ordinal(lead.connection_degree)}-degree connection" if lead.connection_degree
                     else "connection degree unknown", "verified", _link(row.url)])
    blocks += [Heading(2, "Contact info"), Table(["Channel", "Value", "Confidence", "Source"], contacts)]

    # 8. Affinity and warm paths
    affinity = [f for f in findings.values() if f.kind == "affinity"]
    blocks += [Heading(2, "Affinity and warm paths"),
               Bullets([f"{f.value}{cite([f.id])}" for f in affinity] or ["None found."])]

    # 9. Talking points
    blocks += [Heading(2, "Talking points"), Bullets(writeup.get("talking_points") or ["None written."])]

    # 10. Outreach plan
    blocks.append(Heading(2, "Outreach plan"))
    if not steps:
        blocks.append(Para("No drafts yet. Fill in config/sender.yaml (name, company, offer, mailing address), "
                           "then the next run drafts the full sequence."))
    else:
        blocks.append(Para("Send each touch yourself on its date. **Stop the whole sequence the moment they reply, "
                           "on any channel.** Copy drafts from the .md or .docx version: the PDF can't show every "
                           "emoji."))
        blocks.append(Table(["Step", "Date", "Channel", "Angle", "Checks"], [
            [s.step_code, _day(s.planned_date), CHANNEL_NAMES.get(s.channel, s.channel), s.angle or "",
             "passed" if s.checks_passed else "needs review"] for s in steps
        ]))
        failures = writeup.get("check_failures") or []
        for s in steps:
            label = " (recommended first message)" if s.step_code == "L1" else ""
            blocks.append(Heading(3, f"{s.step_code} · {_day(s.planned_date)} · "
                                     f"{CHANNEL_NAMES.get(s.channel, s.channel)}{label}"))
            meta = f"Angle: {s.angle}. Cites{cite(s.cited_finding_ids or []) or ' nothing'}."
            if not s.checks_passed:
                problems = [p.split(": ", 1)[1] for p in failures if p.startswith(f"{s.step_code}:")]
                meta += " **Needs review:** " + ("; ".join(problems) or "no draft was written") + "."
            blocks.append(Para(meta))
            lines = ([f"Subject: {s.subject}", ""] if s.subject else []) + (s.body or "(no draft)").splitlines()
            blocks.append(Quote(lines))

    # 11. Sources
    used = sorted({(f.source_url, f.found_at[:10]) for f in findings.values() if f.source_url})
    blocks += [Heading(2, "Sources"), Bullets([f"{_link(url)} (retrieved {day})" for url, day in used] or ["None."])]
    return blocks


def write_dossier(paths: Paths, session: Session, run: Run, rank: int, row: DigestRow,
                  config: Config) -> DossierFiles:
    out_dir = paths.run_output_dir(run) / f"run{run.id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{rank:02d}-{_slug(row.lead.full_name)}-{_slug(row.company.name if row.company else 'unknown')}"
    blocks = build_blocks(session, run, rank, row, config)
    files = DossierFiles(out_dir / f"{stem}.md", out_dir / f"{stem}.docx", out_dir / f"{stem}.pdf")
    markdown = to_markdown(blocks)
    files.md.write_text(markdown)
    write_docx(blocks, files.docx)
    write_pdf(markdown, files.pdf, row.lead.full_name)

    dossier = session.get(Dossier, (run.id, row.lead.id)) or Dossier(run_id=run.id, lead_id=row.lead.id, rank=rank)
    dossier.rank, dossier.md_path, dossier.docx_path, dossier.pdf_path = rank, str(files.md), str(files.docx), str(files.pdf)
    session.add(dossier)
    return files


def publish(paths: Paths, session: Session, run: Run, config: Config, *, mark: bool) -> tuple[Digest, Path]:
    """Build the digest, write a dossier per lead, then the digest linking to them. `mark` records the leads
    as shown in this run's digest (stage 10 does; rebuilding an old run's documents doesn't)."""
    digest = build_digest(session, run, config)
    for rank, row in enumerate(digest.rows, 1):
        files = write_dossier(paths, session, run, rank, row, config)
        row.dossier = str(files.md.relative_to(paths.run_output_dir(run)))
        if mark:
            row.lead.last_in_digest_run_id = run.id
            session.add(row.lead)
    if mark:
        run.leads_qualified = len(digest.rows)
        session.add(run)
    session.commit()
    digest.demo = config.sender.demo
    return digest, write_digest(paths, digest, session)


def dossiers(ctx: RunContext) -> None:
    """Stage 10: dossiers and the digest."""
    digest, path = publish(ctx.paths, ctx.session, ctx.run, ctx.config, mark=True)
    ctx.log.info("digest and %d dossiers -> %s", len(digest.rows), path.parent)


def _summary(findings: dict[int, Finding]) -> str | None:
    return next((f.value for f in findings.values() if f.kind == "company" and f.source_provider
                 and f.source_provider.endswith(":summary")), None)


def _link(url: str | None) -> str:
    return f"[{re.sub(r'^https?://(www\.)?', '', url)[:60]}]({url})" if url else ""


def _day(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%a %b %-d") if iso else ""


def _ordinal(n: int | None) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, str(n))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "x"
