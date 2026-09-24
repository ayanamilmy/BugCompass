# BugCompass — Blender Bug Assistant

**From "I found a bug" to "I know which file to look at" — a local, privacy-first workbench that trains you to investigate real Blender bugs.**

BugCompass turns bug hunting into a guided workflow: pick an issue worth investigating (AI-scored, with claim-detection), let an AI engine map out evidence-backed investigation paths, run read-only experiments against a local Blender checkout, and practice against real historic fixes with the answer hidden. Everything runs on your machine.

[中文说明](README.zh-CN.md)

## Why BugCompass

Most tools stop at listing "good first issues". The hard part starts after you pick one: reading unfamiliar code, forming hypotheses, running experiments, and knowing when a fix is actually yours to submit. BugCompass is built for that gap.

- **Find** — AI Issue Scout scores Blender tracker issues by investigation value and filters out the ones already being worked on (assignees, PR links in comments, or claim-like replies).
- **Investigate** — Codex CLI or any OpenAI-compatible API (DeepSeek, Qwen, Kimi, GLM, OpenAI, local Ollama) produces three evidence-backed, refutable paths per case, with a cause-effect chain and a mind map.
- **Experiment** — green (read-only) and yellow (build/test/run) experiments run against an isolated source copy; red (mutating) ones require explicit authorization.
- **Practice** — replay 10 real historic fixes with the real answer hidden: investigate, commit to a root-cause judgment, then reveal the actual fix and get scored.
- **Report** — a standalone Report-a-Bug mode takes you from what you personally observed to a complete, checklist-backed official Blender report. No sources, terminal, or AI required.

## Quick start

```bash
pip install -e .
bugcompass gui
```

Requirements: Python 3.10+, Tkinter (bundled with most Python distributions). For source investigation, additionally install and sign in to [Codex CLI](https://developers.openai.com/codex/cli/), or configure any OpenAI-compatible model in ⚙ Settings → Model Services.

### Using your own model API (zero terminal)

⚙ Settings → Model Services → pick a provider → **Import key…** → **Test connection** → **Set as active engine**. A default provider list is auto-created on first launch — no terminal needed. Keys are stored in the macOS Keychain (or a `chmod 600` file elsewhere), never in `providers.json`, backups, diagnostics bundles, or telemetry.

### AI Issue Scout

Click **🔭 AI Issue Scout** in the sidebar: filter by module (real tracker labels), type, and count → **Start scan** (fetches projects.blender.org only when you click) → your model scores each issue (value 1–10, difficulty, one-line reason) → sorted results with **taken/free** status → one click creates an investigation case.

- Issues already being worked on are detected three ways: explicit assignees, PR links in comments, and claim-like replies read by the model. They are hidden by default.
- Good First Issues get their own toggle.
- Results are cached in `~/.bugcompass/scout/` and viewable offline.
- Personalize scoring criteria via `~/.bugcompass/scout-prompt.md` (this file is yours; it never enters the repository).

## Privacy by design

- No account, no cloud. The tracker is fetched only when you click "Start scan"; the AI engine only runs when you start an investigation.
- API keys live in environment variables or the OS keychain — never in project files.
- Telemetry is off by default; when on, it records only aggregate counts (model, duration, turns, tokens, cost) locally, and data leaves your machine only if you explicitly export it.
- Diagnostics bundles are auto-redacted and self-checked: no source code, no problem text, no keys.

## Interface language

The GUI ships bilingual: **中文 / English**, switchable in ⚙ Settings (restart to apply fully). The CLI is currently Chinese-only.

## Development

```bash
python -m pytest          # 172+ tests
python tools/gui_smoke.py # GUI smoke test (needs a display or Xvfb)
```

- Branching and multi-agent collaboration: see [docs/COLLABORATION.md](docs/COLLABORATION.md) — `main` is the single source of truth; branches are `codex/*`, `claude/*`, `arena/*`; tests must pass before pushing.
- All user-facing GUI strings go through `tr()` in `src/bugcompass/i18n.py` with both Chinese and English catalogs (enforced by tests).
- Packaging: PyInstaller + Inno Setup (see `packaging/`); Windows installers are built by CI on `v*` tags.

## License

MIT — see [LICENSE](LICENSE).
