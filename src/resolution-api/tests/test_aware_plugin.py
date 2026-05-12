"""
Integration test for the AWARE watermark plugin.

Requires the watermark-aware-20 container to be running:
    docker compose up watermark-aware-20

Sends the test WAV file through /embed, then /detect, and verifies
the detected binding value matches the embedded one.
"""
from __future__ import annotations

import io
import sys

import httpx
import numpy as np
import soundfile as sf

PLUGIN_URL = "http://localhost:8102"
TIMEOUT = 120.0


def _make_dummy_wav(duration_s: float = 3.0, sr: int = 16000) -> bytes:
    """Generate a complex multi-tone WAV file with noise."""
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)

    # Create a complex signal with multiple sinusoids at different frequencies
    # Fundamental and harmonics
    audio = 0.3 * np.sin(2 * np.pi * 440 * t)      # A4 (440 Hz)
    audio += 0.2 * np.sin(2 * np.pi * 554.37 * t)  # C#5
    audio += 0.15 * np.sin(2 * np.pi * 659.25 * t) # E5
    audio += 0.1 * np.sin(2 * np.pi * 880 * t)     # A5 (first harmonic)

    # Add some low-frequency content
    audio += 0.05 * np.sin(2 * np.pi * 110 * t)    # A2

    # Add Gaussian noise (signal-to-noise ratio ~20dB)
    noise = np.random.normal(0, 0.05, len(t))
    audio += noise

    # Normalize to prevent clipping
    audio = audio / np.max(np.abs(audio)) * 0.8
    audio = audio.astype(np.float32)

    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV")
    return buf.getvalue()


def main() -> None:
    audio_bytes = _make_dummy_wav()
    print(f"Generated dummy WAV ({len(audio_bytes)} bytes)")

    with httpx.Client(timeout=TIMEOUT) as client:
        # 1. Health check
        r = client.get(f"{PLUGIN_URL}/health")
        r.raise_for_status()
        print(f"/health: {r.json()}")

        # 2. Info
        r = client.get(f"{PLUGIN_URL}/info")
        r.raise_for_status()
        info = r.json()
        print(f"/info: alg={info['alg']} valueBits={info['valueBits']}")

        # 3. Embed
        print("\nEmbedding watermark...")
        r = client.post(
            f"{PLUGIN_URL}/embed",
            content=audio_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        r.raise_for_status()
        watermarked_bytes = r.content
        embedded_value = r.headers.get("X-Binding-Value")
        print(f"/embed: watermarked size={len(watermarked_bytes)} bytes")
        print(f"  X-Binding-Value: {embedded_value}")

        # 4. Detect from watermarked audio
        print("\nDetecting watermark from watermarked audio...")
        r = client.post(
            f"{PLUGIN_URL}/detect",
            content=watermarked_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        r.raise_for_status()
        detect_result = r.json()
        detected_value = detect_result.get("bindingValue")
        print(f"/detect: bindingValue={detected_value}")

        # 5. Verify round-trip
        if detected_value == embedded_value:
            print("\nROUND-TRIP OK: detected value matches embedded value")
        else:
            print(f"\nROUND-TRIP MISMATCH:")
            print(f"  embedded: {embedded_value}")
            print(f"  detected: {detected_value}")
            sys.exit(1)

        # 6. Detect from original (unwatermarked) audio
        print("\nDetecting watermark from original (unwatermarked) audio...")
        r = client.post(
            f"{PLUGIN_URL}/detect",
            content=audio_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        r.raise_for_status()
        original_result = r.json()
        original_value = original_result.get("bindingValue")
        print(f"/detect on original: bindingValue={original_value}")

        if original_value is None:
            print("  (no watermark detected in original — expected)")
        elif original_value != embedded_value:
            print("  (different value detected in original — expected)")
        else:
            print("  WARNING: same value detected in original — false positive?")


if __name__ == "__main__":
    main()
