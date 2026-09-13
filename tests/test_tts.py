"""TTS cache behavior without calling an external provider."""

import wave

from repocast import tts


def test_synth_all_reuses_persistent_cache(tmp_path, monkeypatch):
    calls = []

    def fake_synth(text, _key, out, **_kwargs):
        calls.append(text)
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(b"\0\0" * 24_000)
        return 1.0

    monkeypatch.setenv("REPOCAST_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(tts, "synth", fake_synth)
    first = tts.synth_all(["hello"], "key", tmp_path / "first", voice="Test")
    second = tts.synth_all(["hello"], "key", tmp_path / "second", voice="Test")

    assert calls == ["hello"]
    assert first["hello"][1] == second["hello"][1] == 1.0
