# Security Policy for AgentGear

## Reporting a vulnerability

If you discover a security issue, **please do not open a public issue**. Use
[GitHub's private vulnerability reporting](https://github.com/Human-Weapon/AgentGear/security/advisories/new)
for this repository (Security tab → "Report a vulnerability") instead, with:

- A description of the issue
- Steps to reproduce
- Affected versions
- Any suggested fix

We will acknowledge receipt within 48 hours and work toward a coordinated disclosure.

## Scope

AgentGear is a **compute-orchestration decision** library and CLI. Its security-sensitive surface is:

- **Local file I/O only**: watchdog heartbeats and checkpoints are written under a caller-supplied `state_dir`, path-contained via `path_security.py` (symlink/junction-aware, re-validated immediately before any lock/temp/destination write).
- **No network calls**: AgentGear never sends data anywhere. Routing and planning are pure, local, deterministic computations.
- **No credential access**: AgentGear does not read `.env` files, API keys, or any credential store, by design — it has no reason to, since it never calls a real model provider in v0.1.0.
- **No arbitrary code execution**: AgentGear never executes shell commands, installs packages, or writes outside its configured `state_dir`.

## What AgentGear deliberately does NOT do

- It does not decide *what context* to load — that is PromptGraph's responsibility. AgentGear only ever *requests* context through the abstract `ContextProvider` interface.
- It does not validate whether a skill, plugin, or automation is safe — that is SkillGuard's responsibility.
- It does not execute agents, call LLM providers, or manage credentials.
- It does not modify files outside its own state directory, and never touches sibling projects' repositories.

## Standalone guarantee

AgentGear must **never** require a sibling package to function. Optional sibling integration (`PromptGraphContextProvider`) is discovered via `importlib.util.find_spec` at call time and degrades gracefully — a missing or misbehaving sibling never raises out of AgentGear's public API. If a mandatory dependency or hidden integration is ever added, that is a security regression.

## Reporting process (priority)

We follow the ecosystem priority rubric:

- **P0** — security / data loss / critical bugs: fix immediately.
- **P1** — broken functionality: next release.
- **P2+** — scheduled normally.
