# Project: chief-of-stuff

A Claude Code plugin that ships one agent, `agents/chief-of-stuff.md`, meant to run as the main session via `claude --agent chief-of-stuff`.

## Rules

- **TDD.** Behaviour starts as a failing eval case. The agent file changes only to turn a red case green.
- **Evals run against real `claude -p`.** Every run is sandboxed: temp fixture dir outside this repo as cwd; `--setting-sources project` (no user settings or user plugins); `--strict-mcp-config` with no user MCP servers, only harness mocks; `railway` and `git commit|add|push` denied and shimmed; Bash allows only `date`, `TZ=…`, `mv`, `mkdir`, and `python3 scripts/render_board.py`, so a filing move cannot become a copy or a delete and board html only comes from the renderer. The board publisher is a mock MCP tool (`evals/mock_board.py`) that takes a file path, like the Artifact tool. A denied call is still graded as an attempt.
- **Tests:** `cd evals && python3 -m unittest` · **Cases:** `python3 evals/run.py --arm baseline|agent --model sonnet [--case <glob>] [--runs N]`. **Opus only** (Zach, 2026-09-16: "we don't use Sonnet for this"): `--model opus`, and `--runs 3` before calling a case green. The agent frontmatter pins `model: opus` for the live run.
- **No shell scripts.** Harness code, shims, mocks, and probes are Python (or another real language). Shell appears only as a one-line command someone types.
- **Fixtures are templates.** `{{today}}`, `{{yesterday}}`, `{{tomorrow}}`, `{{tz}}`, `{{now_hhmm}}`, `{{now±Nh}}` (`|local`, `|hhmm`), and `{{dotgit}}` (a `.git` dir name, since git cannot track one) in file names and contents. No hardcoded dates. An unknown token fails the run.
- **Fixtures are generic.** No data from any workspace that uses this agent — no patient data, no program documents, no real deploy names.
- **Referenced from `~/Developer/ai-additions`** (`SETUP-LIST.md`, Referenced table). That repo's approval gate governs enabling.
