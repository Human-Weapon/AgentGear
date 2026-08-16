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

- **Local file I/O only**: watchdog heartbeats and checkpoints target a caller-supplied `state_dir`, with symlink/junction-aware containment re-validated immediately before any lock, temp, or destination write. This is **best-effort TOCTOU** mitigation, not an OS-handle-based filesystem sandbox: rejected operations leave no outside artifacts in the supported swap scenarios, but a sufficiently privileged local attacker might still win a directory-swap race between validation and the next filesystem call.
- **No network calls**: AgentGear never sends data anywhere. Routing and planning are pure, local, deterministic computations.
- **No credential access**: AgentGear does not read `.env` files, API keys, or any credential store, by design — it has no reason to, since it never calls a real model provider in v0.1.0.
- **No arbitrary code execution**: AgentGear never executes shell commands or installs packages. Its only writes are the persistence artifacts described above, subject to that stated containment limitation.

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
