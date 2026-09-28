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
| `leadgen run [--trigger manual\|cron] [--dry-run]` | Start a run. Dry runs skip LinkedIn and paid APIs and don't count toward daily caps. |
| `leadgen resume [RUN_ID] [--from-stage N]` | Continue a halted or failed run (defaults to the latest one, from its first unfinished stage). |
| `leadgen report [RUN_ID] [--details]` | Ranked match scores for a run (latest by default), with dropped leads and why; `--details` adds every lead's point breakdown. Also saved to `output/<run-date>/`. |
| `leadgen runs [--limit N]` | List recent runs with status, pages viewed, and where each stopped. |
| `leadgen check-config` | Validate `config/icp.yaml` and `config/sender.yaml`. |
| `leadgen login` | Open Sales Navigator in a visible window and wait while you log in. |
| `leadgen check-session` | Load Sales Navigator once, outside any run, and report whether a run would pass stage 2. |
| `leadgen capture URL NAME` | Save a LinkedIn page's HTML and screenshot to `tests/fixtures/captured/` (git-ignored: it holds real people's data). |

Exit codes: `0` completed, `1` failed or refused, `2` halted and needs you (login, verification prompt, or LinkedIn warning). A halt also sends a macOS notification and saves a screenshot and the page's HTML to `output/<run-date>/`.

## Layout

- `config/icp.yaml`: the ICP, saved-search names, every weight, point value, threshold, and cap.
- `config/sender.yaml`: who is sending and what they offer. Drafting needs it filled in.
- `pipeline/`: the orchestrator (`run.py`), the ten stages (`stages.py`), and the CLI.
- `pipeline/linkedin/`: the browser session, the pacer every LinkedIn action goes through (delays, caps, halt checks), and the halt rules.
- `db/`: SQLite schema. The database lives in `data/leadgen.db`.
- `output/<run-date>/`: run logs now; digests and dossiers later.

## Tests

```sh
uv run pytest                  # everything, including real-Chrome tests against a local fake server
uv run pytest -m "not browser" # skip the Chrome tests
```

No test touches LinkedIn.
