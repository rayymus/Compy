"""Speech-to-text wrapper using whisper.cpp + ffmpeg for mic capture.

Called by the Swift overlay when mic mode is active. Records N seconds of audio
from the default microphone via ffmpeg, transcribes via whisper-cli, returns JSON.

Usage:
  python3 -m compy.daemon.stt [--duration 3] [--model /path/to/ggml-tiny.en.bin]

Output (stdout):
  {"text": "transcribed text", "success": true}
  {"text": "", "success": false, "error": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_MODEL = os.path.expanduser("~/Library/Caches/whisper-cpp/ggml-tiny.en.bin")
DEFAULT_DURATION = 3  # seconds

def _find_whisper_cli() -> str:
    import shutil
    found = shutil.which("whisper-cli")
    if found:
        return found
    # Fallbacks for common Homebrew prefixes.
    for candidate in ("/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"):
        if Path(candidate).exists():
            return candidate
    return "whisper-cli"  # let subprocess fail with a clear error


def record_and_transcribe(
    duration: int = DEFAULT_DURATION,
    model_path: str = DEFAULT_MODEL,
) -> dict:
    if not Path(model_path).exists():
        return {"text": "", "success": False, "error": f"whisper model not found at {model_path}"}

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        # Record audio from default mic via ffmpeg (avfoundation on macOS).
        ffmpeg_cmd = [
            "ffmpeg",
            "-f", "avfoundation",
            "-i", ":0",  # default mic
            "-t", str(duration),
            "-ar", "16000",
            "-ac", "1",
            "-sample_fmt", "s16",
            "-y",
            wav_path,
        ]
        subprocess.run(
            ffmpeg_cmd,
            capture_output=True,
            timeout=duration + 5,
            check=False,
        )

        if not Path(wav_path).exists() or Path(wav_path).stat().st_size < 100:
            return {"text": "", "success": False, "error": "no audio captured"}

        # Transcribe via whisper-cli.
        whisper_cmd = [
            _find_whisper_cli(),
            "-m", model_path,
            "-f", wav_path,
            "--no-timestamps",
            "-np",  # no prints except result
            "--no-gpu",  # avoid Metal init spam in stdout
        ]
        proc = subprocess.run(
            whisper_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        text = proc.stdout.strip()
        # Strip Metal/ggml init lines that leak to stdout.
        lines = [l for l in text.splitlines() if not l.startswith("ggml_") and not l.startswith("load_backend")]
        clean = " ".join(lines).strip()

        return {"text": clean, "success": True}

    except subprocess.TimeoutExpired:
        return {"text": "", "success": False, "error": "transcription timed out"}
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Compy STT: record mic → transcribe via whisper.cpp")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION,
                        help=f"Recording duration in seconds (default: {DEFAULT_DURATION})")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL,
                        help="Path to whisper.cpp GGML model")
    args = parser.parse_args()

    result = record_and_transcribe(duration=args.duration, model_path=args.model)
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
