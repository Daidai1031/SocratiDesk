"""
Quick mic test — run this on Pi to verify:
1. Mic is capturing audio
2. RMS levels (to tune silence threshold)
3. Vosk transcription of what you say

Usage:
    python test_mic.py
    python test_mic.py --vosk ../models/vosk-model-small-en-us-0.15
"""

import argparse
import json
import sys
import time
import threading
import numpy as np

def test_mic_rms(duration=10):
    """Record for N seconds and print RMS every 0.5s."""
    import sounddevice as sd

    RATE = 16000
    CHUNK = 1024
    all_rms = []

    print(f"\n[TEST] Recording for {duration}s — speak normally, then be silent")
    print(f"       Watching RMS levels...\n")

    start = time.time()
    with sd.RawInputStream(samplerate=RATE, blocksize=CHUNK,
                           dtype="int16", channels=1) as stream:
        while time.time() - start < duration:
            data, _ = stream.read(CHUNK)
            arr = np.frombuffer(bytes(data), dtype=np.int16).astype(np.float32)
            rms = int(np.sqrt(np.mean(arr**2)))
            all_rms.append(rms)

            bar = "█" * min(40, rms // 50)
            status = "SOUND" if rms > 300 else "quiet"
            print(f"\r  RMS: {rms:5d}  {bar:<40}  [{status}]", end="", flush=True)
            time.sleep(0.1)

    print(f"\n\n[RESULT]")
    print(f"  Max RMS:  {max(all_rms)}")
    print(f"  Mean RMS: {int(np.mean(all_rms))}")
    print(f"  Min RMS:  {min(all_rms)}")
    print()
    if max(all_rms) < 100:
        print("  ⚠️  PROBLEM: Max RMS very low — mic may not be capturing")
        print("       Try: arecord -l  to check device list")
        print("       Try: export AUDIODEV=hw:1,0  or similar")
    elif max(all_rms) < 300:
        print("  ⚠️  Low signal — lower SILENCE_THRESHOLD in main.py (try 100)")
    else:
        print(f"  ✓ Mic looks good!")
        print(f"  Suggested SILENCE_THRESHOLD for main.py: {int(np.mean(all_rms[:5]) * 2) + 50}")


def test_vosk(model_path, duration=15):
    """Record and transcribe with Vosk."""
    try:
        from vosk import Model, KaldiRecognizer
    except ImportError:
        print("[VOSK] vosk not installed — pip install vosk")
        return

    import sounddevice as sd

    print(f"\n[VOSK] Loading model: {model_path}")
    try:
        model = Model(model_path)
    except Exception as e:
        print(f"[VOSK] Failed to load model: {e}")
        return

    RATE = 16000
    rec = KaldiRecognizer(model, RATE)
    rec.SetWords(True)

    print(f"[VOSK] Listening for {duration}s — say something!\n")
    start = time.time()

    with sd.RawInputStream(samplerate=RATE, blocksize=4000,
                           dtype="int16", channels=1) as stream:
        while time.time() - start < duration:
            data, _ = stream.read(4000)
            arr = np.frombuffer(bytes(data), dtype=np.int16).astype(np.float32)
            rms = int(np.sqrt(np.mean(arr**2)))
            print(f"\r  RMS: {rms:5d}  listening...", end="", flush=True)

            if rec.AcceptWaveform(bytes(data)):
                result = json.loads(rec.Result())
                text = result.get("text", "").strip()
                if text:
                    print(f"\n  [FINAL]   '{text}'")
                    # Check wake words
                    wake_words = ["socrati", "socratic", "socratidesk",
                                  "hey socrati", "hi socrati"]
                    if any(w in text.lower() for w in wake_words):
                        print(f"  ✓ WAKE WORD DETECTED!")
                    else:
                        print(f"  (no wake word in this phrase)")
            else:
                partial = json.loads(rec.PartialResult()).get("partial", "").strip()
                if partial:
                    print(f"\r  [partial] '{partial}'                    ", end="", flush=True)

    print(f"\n\n[VOSK] Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vosk", default=None,
                        help="Path to Vosk model folder (optional)")
    parser.add_argument("--duration", type=int, default=10)
    args = parser.parse_args()

    print("=" * 55)
    print("  SocratiDesk — Microphone Test")
    print("=" * 55)

    # Step 1: RMS test
    test_mic_rms(duration=args.duration)

    # Step 2: Vosk test (if model provided)
    if args.vosk:
        test_vosk(args.vosk, duration=args.duration)
    else:
        print("\n[INFO] To test Vosk transcription, run:")
        print(f"  python test_mic.py --vosk /home/pi/voice-study-companion/models/vosk-model-small-en-us-0.15\n")


if __name__ == "__main__":
    main()