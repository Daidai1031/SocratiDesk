import asyncio
import base64
import json
import threading

import websockets

from audio import MicStreamer, SpeakerPlayer


WS_URL = "wss://live-server-3234073392.us-central1.run.app/live"


class AssistantState:
    def __init__(self):
        self.topic = ""
        self.recording = False
        self.running = True
        self.turn_in_progress = False
        self.receiving_audio = False
        self.turn_count = 0

        self.partial_tutor_text = ""
        self.last_printed_tutor_text = ""

    def current_turn_label(self) -> str:
        return f"Turn {self.turn_count}"


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
                print(f"[STATE] {value}")

            if value == "ready":
                state.turn_in_progress = False
                state.receiving_audio = False

        elif msg_type == "topic":
            topic = (data.get("value") or "").strip()
            if topic and topic != state.topic:
                state.topic = topic
                print(f"[TOPIC][{state.current_turn_label()}] {state.topic}")

        elif msg_type == "text":
            text = (data.get("value") or "").strip()
            if text:
                state.partial_tutor_text = text

        elif msg_type == "audio":
            pcm_b64 = data.get("data", "")
            pcm_bytes = base64.b64decode(pcm_b64)
            if not state.receiving_audio:
                print(f"[TUTOR][{state.current_turn_label()}] speaking...")
                state.receiving_audio = True
            speaker.add_pcm16(pcm_bytes)

        elif msg_type == "turn_meta":
            turn = data.get("turn")
            stage_used = data.get("stage_used")
            next_stage = data.get("next_stage")
            topic = data.get("topic", "")

            print(f"[META] Turn {turn} | topic={topic} | stage_used={stage_used} | next_stage={next_stage}")

        elif msg_type == "turn_complete":
            if state.partial_tutor_text:
                final_text = state.partial_tutor_text.strip()
                if final_text and final_text != state.last_printed_tutor_text:
                    print(f"[TUTOR][{state.current_turn_label()}] {final_text}")
                    state.last_printed_tutor_text = final_text

            print(f"[TURN] {state.current_turn_label()} complete\n")

            state.turn_in_progress = False
            state.receiving_audio = False
            state.partial_tutor_text = ""

        elif msg_type == "error":
            print(f"[ERROR] {data.get('message', 'Unknown error')}\n")
            state.turn_in_progress = False
            state.receiving_audio = False
            state.partial_tutor_text = ""

        else:
            print(f"[INFO] unknown message: {data}")


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

    print(f"[TURN] {state.current_turn_label()} started")
    print(f"[TOPIC][{state.current_turn_label()}] {state.topic or '(detecting...)'}")
    print("[RECORDING] started... press Enter again to stop")


async def stop_recording(ws, mic: MicStreamer):
    if not state.recording:
        return

    mic.stop()
    state.recording = False

    await send_json(ws, {"type": "end_turn"})
    print("[UPLOAD] end of turn sent")


async def command_loop(ws, mic: MicStreamer, speaker: SpeakerPlayer, keyboard: KeyboardController):
    print("Voice Study Companion")
    print("Press Enter to start speaking.")
    print("Press Enter again to stop speaking.")
    print("Type 'reset' to reset topic.")
    print("Type 'quit' to exit.\n")

    while state.running:
        cmd = await keyboard.get_command()

        if cmd.lower() == "quit":
            state.running = False
            if state.recording:
                await stop_recording(ws, mic)
            break

        if cmd.lower() == "reset":
            if state.recording:
                await stop_recording(ws, mic)
            await send_json(ws, {"type": "reset_topic"})
            state.topic = ""
            state.partial_tutor_text = ""
            state.last_printed_tutor_text = ""
            state.turn_count = 0
            print("[SESSION] reset requested\n")
            continue

        if cmd == "":
            if not state.recording:
                await start_recording(ws, mic, speaker)
            else:
                await stop_recording(ws, mic)
            continue

        print("[INFO] unknown command. Use Enter / reset / quit.\n")


async def run_client():
    loop = asyncio.get_running_loop()
    keyboard = KeyboardController(loop)
    keyboard.start()

    mic = MicStreamer(loop)
    speaker = SpeakerPlayer()
    speaker.start()

    async with websockets.connect(
        WS_URL,
        max_size=10 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await send_json(ws, {
            "type": "hello",
            "device": "raspberry-pi-voice-assistant"
        })

        print("[CONNECTED] websocket connected\n")

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