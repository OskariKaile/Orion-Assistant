"""One-time voice enrollment for Orion's speaker verification.

Run this once with:

    python enroll_voice.py

It records THREE short clips (about 5 seconds each), averages them into a
single voice fingerprint, and saves the result to voice_profile.json. After
that, Orion can tell your voice apart from anyone else's. Re-run any time you
want to refresh the profile (after a cold, with a new microphone, etc.).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _paths  # noqa: F401

import speech_recognition as sr

import voice_id

NUM_SAMPLES = 3
SAMPLE_SECONDS = 5


def _record_one(recognizer, source, n):
    print(f"\nSample {n}/{NUM_SAMPLES}: speak naturally for ~{SAMPLE_SECONDS} seconds.")
    for i in range(3, 0, -1):
        print(f"  starting in {i}...", end="\r", flush=True)
        time.sleep(1)
    print("  recording now...        ")
    audio = recognizer.record(source, duration=SAMPLE_SECONDS)
    print("  done.")
    return audio


def main():
    if not voice_id.available():
        print("ERROR: resemblyzer isn't installed. Run:  pip install resemblyzer")
        sys.exit(1)

    print("Orion voice enrollment")
    print("======================")
    print(f"You'll record {NUM_SAMPLES} short samples. Read anything natural --")
    print("a couple of sentences in your normal speaking voice each time.\n")

    recognizer = sr.Recognizer()
    samples = []
    with sr.Microphone() as source:
        print("Calibrating background noise... stay quiet for a moment.")
        recognizer.adjust_for_ambient_noise(source, duration=1.5)
        for i in range(1, NUM_SAMPLES + 1):
            samples.append(_record_one(recognizer, source, i))

    print("\nProcessing voice fingerprint...")
    if voice_id.enroll(samples):
        print("Done. Profile saved to voice_profile.json.")
    else:
        print("ERROR: enrollment failed. Try again in a quieter room.")
        sys.exit(1)


if __name__ == "__main__":
    main()
