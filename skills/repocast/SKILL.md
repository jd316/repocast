---
name: repocast
description: >
  Generate a narrated walkthrough MP4 of a code project — design doc + real
  terminal output + the live app driven in a browser — with AI voice-over, and
  NO manual screen recording or terminal emulator needed. Use when the user wants
  to "make a demo/walkthrough video", "record a walkthrough", "make a Loom/Jam for
  this project", produce a narrated video of their repo (for a take-home submission,
  README, onboarding, or OSS intro), or types /repocast. Outputs an MP4 with baked-in
  narration plus a timestamped NARRATION.md.
metadata:
  type: workflow
---

# repocast — narrated project walkthrough video

Turns a `walkthrough.yaml` into a 1080p narrated MP4. The user uploads the result
(YouTube/Loom/Jam); nothing is screen-recorded by hand.

**Install the `repocast` CLI once — the skill drives it:**
```bash
uv tool install git+https://github.com/jd316/repocast   # puts `repocast` on PATH
playwright install chrome                                # one-time browser download
```
Then every command below is just `repocast …`. If `repocast` isn't found, install it
with the line above (or, from a source checkout, run
`uv run --with pyyaml --with playwright --with markdown python3 -m repocast …`).
Config `file`/`cwd` paths resolve relative to the config file, so the `walkthrough.yaml`
can live next to the target project and you can run `repocast` from anywhere.

## Workflow when a user asks for a walkthrough video
1. **Understand the project**: read its README/design doc, find the test/demo commands
   (`make`, npm scripts, etc.), and how the app starts + its health URL and port.
2. **Scaffold the config** next to (not inside) the project: `repocast init walkthrough.yaml`,
   then fill in the three optional segment types — `doc`, `terminal`, `app` (schema below).
3. **Validate**: `repocast validate walkthrough.yaml`.
4. **Pick a voice**: `repocast voices` writes sample clips; the USER chooses (you can't
   judge a voice by ear).
5. **Render**:
   ```bash
   export GEMINI_API_KEY=...   # AI Studio key; x-goog-api-key header (newer keys are AQ.-prefixed)
   repocast render walkthrough.yaml
   ```
   Flags: `--no-voice` (silent, bring your own), `--voice Orus`, `--output path.mp4`.
6. **Verify** (auto after render, or `repocast verify out.mp4`): audio present + is speech,
   cue alignment. Then SPOT-CHECK A FRAME by eye (`ffmpeg -ss <t> -i out.mp4 -frames:v 1
   f.png`) — automated freeze checks (md5/freezedetect) LIE about held frames.

## Config schema (walkthrough.yaml)
```yaml
output: walkthrough.mp4
size: [1920, 1080]              # match the user's screen / upload target
tts: {provider: gemini, voice: Charon, model: gemini-3.1-flash-tts-preview}
segments:
  - doc:      {file: DESIGN.md, steps: [{anchor: <heading-id>, say: "..."}]}
  - terminal: {cwd: ., title: "proj — make", steps: [{run: ["make","test"], env: {}, say: "..."}]}
  - app:      {cwd: ., start: ["make","run"], url: "http://127.0.0.1:8000", ready_path: /health,
               zoom: 1.4, actions: [{say: "..."}, {click: "#x"}, {fill: "#y", value: "z"},
                                    {wait_for: ".done", timeout: 60000}]}
```
App action verbs: `say`, `click`, `fill`(+`value`), `wait_for`(+`timeout`), `press`, `goto`,
`eval`, `sleep`. Doc `anchor`s are rendered-markdown heading IDs (lowercase, spaces→hyphens) —
render errors LOUDLY on a wrong anchor, so a typo won't silently drop a section.

## Non-obvious rules (baked into the engine — don't fight them)
- **Audio-first pacing**: narration is synthesised and MEASURED first; every on-screen hold
  is ≥ the spoken line. Never overlay TTS on a fixed video (drifts).
- **TTS model `gemini-3.1-flash-tts-preview`**: the 2.5 TTS models return `finishReason:OTHER`
  with no audio for some texts under a style prefix. The engine pre-flights all lines (fail
  fast) and falls back to bare text.
- **No terminal emulator?** The terminal segment renders captured stdout as HTML and films it.
- **Never hardcode narration numbers that differ per backend** (mock vs real model); describe
  behavior or read from the live response.

## For a hiring/take-home submission
The "explain your tradeoffs" criterion is about the candidate's own communication. Offer the
user `NARRATION.md` to record in their own voice (render with `tts.provider: none` for a
silent video to narrate over).
