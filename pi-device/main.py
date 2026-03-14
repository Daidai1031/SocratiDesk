from dotenv import load_dotenv
load_dotenv()

import asyncio
import base64
import json
import os
import sys
import threading
from pathlib import Path

import websockets

from audio import MicStreamer, SpeakerPlayer

# ── Server URL (override via env) ──
WS_URL = os.getenv(
    "SOCRATIDESK_WS",
    "wss://live-server-azyjn6tlia-uc.a.run.app/live",
)
HTTP_URL = os.getenv(
    "SOCRATIDESK_HTTP",
    "https://live-server-azyjn6tlia-uc.a.run.app",
)


class AssistantState:
    def __init__(self):
        self.mode: str | None = None  # None, "curiosity", "textbook"
        self.topic: str = ""
        self.recording: bool = False
        self.running: bool = True
        self.turn_in_progress: bool = False
        self.receiving_audio: bool = False
        self.turn_count: int = 0

        self.partial_tutor_text: str = ""
        self.last_printed_tutor_text: str = ""
        self.available_textbooks: list[dict] = []

    def current_turn_label(self) -> str:
        mode_tag = f" ({self.mode})" if self.mode else ""
        return f"Turn {self.turn_count}{mode_tag}"


state = AssistantState()


class KeyboardController:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.command_queue: asyncio.Queue[str] = asyncio.Queue()

    def start(self):
        thread = threading.Thread(target=self._input_loop, daemon=True)
        thread.start()

    def _input_loop(self):
        while True:
            try:
                text = input().strip()
            except EOFError:
                text = "quit"
            asyncio.run_coroutine_threadsafe(self.command_queue.put(text), self.loop)

    async def get_command(self) -> str:
        return await self.command_queue.get()


async def send_json(ws, payload: dict):
    await ws.send(json.dumps(payload))


async def upload_textbook(filepath: str) -> dict | None:
    """Upload a textbook to the server via HTTP."""
    try:
        import aiohttp
    except ImportError:
        print("[ERROR] aiohttp not installed. Install with: pip install aiohttp")
        return None

    path = Path(filepath)
    if not path.exists():
        print(f"[ERROR] File not found: {filepath}")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field("file", open(path, "rb"), filename=path.name)
            async with session.post(f"{HTTP_URL}/upload-textbook", data=data) as resp:
                result = await resp.json()
                if resp.status == 200:
                    print(f"[UPLOAD] Textbook uploaded: {result.get('name')} ({result.get('chunks')} chunks)")
                    return result
                else:
                    print(f"[ERROR] Upload failed: {result.get('error', 'unknown')}")
                    return None
    except Exception as e:
        print(f"[ERROR] Upload error: {e}")
        return None


async def mic_sender(ws, mic: MicStreamer):
    while state.running:
        chunk = await mic.get_chunk()
        if not state.recording:
            continue
        await send_json(ws, {
            "type": "audio",
            "mime_type": "audio/pcm;rate=16000",
            "data": base64.b64encode(chunk).decode("ascii"),
        })


async def receiver(ws, speaker: SpeakerPlayer):
    while state.running:
        msg = await ws.recv()

        if isinstance(msg, bytes):
            speaker.add_pcm16(msg)
            continue

        data = json.loads(msg)
        msg_type = data.get("type")

        if msg_type == "state":
            value = data.get("value", "")
            if value:
                print(f"  [STATE] {value}")
            if value == "ready":
                state.turn_in_progress = False
                state.receiving_audio = False

        elif msg_type == "mode":
            mode = data.get("value")
            if mode != state.mode:
                state.mode = mode
                if mode:
                    print(f"  [MODE] Switched to: {mode}")
                else:
                    print(f"  [MODE] Mode reset")

        elif msg_type == "textbooks":
            state.available_textbooks = data.get("value", [])
            if state.available_textbooks:
                print(f"  [TEXTBOOKS] Available: {', '.join(b['name'] for b in state.available_textbooks)}")

        elif msg_type == "topic":
            topic = (data.get("value") or "").strip()
            if topic and topic != state.topic:
                state.topic = topic
                print(f"  [TOPIC] {state.topic}")

        elif msg_type == "text":
            text = (data.get("value") or "").strip()
            if text:
                state.partial_tutor_text = text

        elif msg_type == "audio":
            pcm_b64 = data.get("data", "")
            pcm_bytes = base64.b64decode(pcm_b64)
            if not state.receiving_audio:
                print(f"  [TUTOR] speaking...")
                state.receiving_audio = True
            speaker.add_pcm16(pcm_bytes)

        elif msg_type == "turn_meta":
            turn = data.get("turn")
            stage_used = data.get("stage_used")
            next_stage = data.get("next_stage")
            topic = data.get("topic", "")
            mode = data.get("mode", "")
            print(f"  [META] Turn {turn} | mode={mode} | topic={topic} | stage={stage_used} -> {next_stage}")

        elif msg_type == "turn_complete":
            if state.partial_tutor_text:
                final_text = state.partial_tutor_text.strip()
                if final_text and final_text != state.last_printed_tutor_text:
                    print(f"\n  [TUTOR] {final_text}\n")
                    state.last_printed_tutor_text = final_text

            state.turn_in_progress = False
            state.receiving_audio = False
            state.partial_tutor_text = ""

        elif msg_type == "error":
            print(f"  [ERROR] {data.get('message', 'Unknown error')}")
            state.turn_in_progress = False
            state.receiving_audio = False
            state.partial_tutor_text = ""

        else:
            pass  # ignore unknown


