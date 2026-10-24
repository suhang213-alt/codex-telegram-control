"""Offline speech recognition for the paired Telegram owner's voice note."""

import json
from pathlib import Path
import sys

from faster_whisper import WhisperModel


def main() -> None:
    model_dir = Path(sys.argv[1])
    audio_path = Path(sys.argv[2])
    if not (model_dir / "model.bin").is_file() or not audio_path.is_file():
        raise ValueError("Local model or audio is unavailable")
    model = WhisperModel(str(model_dir), device="cpu", compute_type="int8", local_files_only=True)
    segments, _ = model.transcribe(str(audio_path), language="ru", beam_size=5, condition_on_previous_text=False)
    transcript = " ".join(segment.text.strip() for segment in segments).strip()
    print(json.dumps({"text": transcript}, ensure_ascii=False))


if __name__ == "__main__":
    main()
