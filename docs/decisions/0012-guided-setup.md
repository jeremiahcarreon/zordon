# 0012: Install is two commands; the first run is a guided setup

**Status:** accepted, 2026-10-02

## Question

The install path had become `pipx install "zordon[local]"`, `zordon doctor
--download`, `ollama pull ...`, edit config.toml for keys, then `zordon serve`.
Each extra command is a place a first-time user stops.

## Decision

- `pipx install zordon` is the whole install. The local speech dependencies
  (faster-whisper, kokoro-onnx) move into the core dependency list; `[local]`
  stays as an empty alias so old instructions do not break.
- `zordon serve` with no config on an interactive terminal runs
  `zordon/setup.py` first. It detects tmux, Claude Code, Ollama (binary,
  server, pulled models), GPU, keys in the environment, cloudflared, Tailscale
  and downloaded models; asks four questions (speech, spoken-English rewriter,
  routing, reach) with the trade-offs written out next to each option; then
  does the work: `zordon doctor --download` for models, `ollama pull` for the
  model, cloudflared for the tunnel, and writes config.toml. Enter takes the
  detected default everywhere, so the zero-key path is four Enters.
- `zordon setup` re-runs it; `--yes` takes the defaults without asking;
  `zordon serve --no-setup` and non-interactive stdin fall back to writing
  defaults as before. EOF on stdin is treated as "take the default".
- Installing Ollama itself is a system change, so the wizard only runs the
  official installer after an explicit yes, and otherwise prints where to get
  it. It never pulls a model or starts a download the user did not choose.
- The wizard writes ordinary config values only. It cannot set a permission
  mode, cannot widen Claude Code's permissions, and never writes a bypass
  mode; `Config.validate()` runs on what it produces.

## Open

- A browser-based version of the same flow (shown by the web client when it
  connects to an unconfigured agent) would help people who start `zordon
  serve` from a launcher rather than a terminal. The terminal wizard covers
  the install path the README describes.
- `stt_device = "cuda"` is not offered yet because it needs the NVIDIA pip
  wheels; the wizard mentions the GPU only for the Ollama model choice.