async def start_recording(ws, mic: MicStreamer, speaker: SpeakerPlayer):
    if state.recording:
        return

    speaker.clear()
    mic.clear_queue()

    state.turn_count += 1
    state.partial_tutor_text = ""

    await send_json(ws, {"type": "start_turn"})
    mic.start()

    state.recording = True
    state.turn_in_progress = True

    label = state.current_turn_label()
    print(f"\n{'='*50}")
    print(f"  {label} started")
    if state.mode:
        print(f"  Mode: {state.mode} | Topic: {state.topic or '(detecting...)'}")
    else:
        print(f"  Say 'curiosity mode' or 'textbook mode' to choose.")
    print(f"  Press Enter to stop recording")
    print(f"{'='*50}")


async def stop_recording(ws, mic: MicStreamer):
    if not state.recording:
        return
    mic.stop()
    state.recording = False
    await send_json(ws, {"type": "end_turn"})
    print("  [RECORDING] stopped, processing...")


async def command_loop(ws, mic: MicStreamer, speaker: SpeakerPlayer, keyboard: KeyboardController):
    print()
    print("╔══════════════════════════════════════════╗")
    print("║         SocratiDesk Study Companion      ║")
    print("╠══════════════════════════════════════════╣")
    print("║  Enter     = start/stop recording        ║")
    print("║  reset     = reset topic (keep mode)     ║")
    print("║  new       = full session reset           ║")
    print("║  mode X    = set mode (curiosity/textbook)║")
    print("║  upload F  = upload textbook file         ║")
    print("║  books     = list uploaded textbooks      ║")
    print("║  quit      = exit                         ║")
    print("╚══════════════════════════════════════════╝")
    print()
    print("Press Enter to start speaking.")
    print("The tutor will ask you to choose a learning mode.\n")

    while state.running:
        cmd = await keyboard.get_command()
        lower = cmd.lower().strip()

        if lower == "quit":
            state.running = False
            if state.recording:
                await stop_recording(ws, mic)
            break

        if lower == "reset":
            if state.recording:
                await stop_recording(ws, mic)
            await send_json(ws, {"type": "reset_topic"})
            state.topic = ""
            state.partial_tutor_text = ""
            state.last_printed_tutor_text = ""
            state.turn_count = 0
            print("  [SESSION] Topic reset. Mode kept.\n")
            continue

        if lower == "new":
            if state.recording:
                await stop_recording(ws, mic)
            await send_json(ws, {"type": "reset_session"})
            state.mode = None
            state.topic = ""
            state.partial_tutor_text = ""
            state.last_printed_tutor_text = ""
            state.turn_count = 0
            print("  [SESSION] Full reset. Press Enter to start.\n")
            continue

        if lower.startswith("mode "):
            mode = lower.replace("mode ", "").strip()
            if mode in ("curiosity", "textbook"):
                await send_json(ws, {"type": "set_mode", "mode": mode})
                print(f"  [MODE] Set to: {mode}\n")
            else:
                print("  [INFO] Use: mode curiosity  or  mode textbook\n")
            continue

        if lower.startswith("upload "):
            filepath = cmd[7:].strip()
            await upload_textbook(filepath)
            continue

        if lower == "books":
            if state.available_textbooks:
                print("  [TEXTBOOKS]")
                for b in state.available_textbooks:
                    print(f"    - {b['name']} ({b['chunks']} chunks)")
            else:
                print("  [TEXTBOOKS] No textbooks uploaded.")
            print()
            continue

        if lower == "":
            if not state.recording:
                await start_recording(ws, mic, speaker)
            else:
                await stop_recording(ws, mic)
            continue

        print("  [INFO] Unknown command. Use Enter / reset / new / mode X / upload F / books / quit\n")


async def run_client():
    loop = asyncio.get_running_loop()
    keyboard = KeyboardController(loop)
    keyboard.start()

    mic = MicStreamer(loop)
    speaker = SpeakerPlayer()
    speaker.start()

    print(f"  [CONNECTING] {WS_URL}")

    async with websockets.connect(
        WS_URL,
        max_size=10 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await send_json(ws, {
            "type": "hello",
            "device": "socratiDesk-pi"
        })

        print("  [CONNECTED] WebSocket connected\n")

        receiver_task = asyncio.create_task(receiver(ws, speaker))
        sender_task = asyncio.create_task(mic_sender(ws, mic))

        try:
            await command_loop(ws, mic, speaker, keyboard)
        finally:
            state.running = False
            if state.recording:
                await stop_recording(ws, mic)
            mic.stop()
            speaker.stop()
            receiver_task.cancel()
            sender_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass
            try:
                await sender_task
            except asyncio.CancelledError:
                pass


def main():
    asyncio.run(run_client())


if __name__ == "__main__":
    main()