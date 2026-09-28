"""Review screen: `uv run streamlit run app/review.py`.

Read each digest lead's dossier, copy drafts, mark good fit / not a fit, and track the outreach sequence.
Nothing here sends anything; you send every message yourself.
"""

from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from sqlmodel import Session

from db import make_engine
from pipeline import review
from pipeline.context import Paths
from pipeline.dossier import CHANNEL_NAMES
from pipeline.text import strip_emoji

paths = Paths.default()
load_dotenv(paths.root / ".env")
st.set_page_config(page_title="Lead review", layout="wide")


@st.cache_resource
def engine():
    return make_engine(paths.db_path)


def session() -> Session:
    return Session(engine(), expire_on_commit=False)


STATUS_LABELS = {None: "Not started", "active": "Sequence active", "replied": "Replied (stop)",
                 "stopped": "Stopped", "done": "Sequence done"}


def digest_page() -> None:
    with session() as s:
        runs = review.runs_with_digest(s)
        if not runs:
            st.info("No digests yet. Start a run from Run history, or `uv run leadgen run`.")
            return
        run = st.sidebar.selectbox("Run", runs, format_func=lambda r: f"Run {r.id} · {r.started_at[:16].replace('T', ' ')}")
        leads = review.digest_leads(s, run.id)

    st.title(f"Digest: run {run.id}")
    st.caption(f"{run.started_at[:10]} · {run.leads_found} found · {len(leads)} in digest · "
               f"${run.llm_cost_usd:.2f} Claude · {run.pages_viewed} result pages · {run.profiles_read} deep reads")
    show = st.sidebar.radio("Show", ["All", "Not reviewed", "Good fit", "Not a fit"], horizontal=False)
    for item in leads:
        fit = item.review.fit if item.review else None
        if (show == "Not reviewed" and fit) or (show == "Good fit" and fit != "good") or \
                (show == "Not a fit" and fit != "not_fit"):
            continue
        lead_card(run.id, item)


def lead_card(run_id: int, item: review.ReviewLead) -> None:
    s, lead = item.score, item.lead
    fit = item.review.fit if item.review else None
    status = item.review.sequence_status if item.review else None
    badge = {"good": " · GOOD FIT", "not_fit": " · NOT A FIT"}.get(fit, "")
    title = (f"#{item.rank} {strip_emoji(lead.full_name)} · {lead.current_title or ''} · "
             f"{item.company.name if item.company else ''} · match {s.match_score:.0f}% · "
             f"response {s.response_score or 0:.0f}% {s.response_label or ''}{badge}")
    with st.expander(title):
        left, right = st.columns([3, 2])
        with right:
            st.markdown("**Your marks**")
            c1, c2 = st.columns(2)
            if c1.button("Good fit", key=f"good-{lead.id}", type="primary" if fit == "good" else "secondary"):
                mark(lead.id, fit="good")
            if c2.button("Not a fit", key=f"bad-{lead.id}", type="primary" if fit == "not_fit" else "secondary"):
                mark(lead.id, fit="not_fit")
            options = [None, *review.SEQUENCE_STATUSES]
            chosen = st.selectbox("Outreach", options, index=options.index(status), key=f"seq-{lead.id}",
                                  format_func=lambda v: STATUS_LABELS[v])
            if chosen != status:
                if chosen:
                    mark(lead.id, sequence_status=chosen)
                else:
                    mark(lead.id, clear=("sequence_status",))
            notes = st.text_area("Notes", value=(item.review.notes if item.review else "") or "", key=f"notes-{lead.id}")
            if st.button("Save notes", key=f"save-notes-{lead.id}"):
                mark(lead.id, notes=notes)
            for label, path, mime in (
                ("Download DOCX", item.dossier.docx_path,
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                ("Download PDF", item.dossier.pdf_path, "application/pdf"),
            ):
                if path and Path(path).exists():
                    st.download_button(label, Path(path).read_bytes(), file_name=Path(path).name, mime=mime,
                                       key=f"{label}-{lead.id}")
        with left:
            tabs = st.tabs(["Outreach plan", "Dossier"])
            with tabs[0]:
                if not item.steps:
                    st.info("No drafts for this lead. Check config/sender.yaml.")
                for step in item.steps:
                    warn = "" if step.checks_passed else " · **needs review**"
                    st.markdown(f"**{step.step_code}** · {step.planned_date} · "
                                f"{CHANNEL_NAMES.get(step.channel, step.channel)}{warn}  \n_{step.angle}_")
                    text = (f"Subject: {step.subject}\n\n" if step.subject else "") + (step.body or "(no draft)")
                    st.code(text, language=None, wrap_lines=True)  # has a copy button
            with tabs[1]:
                md = Path(item.dossier.md_path)
                st.markdown(md.read_text() if md.exists() else "Dossier file is missing; run `leadgen digest`.")


def mark(lead_id: int, **changes) -> None:
    with session() as s:
        review.save_review(s, lead_id, **changes)
    st.rerun()


def run_controls() -> None:
    """In the sidebar on every page: start a run, or see the one that's going."""
    with session() as s:
        running = review.run_in_progress(s, paths)
    st.sidebar.divider()
    if running:
        st.sidebar.warning(f"Run {running.id} in progress (started {running.started_at[11:16]}). "
                           "Refresh to update.")
    elif st.sidebar.button("Run now", type="primary", use_container_width=True):
        log = review.start_run_in_background(paths)
        st.sidebar.success(f"Run started in the background. Output: {log}. See Run history.")


def history_page() -> None:
    st.title("Run history")
    with session() as s:
        runs = review.runs(s)
    st.caption("Manual runs are capped per day (icp.yaml caps). A halted run needs you: see its reason and "
               "screenshot below, fix it (usually `uv run leadgen login`), then `uv run leadgen resume`.")
    st.dataframe(
        [{"run": r.id, "started": r.started_at[:16].replace("T", " "), "trigger": r.trigger, "status": r.status,
          "dry": r.dry_run, "pages": r.pages_viewed, "deep reads": r.profiles_read, "found": r.leads_found,
          "in digest": r.leads_qualified, "Claude $": round(r.llm_cost_usd, 2), "reason": r.halt_reason or ""}
         for r in runs],
        hide_index=True, use_container_width=True,
    )
    for r in runs:
        if r.status == "halted" and r.halt_screenshot_path and Path(r.halt_screenshot_path).exists():
            with st.expander(f"Run {r.id} halted: {r.halt_reason}"):
                st.image(r.halt_screenshot_path)


page = st.sidebar.radio("Page", ["Digest", "Run history"])
run_controls()
if page == "Digest":
    digest_page()
else:
    history_page()
