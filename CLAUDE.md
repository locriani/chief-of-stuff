# Project: chief-of-stuff

A versioned coordinator for Claude Code, Codex CLI, Cursor CLI, and Antigravity CLI. The Claude plugin ships `agents/chief-of-stuff.md`; `start_coordinator.py` adapts the same rules for the other hosts.

## Rules

- **TDD.** Behaviour starts as a failing eval case. The agent file changes only to turn a red case green.
- **Evals are runtime selectable.** `python3 evals/run.py --runtime claude|codex|cursor|agy --arm baseline|agent --model <host-model> [--case <glob>] [--runs N] [--golden]`. Claude is the default runtime and defaults to Sonnet for iteration; other runtimes require an explicit model. Run the Python suite with `python3 -m unittest discover -s evals -p 'test_*.py'`. The harness uses temporary fixtures and mocks external integrations; it records denied calls as attempts.
- **Claude plugin model policy.** The Claude agent frontmatter pins `model: opus`; Claude golden cases use `--golden --model opus --runs 3`. This policy applies to Claude runs only. Codex, Cursor, and Antigravity use their selected model for the same case definitions. A case is golden when its `case.json` says `"golden": true` with a `golden_why`; an empty golden selection exits nonzero.
- **No shell scripts.** Harness code, shims, mocks, and probes are Python (or another real language). Shell appears only as a one-line command someone types.
- **Fixtures are templates.** `{{today}}`, `{{yesterday}}`, `{{tomorrow}}`, `{{tz}}`, `{{now_hhmm}}`, `{{now±Nh}}` (`|local`, `|hhmm`), and `{{dotgit}}` (a `.git` dir name, since git cannot track one) in file names and contents. No hardcoded dates. An unknown token fails the run.
- **Fixtures are generic.** No data from any workspace that uses this agent — no patient data, no program documents, no real deploy names.
