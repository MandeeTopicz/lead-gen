# lead-gen

Finds operations leaders at Austin logistics companies in Sales Navigator, scores them, researches the strongest, and drafts a dated outreach plan for each. A human reviews and sends every message; the agent never sends anything.

## Setup

```sh
brew install uv
uv sync
uv run leadgen check-config
```

## Commands

| Command | What it does |
| --- | --- |
| `leadgen run [--trigger manual\|cron] [--dry-run]` | Start a run. Dry runs skip LinkedIn and paid APIs and don't count toward daily caps. |
| `leadgen resume [RUN_ID] [--from-stage N]` | Continue a halted or failed run (defaults to the latest one, from its first unfinished stage). |
| `leadgen runs [--limit N]` | List recent runs with status, pages viewed, and where each stopped. |
| `leadgen check-config` | Validate `config/icp.yaml` and `config/sender.yaml`. |

Exit codes: `0` completed, `1` failed or refused, `2` halted and needs you (login, verification prompt, or LinkedIn warning).

## Layout

- `config/icp.yaml`: the ICP, saved-search names, every weight, point value, threshold, and cap.
- `config/sender.yaml`: who is sending and what they offer. Drafting needs it filled in.
- `pipeline/`: the orchestrator (`run.py`), the ten stages (`stages.py`), and the CLI.
- `db/`: SQLite schema. The database lives in `data/leadgen.db`.
- `output/<run-date>/`: run logs now; digests and dossiers later.

## Tests

```sh
uv run pytest
```
