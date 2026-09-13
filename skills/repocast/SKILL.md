---
name: repocast
description: >
  Create a local narrated product or technical demo from real project behavior. Use for
  product demos, software workflows, code walkthroughs, CLI/API demos, bug reproductions, release videos,
  onboarding, benchmarks, take-home submissions, or requests mentioning repocast.
  Produces MP4/GIF, narration, optional captions, and verification JSON.
metadata:
  type: workflow
---

# Repocast demo workflow

Repocast is the deterministic renderer; you are the planner. Inspect the target project,
choose the shortest coherent story, express it in YAML, execute it, and inspect the local
result. Do not upload or publish the output unless the user explicitly asks.

## Prerequisites

```bash
uv tool install git+https://github.com/jd316/repocast
playwright install chrome
```

From a source checkout, use `uv run python -m repocast`. FFmpeg and FFprobe must be on
`PATH`. Voice generation needs `GEMINI_API_KEY`; use `--no-voice` when it is unavailable
or when the user will narrate.

## Required workflow

1. Read the project instructions, README, package/build files, and relevant code.
2. Find real test, CLI, API, and app-start commands. Never invent successful output.
3. Decide the audience and tell one story. Prefer 3–7 scenes and under two minutes unless
   the user requests something longer.
4. Run `repocast schema` and create `walkthrough.yaml` beside the target project.
5. Run `repocast validate walkthrough.yaml --json`.
6. Run `repocast dry-run walkthrough.yaml --json`. Repair failed commands, startup,
   anchors, and selectors before recording.
7. Run `repocast render walkthrough.yaml --json` (plus `--no-voice` when appropriate).
8. Read the JSON result and verification report. Extract representative frames with
   FFmpeg and inspect them visually. Repair clipping, unreadable text, stale overlays,
   awkward timing, or incorrect narration, then render again.
9. Return the local paths and a concise description of what the video proves.

Preserve failed render scratch files until the failure is understood. Never use
`allow_failure: true` merely to make a broken demo pass; it is only for deliberately
demonstrating an error path.

## Story selection

| User intent | Composition |
|---|---|
| Code walkthrough | `doc` → `terminal` → `app` |
| CLI/API demo | `terminal`, optionally followed by `app` |
| Product demo | `app` with focus, click effects, and annotations |
| Bug reproduction | `app`/`terminal` actions that visibly reproduce the behavior |
| Release demo | short `doc` context followed by the changed behavior |
| Architecture/slides | `doc` and generated diagrams through `media` |
| Existing footage | `media`, optionally with narration, trim, fade, and audio |
| Before/after | two explicitly named segments with the same framing |

## Configuration essentials

Paths resolve relative to the YAML file. Use `repocast schema` as the current contract.

```yaml
output: demo.mp4
size: youtube
captions: true
burn_captions: true
theme: {background: "#111827", padding: 36, radius: 16, shadow: true}
video: {crf: 21, fps: 30, preset: slow}
quality: {loudness_target: -16, loudness_tolerance: 2}
tts: {provider: gemini, voice: Charon, model: gemini-3.1-flash-tts-preview}
segments:
  - doc:
      file: DESIGN.md
      steps: [{anchor: architecture, say: "The design has three layers."}]
  - terminal:
      cwd: .
      steps: [{run: ["pytest", "-q"], say: "The real tests pass."}]
  - app:
      cwd: .
      start: ["npm", "run", "dev"]
      url: http://127.0.0.1:3000
      ready_path: /
      actions:
        - {fill: "#email", value: "demo@example.com", focus: true}
        - {click: "#submit", focus: true, effect: ripple}
        - {wait_for: ".result"}
        - annotate: {target: ".result", text: "Real response"}
        - check: {no_scroll: true, min_font_size: 18, required_text: ["Real response"]}
        - {say: "The result comes from the running application."}
  - media: {file: architecture.png, duration: 5, say: "The architecture at a glance."}
```

App action verbs are `say`, `click`, `fill`, `wait_for`, `press`, `goto`, `eval`,
`sleep`, `annotate`, and `check`. Use exactly one verb per action. Prefer stable IDs, roles, or
test IDs over fragile CSS ancestry selectors.

Narration must describe what is visible and remain true across environments. Do not
hardcode variable counts or timings. Repocast measures speech first; do not add manual
sleeps to approximate narration duration.

Use imported audio, webcam overlays, GIF output, trimming, fades, or cursor styling only
when they improve the requested story. Avoid turning a concise technical demo into a
feature tour.
