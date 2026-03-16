from dotenv import load_dotenv
load_dotenv()

import asyncio
import base64
import json
import os
import re
import sys
import threading
from pathlib import Path

import websockets

from audio import MicStreamer, SpeakerPlayer

# ── Server URL ──
WS_URL = os.getenv(
    "SOCRATIDESK_WS",
    "wss://live-server-3234073392.us-central1.run.app/live",
)
HTTP_URL = os.getenv(
    "SOCRATIDESK_HTTP",
    "https://live-server-3234073392.us-central1.run.app",
)
DEVICE_ID = os.getenv("DEVICE_ID", "socratiDesk-001")

# Vosk model path
VOSK_MODEL_PATH = os.getenv(
    "VOSK_MODEL_PATH",
    str(Path(__file__).parent.parent / "models" / "vosk-model-s"),
)

# ── Phase constants ──
PHASE_IDLE            = "idle"
PHASE_GREETING        = "greeting"
PHASE_AWAITING_MODE   = "awaiting_mode"
PHASE_AWAITING_UPLOAD = "awaiting_upload"
PHASE_TEXTBOOK_READY  = "textbook_ready"
PHASE_TEXTBOOK        = "textbook"
PHASE_CURIOSITY       = "curiosity"

WAKE_WORDS = [
    "socrati", "socratic", "socratidesk",
    "hey socrati", "hi socrati", "hello socrati",
    "hey socratic", "hi socratic",
    "sock righty", "sock rati", "so crazy",
]


# ═══════════════════════════════════════════
# Vosk Wake Word Detector
# ═══════════════════════════════════════════

class VoskWakeWord:
    """Wake word detector that shares audio chunks from MicStreamer.
    Does NOT open its own sounddevice stream — avoids device conflict.
    Call feed(chunk) from mic_sender to pass audio in.
    """

    def __init__(self, model_path: str, wake_callback, response_callback=None):
        self.model_path = model_path
        self.wake_callback = wake_callback
        self.response_callback = response_callback  # called with "yes" or "no"
        self._paused = False
        self._available = False
        self._rec = None
        self._lock = threading.Lock()

    def start(self):
        try:
            from vosk import Model, KaldiRecognizer
            self._model = Model(self.model_path)
            self._rec = KaldiRecognizer(self._model, 16000)
            self._rec.SetWords(False)
            self._KaldiRecognizer = KaldiRecognizer
            self._available = True
            print("[VOSK] Wake word detector ready (sharing mic stream)")
        except Exception as e:
            print(f"[VOSK] Not available: {e}")

    def feed(self, pcm_bytes: bytes, user_is_speaking: bool = False):
        """Called from mic_sender with each audio chunk.
        user_is_speaking=True means mic is recording user (not Gemini output).
        Yes/no detection only fires when user_is_speaking=True."""
        if not self._available or self._paused or self._rec is None:
            return
        with self._lock:
            try:
                if self._rec.AcceptWaveform(pcm_bytes):
                    text = json.loads(self._rec.Result()).get("text", "").lower().strip()
                    if text:
                        if self._is_wake(text):
                            print(f"\n[VOSK] wake: '{text}'")
                            self.wake_callback()
                        elif self.response_callback and user_is_speaking and self._is_yes(text):
                            print(f"\n[VOSK] YES detected locally: '{text}'")
                            self.response_callback("yes")
                        elif self.response_callback and user_is_speaking and self._is_no(text):
                            print(f"\n[VOSK] NO detected locally: '{text}'")
                            self.response_callback("no")
                else:
                    partial = json.loads(self._rec.PartialResult()).get("partial", "").lower().strip()
                    if partial:
                        if self._is_wake(partial):
                            print(f"\n[VOSK] wake(partial): '{partial}'")
                            self.wake_callback()
                            self._rec = self._KaldiRecognizer(self._model, 16000)
                            self._rec.SetWords(False)
                        elif self.response_callback and user_is_speaking and self._is_yes(partial):
                            print(f"\n[VOSK] YES(partial): '{partial}'")
                            self.response_callback("yes")
                        elif self.response_callback and user_is_speaking and self._is_no(partial):
                            print(f"\n[VOSK] NO(partial): '{partial}'")
                            self.response_callback("no")
            except Exception:
                pass

    @staticmethod
    def _is_yes(text):
        words = text.strip().split()
        # ONLY unambiguous first-word YES - never phrases Gemini might say
        return bool(words) and words[0] in (
            "yes","yeah","yep","yup","ja","ya","ye","yea","sure","ok","okay"
        )

    @staticmethod
    def _is_no(text):
        words = text.strip().split()
        # First word is no
        if words and words[0] in ("no","nope","nah","nein","na","not","nah","never"):
            return True
        # Common phrases
        no_phrases = ["don't have","do not have","no book","just curious",
                      "no textbook","without","i don't","haven't got",
                      "no i","not really","no thank","no pdf","no file",
                      "i have no","don't own","no course","curious mode",
                      "free mode","explore","just want to"]
        return any(p in text for p in no_phrases)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._available = False

    @staticmethod
    def _is_wake(text: str) -> bool:
        return any(w in text for w in WAKE_WORDS)


