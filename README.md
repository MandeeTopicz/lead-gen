# lead-gen

Finds operations leaders at Austin logistics companies in Sales Navigator, scores them, researches the strongest, and drafts a dated outreach plan for each. A human reviews and sends every message; the agent never sends anything.

## Setup

```sh
brew install uv
uv sync
uv run leadgen check-config
uv run leadgen login          # log in yourself in a visible Chrome window; the session is saved in data/browser-profile/
uv run leadgen check-session  # confirm a run would get past the session check
```

The agent uses its own Chrome profile, separate from your everyday one, and never stores your LinkedIn password.

## Commands

| Command | What it does |
| --- | --- |
| `leadgen run [--trigger manual\|cron] [--dry-run] [--max-deep-reads N] [--watch]` | Start a run. Dry runs skip LinkedIn and paid APIs and don't count toward daily caps. `--watch` shows the Chrome window so you can watch it work. |
| `leadgen resume [RUN_ID] [--from-stage N]` | Continue a halted or failed run (defaults to the latest one, from its first unfinished stage). |
| `leadgen digest [RUN_ID]` | Rebuild and print a run's digest and dossiers (latest completed run by default). |
| `leadgen report [RUN_ID] [--details]` | Ranked match scores for a run (latest by default), with dropped leads and why; `--details` adds every lead's point breakdown. Also saved to `output/<run-date>/`. |
| `leadgen review` | Open the review screen: each digest lead's dossier, copyable drafts, good fit / not a fit, outreach status, notes, run history, and a Run now button. |
| `leadgen schedule install [--at 07:30] \| remove \| status` | The daily scheduled run as a macOS launchd job (runs once on wake if the Mac was asleep). |
| `leadgen runs [--limit N]` | List recent runs with status, pages viewed, and where each stopped. |
| `leadgen check-config` | Validate `config/icp.yaml` and `config/sender.yaml`. |
| `leadgen check-llm` | Confirm the Anthropic API key in `.env` works with the configured models. |
| `leadgen login` | Open Sales Navigator in a visible window and wait while you log in. |
| `leadgen check-session` | Load Sales Navigator once, outside any run, and report whether a run would pass stage 2. |
| `leadgen capture URL NAME` | Save a LinkedIn page's HTML and screenshot to `tests/fixtures/captured/` (git-ignored: it holds real people's data). |

Exit codes: `0` completed, `1` failed or refused, `2` halted and needs you (login, verification prompt, or LinkedIn warning). A halt also sends a macOS notification and saves a screenshot and the page's HTML to `output/<run-date>/`.

## Layout

- `config/icp.yaml`: the ICP, saved-search names, every weight, point value, threshold, and cap.
- `config/sender.yaml`: who is sending and what they offer. Ships with a fictional demo profile (`demo: true`) so the pipeline can run end to end; outputs carry a "don't send" banner until you replace it and set `demo: false`.
- `pipeline/`: the orchestrator (`run.py`), the ten stages (`stages.py`), and the CLI.
- `pipeline/linkedin/`: the browser session, the pacer every LinkedIn action goes through (delays, caps, halt checks), and the halt rules.
- `app/review.py`: the Streamlit review screen (`pipeline/review.py` holds its data logic).
- `db/`: SQLite schema. The database lives in `data/leadgen.db`.
- `output/<run-date>/`: `digest.md` (the day's latest) and `digest-run<N>.md`, run logs, and `run<N>/` with one dossier per lead as `.md`, `.docx`, and `.pdf`. Copy drafts from the `.md` or `.docx`; the PDF can't show every emoji.
- `.env`: `ANTHROPIC_API_KEY` for research and drafting (git-ignored; see `.env.example`).

## Tests

```sh
uv run pytest                  # everything, including real-Chrome tests against a local fake server
uv run pytest -m "not browser" # skip the Chrome tests
```

No test touches LinkedIn.

## Oversight

- **Review screen, Run history page:** while a run is going, a panel refreshes every few seconds with the current stage, elapsed time, pages, deep reads, Claude spend, and the latest log lines. Pick any past run to see stage times, Claude cost per lead, and its browser trace.
- **Run log** (`output/<date>/run.log`): every page the agent opened, the pause before it, what cap it counted against, and the halt checks; every search Claude ran and page it read during research; per-lead cost and time for research and drafting.
- **Browser trace** (`output/<date>/run<N>/browser-trace-*.zip`): a replayable timeline of every page, click, and scroll with screenshots. Open it with `uv run playwright show-trace <file>`. Turn off with `observability.browser_trace: false` in `icp.yaml`.
- **Watch mode:** `uv run leadgen run --watch` shows the Chrome window for that run (it takes focus).
- **Dossiers** end with a **Research trail**: what Claude searched, what it read, tokens, cost, and time. **Digests** end with **Where the time and money went**: time and Claude cost per stage and per lead.
