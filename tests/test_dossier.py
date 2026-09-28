"""Stage 10: dossiers (Markdown, DOCX, PDF) and the digest linking to them. Fakes only."""

import docx
from sqlmodel import Session, select

from db import make_engine
from db.models import Dossier
from pipeline.cli import main
from tests.test_drafting import FakeWriter, run_pipeline, with_sender

SECTIONS = ["Scores", "Why this lead", "Person", "Company", "Research findings", "Contact info",
            "Affinity and warm paths", "Talking points", "Outreach plan", "Sources"]


def run_dir(paths, run):
    return paths.output_dir / run.started_at[:10] / f"run{run.id}"


def test_dossier_has_every_section_in_three_formats(paths, full_sequence):
    with_sender(paths)
    run = run_pipeline(paths, FakeWriter())
    files = sorted(run_dir(paths, run).iterdir())
    assert [f.name for f in files] == ["01-jordan-reyes-hill-country-freight.docx",
                                       "01-jordan-reyes-hill-country-freight.md",
                                       "01-jordan-reyes-hill-country-freight.pdf"]
    md = files[1].read_text()
    assert md.startswith("# Jordan Reyes\n")
    for section in SECTIONS:
        assert f"\n## {section}\n" in md, section
    assert "### L1 · " in md and "(recommended first message)" in md
    assert "> Subject: Hutto cross-dock" in md and "100 Congress Ave" in md  # email with its footer
    assert "[hcf.example/team](https://hcf.example/team)" in md  # sourced contact info
    assert "likely, published" in md

    headings = [p.text for p in docx.Document(files[0]).paragraphs if p.style.name.startswith("Heading")]
    assert all(section in headings for section in SECTIONS)
    assert files[2].read_bytes().startswith(b"%PDF") and files[2].stat().st_size > 10_000

    with Session(make_engine(paths.db_path)) as session:
        dossier = session.exec(select(Dossier)).one()
        assert (dossier.rank, dossier.md_path) == (1, str(files[1]))


def test_digest_links_each_dossier(paths):
    with_sender(paths)
    run = run_pipeline(paths, FakeWriter())
    digest = (paths.output_dir / run.started_at[:10] / f"digest-run{run.id}.md").read_text()
    assert f"[open](run{run.id}/01-jordan-reyes-hill-country-freight.md)" in digest


def test_flagged_drafts_say_why(paths, full_sequence):
    with_sender(paths)

    class Stubborn(FakeWriter):
        def parse(self, **kwargs):
            result = super().parse(**kwargs)
            if hasattr(result.parsed_output, "drafts"):
                result.parsed_output.drafts[2].body = "Hi [First Name]!"
            return result

    run = run_pipeline(paths, Stubborn())
    md = next(run_dir(paths, run).glob("*.md")).read_text()
    assert "| L3 | " in md and "needs review" in md
    assert "**Needs review:** unfilled placeholder '[First Name]'." in md


def test_without_sender_the_plan_explains_what_to_fill_in(paths):
    (paths.config_dir / "sender.yaml").write_text("sender: {}\n")
    run = run_pipeline(paths, FakeWriter())
    md = next(run_dir(paths, run).glob("*.md")).read_text()
    assert "No drafts yet. Fill in config/sender.yaml" in md


def test_digest_command_rebuilds_without_marking(paths, monkeypatch, capsys):
    with_sender(paths)
    run = run_pipeline(paths, FakeWriter())
    for f in run_dir(paths, run).iterdir():
        f.unlink()
    monkeypatch.setenv("LEADGEN_ROOT", str(paths.root))
    assert main(["digest", str(run.id)]) == 0
    assert len(list(run_dir(paths, run).iterdir())) == 3
    assert "1 dossiers" in capsys.readouterr().out


def test_no_emoji_in_drafts_or_documents(paths):
    with_sender(paths)

    class Emoji(FakeWriter):
        def parse(self, **kwargs):
            result = super().parse(**kwargs)
            if hasattr(result.parsed_output, "drafts"):
                for d in result.parsed_output.drafts:
                    d.body = "Congrats on Hutto 🎉 Worth comparing notes? 🚛"
            return result

    run = run_pipeline(paths, Emoji())
    with Session(make_engine(paths.db_path)) as session:
        from db.models import OutreachStep
        bodies = [s.body for s in session.exec(select(OutreachStep)).all()]
    assert bodies and all("🎉" not in b and "🚛" not in b for b in bodies)
    assert bodies[0] == "Congrats on Hutto Worth comparing notes?"
    md = next(run_dir(paths, run).glob("*.md")).read_text()
    assert "🎉" not in md and "Congrats on Hutto Worth comparing notes?" in md


def test_demo_sender_banner(paths):
    import yaml
    from tests.test_drafting import SENDER
    (paths.config_dir / "sender.yaml").write_text(yaml.safe_dump({**SENDER, "demo": True}))
    run = run_pipeline(paths, FakeWriter())
    md = next(run_dir(paths, run).glob("*.md")).read_text()
    digest = (paths.output_dir / run.started_at[:10] / f"digest-run{run.id}.md").read_text()
    assert "Demo sender profile" in md and "Don't send them." in md
    assert "Demo sender profile" in digest


def test_shipped_demo_sender_is_complete_and_flagged(paths):
    from pipeline.config import load_config
    sender = load_config(paths.config_dir).sender
    assert sender.demo and sender.missing_for_drafting() == []


def test_research_trail_and_cost_report(paths):
    from tests.test_research import FakeClient, record, response
    from types import SimpleNamespace as NS
    import tests.test_drafting as td

    with_sender(paths)
    searched = NS(type="server_tool_use", name="web_search", input={"query": "Hill Country Freight Hutto"})
    fetched = NS(type="server_tool_use", name="web_fetch", input={"url": "https://hcf.example/about"})
    researcher = FakeClient([response(searched, fetched, record([td.EMAIL]), searches=1)])

    class Search:
        def open(self, name): pass
        def cards(self): return [td.JORDAN]
        def next_page(self): return False

    from pipeline.drafting import drafting
    from pipeline.linkedin.searches import run_searches
    from pipeline.research import research
    from pipeline.run import start_run
    from pipeline.stages import STAGES, Stage
    swapped = {2: lambda ctx: None, 3: lambda ctx: run_searches(ctx, Search()), 6: lambda ctx: None,
               7: lambda ctx: research(ctx, researcher), 9: lambda ctx: drafting(ctx, FakeWriter())}
    run = start_run(paths, stages=[Stage(s.number, s.name, swapped.get(s.number, s.run), s.uses_linkedin)
                                   for s in STAGES])
    md = next(run_dir(paths, run).glob("*.md")).read_text()
    assert "## Research trail" in md
    assert "Searched: Hill Country Freight Hutto" in md and "Read: [hcf.example/about]" in md
    digest = (paths.output_dir / run.started_at[:10] / f"digest-run{run.id}.md").read_text()
    assert "## Where the time and money went" in digest
    assert "| 7. research | completed |" in digest
    assert "| Jordan Reyes | $" in digest