# ═══════════════════════════════════════════
# TFT Display (optional)
# ═══════════════════════════════════════════

class TFTDisplay:
    def __init__(self):
        self.display = None
        self.img = None
        self.draw = None
        self.font_big = None
        self.font_sm = None
        self.spi = None
        self.cs_pin = None
        self.dc_pin = None
        self.backlight = None
        self._available = False
        self._try_init()

    def _try_init(self):
        try:
            import board, digitalio
            from PIL import Image, ImageDraw, ImageFont
            from adafruit_rgb_display import st7789
            self.cs_pin = digitalio.DigitalInOut(board.D5)
            self.dc_pin = digitalio.DigitalInOut(board.D25)
            self.backlight = digitalio.DigitalInOut(board.D22)
            self.backlight.switch_to_output()
            self.backlight.value = True
            self.spi = board.SPI()
            self.display = st7789.ST7789(
                self.spi, cs=self.cs_pin, dc=self.dc_pin, rst=None,
                baudrate=24000000, width=135, height=240, x_offset=53, y_offset=40,
            )
            try:
                self.font_big = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
                self.font_sm = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
            except Exception:
                self.font_big = ImageFont.load_default()
                self.font_sm = self.font_big
            from PIL import Image, ImageDraw
            self.img = Image.new("RGB", (240, 135), (0, 0, 0))
            self.draw = ImageDraw.Draw(self.img)
            self._available = True
            print("[TFT] Display initialized.")
        except Exception as e:
            print(f"[TFT] Not available: {e}")

    def _push(self):
        if self.display and self.img:
            try:
                self.display.image(self.img, 90)
            except Exception:
                pass

    def show_idle(self):
        if not self._available: return
        from PIL import Image, ImageDraw
        self.img = Image.new("RGB", (240, 135), (10, 10, 10))
        self.draw = ImageDraw.Draw(self.img)
        self.draw.text((10, 45), "SocratiDesk", font=self.font_big, fill=(80, 200, 140))
        self.draw.text((10, 68), "Say 'Hey Socrati'", font=self.font_sm, fill=(140, 140, 140))
        self._push()

    def show_status(self, line1: str, line2: str = "", color=(100, 220, 160)):
        if not self._available: return
        from PIL import Image, ImageDraw
        self.img = Image.new("RGB", (240, 135), (10, 10, 10))
        self.draw = ImageDraw.Draw(self.img)
        self.draw.text((10, 8),  "SocratiDesk", font=self.font_big, fill=(80, 200, 140))
        self.draw.line([(10, 28), (230, 28)], fill=(50, 50, 50), width=1)
        self.draw.text((10, 38), line1, font=self.font_sm, fill=color)
        if line2:
            self.draw.text((10, 58), line2, font=self.font_sm, fill=(180, 180, 180))
        self._push()

    def show_qr(self, url: str, label=None):
        if not self._available:
            print(f"[TFT] QR URL: {url}")
            return
        if label is None:
            label = ["Scan to", "upload", "textbook"]
        try:
            import qrcode
            from PIL import Image, ImageDraw
            qr = qrcode.QRCode(version=1,
                               error_correction=qrcode.constants.ERROR_CORRECT_M,
                               box_size=3, border=2)
            qr.add_data(url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            qr_img = qr_img.resize((131, 131), Image.NEAREST)
            self.img = Image.new("RGB", (240, 135), (0, 0, 0))
            self.draw = ImageDraw.Draw(self.img)
            self.img.paste(qr_img, (2, 2))
            tx = 137
            for i, line in enumerate(label[:4]):
                self.draw.text((tx, 8 + i * 16), line, font=self.font_sm, fill=(100, 220, 160))
            self._push()
        except Exception as e:
            print(f"[TFT] QR error: {e}")

    def show_textbook_received(self, name: str):
        short = name[:18] + "..." if len(name) > 18 else name
        self.show_status("Book received!", short, color=(100, 220, 160))

    def clear(self):
        if not self._available: return
        try:
            from PIL import Image
            self.display.image(Image.new("RGB", (240, 135), (0, 0, 0)), 90)
        except Exception:
            pass

    def cleanup(self):
        self.clear()
        try:
            if self.backlight: self.backlight.value = False
        except Exception:
            pass
        for obj in [self.cs_pin, self.dc_pin, self.backlight]:
            try:
                if obj: obj.deinit()
            except Exception:
                pass
        try:
            if self.spi: self.spi.deinit()
        except Exception:
            pass


# ═══════════════════════════════════════════
# State
# ═══════════════════════════════════════════

class AssistantState:
    def __init__(self):
        self.phase = PHASE_IDLE
        self.topic = ""
        self.stage = 1
        self.recording = False
        self.running = True
        self.turn_in_progress = False
        self.receiving_audio = False
        self.turn_count = 0
        self.partial_tutor_text = ""
        self.last_printed_tutor_text = ""
        self.partial_user_text = ""
        self.last_printed_user_text = ""
        self.last_user_transcript = ""
        self.available_textbooks = []
        self.curiosity_intro_done = False
        self.textbook_intro_done = False
        self.topic_complete = False

    def phase_label(self):
        labels = {
            PHASE_IDLE:            "Idle - say 'Hey Socrati'",
            PHASE_GREETING:        "Greeting",
            PHASE_AWAITING_MODE:   "Waiting for yes/no",
            PHASE_AWAITING_UPLOAD: "Waiting for PDF upload",
            PHASE_TEXTBOOK_READY:  "Textbook ready - ask a topic",
            PHASE_TEXTBOOK:        f"Textbook study (stage {self.stage}/4)",
            PHASE_CURIOSITY:       f"Curiosity mode (stage {self.stage}/3)",
        }
        return labels.get(self.phase, self.phase)


state = AssistantState()
tft = TFTDisplay()
_ws_ref = None
_loop_ref = None
_mic_ref = None
_speaker_ref = None
_vosk_ref = None


# ═══════════════════════════════════════════
# Wake word callback
# ═══════════════════════════════════════════

def on_wake_word():
    if state.phase != PHASE_IDLE:
        return
    if state.turn_in_progress or state.recording:
        return
    if _ws_ref is None or _loop_ref is None:
        return
    asyncio.run_coroutine_threadsafe(_trigger_wake(), _loop_ref)


def on_vosk_response(answer: str):
    """Called from Vosk when yes/no detected locally during awaiting_mode."""
    if state.phase != PHASE_AWAITING_MODE:
        return
    if _ws_ref is None or _loop_ref is None:
        return
    print(f"\n  [VOSK-LOCAL] {answer.upper()} detected → stopping mic and notifying server")
    asyncio.run_coroutine_threadsafe(
        _stop_and_answer(_ws_ref, answer),
        _loop_ref
    )


async def _stop_and_answer(ws, answer: str):
    """Stop recording first, then send answer to server."""
    if state.recording:
        state.recording = False
        await send_json(ws, {"type": "end_turn"})
        print(f"  [MIC] Stopped for yes/no answer")
    await asyncio.sleep(0.2)
    await send_json(ws, {"type": "vosk_answer", "answer": answer})


async def _trigger_wake():
    if state.phase != PHASE_IDLE or _ws_ref is None:
        return
    print("\n  Wake word detected - starting greeting...")
    state.phase = PHASE_GREETING
    tft.show_status("Hello!", "Listening...")
    await send_json(_ws_ref, {"type": "set_phase", "phase": PHASE_GREETING})
    await _do_start_recording(_ws_ref)


async def _wait_then_listen(ws):
    """Wait for speaker to finish playing, then start next recording turn."""
    if _speaker_ref is not None:
        waited = 0
        while waited < 10.0:
            qsize = _speaker_ref.byte_queue.qsize()
            pending = len(_speaker_ref.pending)
            if qsize == 0 and pending == 0:
                break
            await asyncio.sleep(0.1)
            waited += 0.1
        # Wait longer after speaker to avoid echo pickup
        await asyncio.sleep(1.5)

    # Don't auto-listen after topic is complete
    if state.topic_complete:
        state.topic_complete = False
        state.phase = PHASE_IDLE
        state.topic = ""
        state.stage = 1
        state.textbook_intro_done = False
        state.curiosity_intro_done = False

        progress_url = f"{HTTP_URL}/progress?device_id={DEVICE_ID}&session={DEVICE_ID}"
        print(f"  [TOPIC DONE] Scan for summary: {progress_url}")

        # Tell server we're going idle (but don't clear session history)
        await send_json(ws, {"type": "set_phase", "phase": PHASE_IDLE})

        # Show "Topic done!" then QR to progress page
        tft.show_status("Great job!", "Scan QR for summary", color=(100, 220, 160))
        await asyncio.sleep(2)
        tft.show_qr(progress_url, label=["Scan for", "your learning", "summary!"])

        # Wait 10 seconds, then go back to idle
        await asyncio.sleep(10)
        tft.show_idle()
        # Resume Vosk so "Hey Socrati" works again
        if _vosk_ref is not None:
            _vosk_ref.resume()
        print(f"  [IDLE] Say 'Hey Socrati' to start a new topic\n")
        return

    print(f"  [AUTO-LISTEN] Audio done, starting next turn for phase={state.phase}")
    await _do_start_recording(ws)


async def _do_start_recording(ws):
    if _mic_ref is None or state.recording or state.turn_in_progress:
        return
    _speaker_ref.clear()
    _mic_ref.clear_queue()
    state.turn_count += 1
    state.partial_tutor_text = ""
    state.partial_user_text = ""
    state.turn_in_progress = True
    await send_json(ws, {"type": "start_turn"})

    # Gemini speaks first in these phases/conditions:
    # - GREETING: always
    # - AWAITING_UPLOAD: always  
    # - TEXTBOOK_READY: only FIRST time (confirm book receipt)
    # - CURIOSITY: only FIRST time (ask what topic)
    is_curiosity_intro = (state.phase == PHASE_CURIOSITY and 
                           not state.topic and 
                           not state.curiosity_intro_done)
    if is_curiosity_intro:
        state.curiosity_intro_done = True

    is_textbook_intro = (state.phase == PHASE_TEXTBOOK_READY and
                          not state.textbook_intro_done)
    if is_textbook_intro:
        state.textbook_intro_done = True

    ALWAYS_SPEAK_FIRST = (PHASE_GREETING, PHASE_AWAITING_UPLOAD)
    if state.phase in ALWAYS_SPEAK_FIRST or is_curiosity_intro or is_textbook_intro:
        print(f"\n  [TURN {state.turn_count}] {state.phase_label()} — waiting for Gemini...")
        return

    # User-voice phases: start recording
    state.recording = True
    print(f"\n{'='*52}")
    print(f"  Turn {state.turn_count} | {state.phase_label()}")
    if state.topic:
        print(f"  Topic: {state.topic}")
    print(f"  Speak now — auto-stops on silence")
    print(f"{'='*52}")

# ═══════════════════════════════════════════
# Keyboard
# ═══════════════════════════════════════════

class KeyboardController:
    def __init__(self, loop):
        self.loop = loop
        self.command_queue = asyncio.Queue()

    def start(self):
        threading.Thread(target=self._input_loop, daemon=True).start()

    def _input_loop(self):
        while True:
            try:
                text = input().strip()
            except EOFError:
                text = "quit"
            asyncio.run_coroutine_threadsafe(self.command_queue.put(text), self.loop)

    async def get_command(self):
        return await self.command_queue.get()


async def send_json(ws, payload):
    await ws.send(json.dumps(payload))


# ═══════════════════════════════════════════
# Receiver
# ═══════════════════════════════════════════

async def receiver(ws, speaker, vosk):
    while state.running:
        msg = await ws.recv()
        if isinstance(msg, bytes):
            speaker.add_pcm16(msg)
            continue

        data = json.loads(msg)
        t = data.get("type")

        if t == "state":
            v = data.get("value", "")
            if v == "ready":
                state.turn_in_progress = False
                state.receiving_audio = False
                if state.phase == PHASE_IDLE:
                    vosk.resume()

        elif t == "phase":
            new = data.get("value", "")
            if new and new != state.phase:
                state.phase = new
                print(f"\n  [PHASE] -> {state.phase_label()}")
                _handle_phase_ui(new)
                if new in (PHASE_IDLE, PHASE_AWAITING_MODE):
                    vosk.resume()
                else:
                    vosk.pause()

        elif t == "textbooks":
            state.available_textbooks = data.get("value", [])
            if state.available_textbooks:
                print(f"  [TEXTBOOKS] {', '.join(b['name'] for b in state.available_textbooks)}")

        elif t == "topic":
            topic = (data.get("value") or "").strip()
            if topic and topic != state.topic:
                state.topic = topic
                print(f"  [TOPIC] {topic}")

        elif t == "show_qr":
            if data.get("value"):
                _show_qr_ui()
            else:
                tft.show_status("Ready to study!", state.topic or "")

        # v3: Server tells Pi to stop recording (e.g. after yes/no detected)
        elif t == "stop_recording":
            if state.recording:
                state.recording = False
                print(f"\n  [STOP] Server requested stop recording")

        elif t == "textbook_received":
            name = data.get("name", "")
            print(f"\n  [UPLOAD] Received: {name}")
            tft.show_textbook_received(name)
            # Brief pause then start recording so server can confirm book
            if _ws_ref is not None:
                asyncio.ensure_future(_wait_then_listen(_ws_ref))

        elif t == "audio":
            pcm = base64.b64decode(data.get("data", ""))
            if not state.receiving_audio:
                print(f"\n  [speaking...]", end="", flush=True)
                state.receiving_audio = True
            speaker.add_pcm16(pcm)

        elif t == "user_transcript":
            raw_text = (data.get("value") or "").strip()
            clean_text = re.sub(r"<[^>]+>", "", raw_text).strip()
            if raw_text and raw_text != state.partial_user_text:
                state.partial_user_text = raw_text
                state.last_user_transcript = clean_text or raw_text
                print(f"\n  YOU:   {state.last_user_transcript}")
                if clean_text and clean_text != raw_text:
                    print(f"  [ASR RAW] {raw_text}")

        elif t == "tutor_transcript":
            text = (data.get("value") or "").strip()
            if text and text != state.partial_tutor_text:
                state.partial_tutor_text = text
                print(f"\r  TUTOR: {text:<60}", end="", flush=True)

        elif t == "turn_meta":
            stage = data.get("stage")
            if stage:
                state.stage = stage
            pages = data.get("textbook_pages", [])
            if pages:
                print(f"\n  [PAGES] {pages}")
            if data.get("topic_complete"):
                state.topic_complete = True
                print(f"  [TOPIC COMPLETE] Socratic dialogue finished")

        elif t == "turn_complete":
            print()
            if state.last_user_transcript:
                user_final = state.last_user_transcript.strip()
                if user_final and user_final != state.last_printed_user_text:
                    print(f"  [USER FINAL] {user_final}")
                    state.last_printed_user_text = user_final
            if state.partial_tutor_text:
                final = state.partial_tutor_text.strip()
                if final and final != state.last_printed_tutor_text:
                    print(f"  +--------------------------------------------------+")
                    words, line = final.split(), ""
                    for word in words:
                        if len(line) + len(word) + 1 > 48:
                            print(f"  | {line:<48} |")
                            line = word
                        else:
                            line = (line + " " + word).strip()
                    if line:
                        print(f"  | {line:<48} |")
                    print(f"  +--------------------------------------------------+\n")
                    state.last_printed_tutor_text = final
            state.turn_in_progress = False
            state.receiving_audio = False
            state.partial_tutor_text = ""
            state.partial_user_text = ""

            # Auto-start next recording AFTER audio finishes playing
            AUTO_LISTEN_PHASES = [
                PHASE_GREETING, PHASE_AWAITING_MODE,
                PHASE_TEXTBOOK_READY, PHASE_TEXTBOOK, PHASE_CURIOSITY,
            ]
            if state.phase in AUTO_LISTEN_PHASES and _ws_ref is not None:
                print(f"  [AUTO-LISTEN] phase={state.phase}")
                asyncio.ensure_future(_wait_then_listen(_ws_ref))
            elif state.phase == PHASE_AWAITING_UPLOAD:
                _show_qr_ui()
                print("  [QR] Showing QR code — waiting for upload...")

        elif t == "error":
            print(f"\n  [ERROR] {data.get('message')}")
            state.turn_in_progress = False
            state.receiving_audio = False


def _handle_phase_ui(phase):
    if phase == PHASE_IDLE:
        tft.show_idle()
    elif phase == PHASE_GREETING:
        tft.show_status("Hello!", "Listening...")
    elif phase == PHASE_AWAITING_MODE:
        tft.show_status("Have a textbook?", "Say yes or no")
    elif phase == PHASE_AWAITING_UPLOAD:
        _show_qr_ui()
    elif phase == PHASE_TEXTBOOK_READY:
        tft.show_status("Textbook loaded!", "What topic?")
    elif phase == PHASE_TEXTBOOK:
        tft.show_status("Textbook mode", f"Stage {state.stage}/4")
    elif phase == PHASE_CURIOSITY:
        tft.show_status("Curiosity mode", f"Stage {state.stage}/3")


def _show_qr_ui():
    url = f"{HTTP_URL}/upload?session={DEVICE_ID}"
    print(f"\n  [QR] {url}")
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=1, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    tft.show_qr(url, label=["Scan to", "upload", "textbook", "Waiting..."])


# ═══════════════════════════════════════════
# Recording
# ═══════════════════════════════════════════

async def start_recording(ws, mic, speaker):
    if state.recording or state.turn_in_progress:
        return
    if state.phase == PHASE_IDLE:
        state.phase = PHASE_GREETING
        tft.show_status("Hello!", "Listening...")
        await send_json(ws, {"type": "set_phase", "phase": PHASE_GREETING})
    speaker.clear()
    mic.clear_queue()
    state.turn_count += 1
    state.partial_tutor_text = ""
    state.partial_user_text = ""
    await send_json(ws, {"type": "start_turn"})
    state.recording = True
    state.turn_in_progress = True
    print(f"\n{'='*52}")
    print(f"  Turn {state.turn_count} | {state.phase_label()}")
    if state.topic:
        print(f"  Topic: {state.topic}")
    print(f"  Press Enter to stop recording")
    print(f"{'='*52}")


async def stop_recording(ws, mic):
    if not state.recording:
        return
    state.recording = False
    await send_json(ws, {"type": "end_turn"})
    print("  [MIC] Stopped - processing...")


# ═══════════════════════════════════════════
# Command loop
# ═══════════════════════════════════════════

async def command_loop(ws, mic, speaker, keyboard):
    print()
    print("  SocratiDesk - just say 'Hey Socrati' to start!")
    print("  (Enter = manual trigger, 'new' = reset, 'quit' = exit)\n")
    tft.show_idle()

    while state.running:
        cmd = await keyboard.get_command()
        lower = cmd.lower().strip()

        if lower == "quit":
            state.running = False
            if state.recording:
                await stop_recording(ws, mic)
            break

        elif lower == "reset":
            if state.recording:
                await stop_recording(ws, mic)
            await send_json(ws, {"type": "reset_topic"})
            state.topic = ""
            state.partial_tutor_text = ""
            state.partial_user_text = ""
            state.last_printed_tutor_text = ""
            state.last_printed_user_text = ""
            state.turn_count = 0
            print("  [RESET] Topic reset.\n")

        elif lower == "new":
            if state.recording:
                await stop_recording(ws, mic)
            await send_json(ws, {"type": "reset_session"})
            state.phase = PHASE_IDLE
            state.topic = ""
            state.stage = 1
            state.partial_tutor_text = ""
            state.partial_user_text = ""
            state.last_printed_tutor_text = ""
            state.last_printed_user_text = ""
            state.turn_count = 0
            state.curiosity_intro_done = False
            state.textbook_intro_done = False
            state.topic_complete = False
            tft.show_idle()
            print("  [RESET] Full reset. Say 'Hey Socrati' to start.\n")

        elif lower == "books":
            if state.available_textbooks:
                for b in state.available_textbooks:
                    print(f"    - {b['name']} ({b.get('pages','?')} pages)")
            else:
                print("  No textbooks uploaded.")
            print()

        elif lower == "":
            if not state.recording:
                await start_recording(ws, mic, speaker)
            else:
                await stop_recording(ws, mic)

        else:
            print(f"  Unknown: '{cmd}'\n")


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

async def mic_sender(ws, mic, vosk):
    """Send mic audio to server + feed Vosk wake word detector.
    Single audio stream shared between Gemini and Vosk.
    Auto-stops on silence when recording.
    """
    import numpy as np
    SILENCE_THRESHOLD = 1500    # Raw RMS below this = silence (your mic noise floor is ~1000)
    SPEECH_THRESHOLD  = 2500    # Raw RMS above this = confirmed speech
    SILENCE_TIMEOUT   = 2.0     # Seconds of silence after speech to auto-stop
    MIN_RECORD_TIME   = 1.5
    MAX_GAIN          = 4.0

    last_sound_time = None
    record_start_time = None
    speech_detected = False

    while state.running:
        chunk = await mic.get_chunk()
        now = asyncio.get_event_loop().time()

        try:
            arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            raw_rms = int(np.sqrt(np.mean(arr ** 2)))
        except Exception:
            raw_rms = 0
            arr = None

        # Apply gain boost ONLY for audio sent to Gemini (not for silence detection)
        send_chunk = chunk
        if arr is not None and state.recording and raw_rms > 0:
            target_rms = 1200.0
            gain = max(1.0, min(MAX_GAIN, target_rms / max(raw_rms, 1)))
            if gain > 1.05:
                boosted = np.clip(arr * gain, -32768, 32767).astype(np.int16)
                send_chunk = boosted.tobytes()

        is_awaiting_mode_recording = (state.recording and 
                                       state.phase == PHASE_AWAITING_MODE and
                                       record_start_time is not None and
                                       (now - record_start_time) > 1.5)
        if not state.turn_in_progress:
            vosk.feed(chunk, user_is_speaking=False)
        elif is_awaiting_mode_recording:
            vosk.feed(chunk, user_is_speaking=True)

        if not state.recording:
            last_sound_time = None
            record_start_time = None
            speech_detected = False
            continue

        if record_start_time is None:
            record_start_time = now
            speech_detected = False
            print(f"  [AUDIO] Recording started, silence={SILENCE_THRESHOLD} speech={SPEECH_THRESHOLD}")

        await send_json(ws, {
            "type": "audio",
            "mime_type": "audio/pcm;rate=16000",
            "data": base64.b64encode(send_chunk).decode("ascii"),
        })

        # Use raw_rms (before gain) for silence detection
        if int(now * 4) % 8 == 0:
            bar = "█" * min(20, raw_rms // 100)
            tag = "SPEECH" if raw_rms > SPEECH_THRESHOLD else ("quiet" if raw_rms < SILENCE_THRESHOLD else "")
            print(f"\r  [RMS={raw_rms:4d}] {bar:<20} {tag}", end="", flush=True)

        if raw_rms > SILENCE_THRESHOLD:
            last_sound_time = now
        if raw_rms > SPEECH_THRESHOLD:
            speech_detected = True
        if last_sound_time is None:
            last_sound_time = now

        elapsed = now - record_start_time
        silence_dur = now - (last_sound_time or now)

        # Only auto-stop AFTER user has actually spoken (speech_detected=True)
        if state.phase != PHASE_AWAITING_MODE:
            if speech_detected and elapsed > MIN_RECORD_TIME and silence_dur >= SILENCE_TIMEOUT:
                print(f"\n  [AUTO-STOP] {silence_dur:.1f}s silence after speech (raw_rms={raw_rms})")
                await stop_recording(ws, mic)
                last_sound_time = None
                record_start_time = None
                speech_detected = False


async def run_client():
    global _ws_ref, _loop_ref, _mic_ref, _speaker_ref, _vosk_ref
    _loop_ref = asyncio.get_running_loop()

    keyboard = KeyboardController(_loop_ref)
    keyboard.start()

    mic = MicStreamer(_loop_ref)
    speaker = SpeakerPlayer()
    speaker.start()
    _mic_ref = mic
    _speaker_ref = speaker

    mic.start()

    vosk = VoskWakeWord(VOSK_MODEL_PATH, on_wake_word, response_callback=on_vosk_response)
    vosk.start()
    _vosk_ref = vosk

    sep = "&" if "?" in WS_URL else "?"
    ws_url = f"{WS_URL}{sep}device_id={DEVICE_ID}"
    print(f"  [CONNECTING] {ws_url}")

    async with websockets.connect(
        ws_url, max_size=10*1024*1024,
        ping_interval=20, ping_timeout=20,
    ) as ws:
        _ws_ref = ws
        await send_json(ws, {"type": "hello", "device": DEVICE_ID})
        print("  [CONNECTED]\n")

        r_task = asyncio.create_task(receiver(ws, speaker, vosk))
        s_task = asyncio.create_task(mic_sender(ws, mic, vosk))

        try:
            await command_loop(ws, mic, speaker, keyboard)
        finally:
            state.running = False
            _ws_ref = None
            if state.recording:
                state.recording = False
            mic.stop()
            speaker.stop()
            vosk.stop()
            tft.cleanup()
            r_task.cancel()
            s_task.cancel()
            for t in [r_task, s_task]:
                try:
                    await t
                except asyncio.CancelledError:
                    pass


def main():
    asyncio.run(run_client())


if __name__ == "__main__":
    main()