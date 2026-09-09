"""Lists TTS voices installed on this Windows machine that pyttsx3 (SAPI5) can see.

Usage:
    python tools/list_voices.py

Use the printed name (or a distinctive part of it) as `feedback.tts_voice` in
config/config.yaml to make the assistant speak with that voice instead of the
system default.
"""
import pyttsx3


def main() -> None:
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")

    if not voices:
        print("No SAPI5 voices found on this system.")
        return

    current_id = engine.getProperty("voice")
    for index, voice in enumerate(voices):
        marker = " (current default)" if voice.id == current_id else ""
        print(f"[{index}] {voice.name}{marker}")
        print(f"    id:        {voice.id}")
        print(f"    languages: {voice.languages}")
        print(f"    gender:    {voice.gender}")
        print()

    print(
        "To use one of these, set feedback.tts_voice in config/config.yaml to a\n"
        "distinctive substring of its name (case-insensitive), e.g.:\n"
        '  tts_voice: "Zira"'
    )
    print(
        "\nIf the built-in voices all sound harsh, Windows 11 offers extra\n"
        "natural-sounding voices under Settings -> Time & language -> Speech ->\n"
        "Manage voices -> Add voices. Install one there, then rerun this script -\n"
        "it should show up in the list above."
    )


if __name__ == "__main__":
    main()
