"""Export real Silero prompts and commands for listening, using the actual inbound welcome."""

import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/src"))
from calls.tts.silero import SileroTTS, to_pcm16


def main():
    output = ROOT / "debug/runs/speech-only/audio"
    output.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "scenarios/inbound.flow").read_text()
    welcome = re.search(r'state welcome\s+say "([^"]+)"', text).group(1)
    cases = {
        "welcome": welcome,
        "book": "Запишите меня к терапевту.",
        "move": "Перенесите мою запись на пятницу после обеда.",
        "cancel": "Да, отмените мою запись.",
    }
    model = SileroTTS(
        ROOT / "modules/tts/models/v5_cis_base.pt", "ru_ekaterina", stress="dict"
    )
    for name, phrase in cases.items():
        with wave.open(str(output / (name + ".wav")), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(to_pcm16(model.synth(phrase, 1.5)))
    print(output)


if __name__ == "__main__":
    main()
