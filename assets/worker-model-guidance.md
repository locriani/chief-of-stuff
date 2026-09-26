# Worker model and provider guidance

This is the workspace's editable routing policy for dispatched workers. Replace the suggested models with exact IDs available to your accounts. The coordinator reads this file before proposing a worker; the user still approves every new launch.

## Choice order

1. Follow an explicit user choice of provider, runtime, model, or effort exactly. Do not silently substitute a smaller model in the same family. If the requested choice is unavailable, report that and ask which alternative to use.
2. Honor task requirements: tools, repository access, network access, context size, visual work, and the ability to run without prompts in one-shot mode.
3. Among suitable options, choose the least expensive model likely to finish the task well. Increase reasoning effort for ambiguity, risk, or difficult debugging. Reuse a working session's model for follow-up work unless the user changes it.
4. State the proposed runtime, exact model ID, effort if supported, and reason before the user approves dispatch. Check installed CLI model lists and authentication through the configured interactive login shell. Do not print credentials.

## Suggested routing

| Task | Starting provider and model class | Reasoning effort |
|---|---|---|
| Architecture, security, migration, or hard debugging | Codex with its strongest available reasoning model (for example, GPT-6 Astra if offered), or Claude Code with Opus | High |
| Multi-file implementation with tests | Codex with a capable coding model (for example, GPT-6 Sol if offered), or Claude Code with Sonnet | Medium; high if the failure is hard to reproduce |
| Small mechanical edit, documentation, or straightforward triage | A fast suitable model on an available provider, such as a fast Codex model or Claude Haiku | Low to medium |
| Editor-centered UI work or work that needs Cursor-specific tools | Cursor CLI with a model that supports those tools | Medium |
| Multimodal work or work that needs Antigravity-specific tools | Antigravity CLI with a capable Gemini model | Medium to high |
| Independent review of a completed change | A different provider or model family from the implementer, when available | High for risky changes |

These are examples, not assumed installed model IDs. The table is not an authorization to switch providers or lower a user-requested model: a request for GPT-6 does not mean GPT-6 Sol. For one-shot tasks, choose a runtime that can complete the task with its noninteractive permissions; a blocked operation goes to human review with its reason and partial changes.

## Workspace choices

Record the exact model IDs and any provider restrictions for this workspace here. The CLI flags use these IDs verbatim.

| Task class | Runtime | Exact model ID | Effort | Notes |
|---|---|---|---|---|
| Deep reasoning | | | | |
| General implementation | | | | |
| Fast work | | | | |
| Independent review | | | | |
