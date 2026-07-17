"""Text-to-speech via the Gemini API, with the quirks handled.

Lessons baked in:
- Use `gemini-3.1-flash-tts-preview`. The 2.5 TTS models return
  finishReason=OTHER with NO audio for some texts when a style prefix is
  present, and no rewording fixes it (systemInstruction -> HTTP 500 for TTS).
- If a styled request returns no audio, retry the bare text.
- Duration is measured EXACTLY from the PCM byte length, not estimated.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_VOICE = "Charon"              # Gemini prebuilt voices: Charon, Orus, Iapetus, Algenib, …
DEFAULT_STYLE = "Read this aloud in a clear, natural voice, professional and measured. Do not add any words: "
SAMPLE_RATE = 24000                   # Gemini TTS returns 24 kHz mono s16 PCM
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class TTSError(RuntimeError):
    pass


def _request(text: str, key: str, voice: str, model: str) -> bytes:
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    req = urllib.request.Request(
        _ENDPOINT.format(model=model),
        data=json.dumps(body).encode(),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    cand = json.load(urllib.request.urlopen(req, timeout=180))["candidates"][0]
    if "content" not in cand:                       # model refused: no audio came back
        raise TTSError(f"no audio (finishReason={cand.get('finishReason')})")
    return base64.b64decode(cand["content"]["parts"][0]["inlineData"]["data"])


def synth(text: str, key: str, out: Path, *, voice: str = DEFAULT_VOICE,
          model: str = DEFAULT_MODEL, style: str = DEFAULT_STYLE, tries: int = 3) -> float:
    """Synthesise one line to `out` (wav). Returns its exact duration in seconds.

    Tries the styled prompt first, then falls back to bare text if the model
    silently refuses. Raises TTSError only if BOTH fail after retries.
    """
    last = ""
    for prompt in (style + text, text):             # styled, then bare fallback
        for attempt in range(tries):
            try:
                pcm = _request(prompt, key, voice, model)
                raw = out.with_suffix(".pcm")
                raw.write_bytes(pcm)
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                     "-i", str(raw), str(out)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                raw.unlink()
                return len(pcm) / (SAMPLE_RATE * 2)  # 16-bit mono
            except Exception as e:                   # noqa: BLE001 - ret/fallback on anything
                last = str(e)
                if attempt < tries - 1:
                    time.sleep(2 + 3 * attempt)      # back off on 429/5xx
    raise TTSError(f"TTS failed for {text[:60]!r}: {last}")


def synth_all(texts: list[str], key: str, outdir: Path, **kw) -> dict[str, tuple[Path, float]]:
    """Synthesise every unique line up front (pre-flight). Returns {text: (wav, seconds)}.

    Doing this before any recording means a stubborn line fails fast instead of
    30 minutes into a render.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    audio: dict[str, tuple[Path, float]] = {}
    for i, t in enumerate(dict.fromkeys(texts)):     # de-dup, keep order
        p = outdir / f"cue{i:03d}.wav"
        dur = synth(t, key, p, **kw)
        audio[t] = (p, dur)
        print(f"  [{i + 1}/{len(set(texts))}] {dur:5.1f}s  {t[:60]}")
    return audio
