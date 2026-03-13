import asyncio
import base64
import json
import os
import time
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

app = FastAPI()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

MODEL_NAME = os.getenv(
    "LIVE_MODEL",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)

VOICE_NAME = os.getenv("VOICE_NAME", "Kore")

client = genai.Client(api_key=GEMINI_API_KEY)


def infer_topic_from_text(text: str) -> str:
    raw = (text or "").strip()
    lower = raw.lower()

    filler_prefixes = ["oh ", "uh ", "um ", "well ", "so ", "i want to know "]
    for filler in filler_prefixes:
        if lower.startswith(filler):
            raw = raw[len(filler):].strip()
            lower = raw.lower()
            break

    prefixes = [
        "what is ",
        "what are ",
        "who is ",
        "who are ",
        "explain ",
        "tell me about ",
        "how does ",
        "how do ",
        "why is ",
        "why are ",
        "define ",
    ]
    for prefix in prefixes:
        if lower.startswith(prefix):
            return raw[len(prefix):].strip(" ?.")
    return raw.strip(" ?.")


def build_history_summary(history: list[dict]) -> str:
    if not history:
        return "No previous turns yet."

    lines = []
    for i, item in enumerate(history[-2:], start=max(1, len(history) - 1)):
        user_text = item.get("user", "").strip()
        tutor_text = item.get("tutor", "").strip()
        lines.append(f"Turn {i} user: {user_text}")
        lines.append(f"Turn {i} tutor: {tutor_text}")
    return "\n".join(lines)


def build_system_instruction(topic: str, stage: int, history: list[dict]) -> str:
    history_summary = build_history_summary(history)

    base = f"""
You are Voice Study Companion, a voice-first AI tutor.

Topic: {topic if topic else "unknown"}
Current teaching stage: {stage}

Conversation context:
{history_summary}

Global rules:
- Your response will be spoken aloud, so keep it concise and easy to hear.
- Use 2-3 short sentences.
- Sound warm, clear, and encouraging.
- Do not sound like a textbook.
- Do not use bullet points.
- Focus on one teaching move at a time.
- Never dump a long definition immediately unless stage 3 requires a short conclusion.
"""

    if stage == 1:
        return base + """
Stage 1 goal:
- The student is asking the initial question.
- Do NOT answer directly.
- First acknowledge the question briefly.
- Then ask one open-ended question about what the student already knows.
- Encourage the student to guess or think out loud.

Required behavior:
- Sentence 1: brief acknowledgement
- Sentence 2: one open-ended question
- Optional Sentence 3: short encouragement

Do not provide the final definition.
"""

    if stage == 2:
        return base + """
Stage 2 goal:
- The student has given an initial answer.
- Do NOT give the final definition yet.
- Briefly evaluate the student's answer.
- Distinguish between:
  - mostly correct
  - partially correct
  - incorrect
- Then ask one guiding follow-up question that helps the student get closer.

Required behavior:
- Sentence 1: short feedback on the student's answer
- Sentence 2: one guiding question
- Optional Sentence 3: brief encouragement

Do not provide the final definition.
"""

    return base + """
Stage 3 goal:
- The student has already had at least two rounds.
- Briefly evaluate the student's answer.
- Say what is correct and what is missing if needed.
- Then provide a short, clear conclusion or definition.

Required behavior:
- Sentence 1: short feedback
- Sentence 2: short conclusion
- Optional Sentence 3: one reinforcing sentence

Keep the conclusion concise and spoken-friendly.
"""


class LiveSessionBridge:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket

        self.live_cm = None
        self.live_session = None
        self.receiver_task: Optional[asyncio.Task] = None

        self.topic: str = ""
        self.stage: int = 1
        self.turn_count: int = 0
        self.history: list[dict] = []

        self.current_input_transcript: str = ""
        self.current_output_text: str = ""

        self.last_topic_sent: str = ""
        self.last_output_text_sent: str = ""

        self.audio_buffer = bytearray()
        self.last_audio_flush_time = time.monotonic()
        self.audio_flush_bytes = 12000
        self.audio_flush_interval = 0.12

    async def _open_turn_session(self):
        system_instruction = build_system_instruction(
            topic=self.topic,
            stage=self.stage,
            history=self.history,
        )

        config = {
            "response_modalities": ["AUDIO"],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": VOICE_NAME
                    }
                }
            },
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "disabled": True
                }
            },
            "system_instruction": system_instruction,
        }

        self.live_cm = client.aio.live.connect(
            model=MODEL_NAME,
            config=config,
        )
        self.live_session = await self.live_cm.__aenter__()
        self.receiver_task = asyncio.create_task(self._receiver_loop())

    async def _close_turn_session(self):
        if self.receiver_task:
            self.receiver_task.cancel()
            try:
                await self.receiver_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self.receiver_task = None

        if self.live_cm is not None:
            try:
                await self.live_cm.__aexit__(None, None, None)
            except Exception:
                pass

        self.live_cm = None
        self.live_session = None

    async def begin_turn(self):
        self.turn_count += 1
        self.current_input_transcript = ""
        self.current_output_text = ""
        self.last_output_text_sent = ""
        self.audio_buffer = bytearray()

        await self._open_turn_session()
        await self.live_session.send_realtime_input(
            activity_start=types.ActivityStart()
        )

    async def send_audio(self, pcm_bytes: bytes, mime_type: str = "audio/pcm;rate=16000"):
        if self.live_session is None:
            return
        await self.live_session.send_realtime_input(
            audio=types.Blob(
                data=pcm_bytes,
                mime_type=mime_type,
            )
        )

    async def end_turn(self):
        if self.live_session is None:
            return
        await self.live_session.send_realtime_input(
            activity_end=types.ActivityEnd()
        )

    async def reset_topic(self):
        await self._close_turn_session()
        self.topic = ""
        self.stage = 1
        self.turn_count = 0
        self.history = []
        self.current_input_transcript = ""
        self.current_output_text = ""
        self.last_topic_sent = ""
        self.last_output_text_sent = ""
        self.audio_buffer = bytearray()

        await self.websocket.send_text(json.dumps({
            "type": "state",
            "value": "ready"
        }))

    async def close(self):
        await self._close_turn_session()

    async def _flush_audio_buffer(self, force: bool = False):
        now = time.monotonic()

        if not self.audio_buffer:
            return

        should_flush = (
            force
            or len(self.audio_buffer) >= self.audio_flush_bytes
            or (now - self.last_audio_flush_time) >= self.audio_flush_interval
        )

        if should_flush:
            payload = base64.b64encode(bytes(self.audio_buffer)).decode("ascii")
            await self.websocket.send_text(json.dumps({
                "type": "audio",
                "data": payload
            }))
            self.audio_buffer = bytearray()
            self.last_audio_flush_time = now

    async def _receiver_loop(self):
        async for response in self.live_session.receive():
            try:
                content = response.server_content
                if content is None:
                    continue

                # streaming user transcript
                if content.input_transcription:
                    transcript = (content.input_transcription.text or "").strip()
                    if transcript:
                        self.current_input_transcript = transcript

                        if not self.topic:
                            inferred = infer_topic_from_text(transcript)
                            if inferred and len(inferred) >= 4:
                                self.topic = inferred

                        if self.topic and self.topic != self.last_topic_sent:
                            self.last_topic_sent = self.topic
                            await self.websocket.send_text(json.dumps({
                                "type": "topic",
                                "value": self.topic
                            }))

                        await self.websocket.send_text(json.dumps({
                            "type": "state",
                            "value": "thinking"
                        }))

                # streaming tutor transcript
                if content.output_transcription:
                    text = (content.output_transcription.text or "").strip()
                    if text and text != self.last_output_text_sent:
                        self.last_output_text_sent = text
                        self.current_output_text = text
                        await self.websocket.send_text(json.dumps({
                            "type": "text",
                            "value": text
                        }))

                # streaming audio
                if content.model_turn:
                    for part in content.model_turn.parts:
                        if getattr(part, "inline_data", None):
                            audio_bytes = part.inline_data.data
                            self.audio_buffer.extend(audio_bytes)
                            await self._flush_audio_buffer(force=False)

                # turn complete
                if content.generation_complete is True:
                    await self._flush_audio_buffer(force=True)

                    # save history
                    self.history.append({
                        "turn": self.turn_count,
                        "user": self.current_input_transcript,
                        "tutor": self.current_output_text,
                        "stage_used": self.stage,
                    })

                    # advance stage
                    if self.stage == 1:
                        self.stage = 2
                    elif self.stage == 2:
                        self.stage = 3
                    else:
                        self.stage = 3

                    await self.websocket.send_text(json.dumps({
                        "type": "turn_meta",
                        "turn": self.turn_count,
                        "stage_used": self.history[-1]["stage_used"],
                        "next_stage": self.stage,
                        "topic": self.topic,
                    }))

                    await self.websocket.send_text(json.dumps({
                        "type": "state",
                        "value": "ready"
                    }))
                    await self.websocket.send_text(json.dumps({
                        "type": "turn_complete"
                    }))

                    await self._close_turn_session()
                    return

            except Exception as inner_exc:
                await self.websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Receiver loop error: {str(inner_exc)}"
                }))
                return


@app.get("/")
async def root():
    return JSONResponse({
        "status": "ok",
        "service": "live-server",
        "model": MODEL_NAME,
        "voice": VOICE_NAME,
    })


@app.websocket("/live")
async def live_endpoint(websocket: WebSocket):
    await websocket.accept()

    bridge = LiveSessionBridge(websocket)

    try:
        await websocket.send_text(json.dumps({
            "type": "state",
            "value": "ready"
        }))

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "hello":
                await websocket.send_text(json.dumps({
                    "type": "state",
                    "value": "ready"
                }))

            elif msg_type == "start_turn":
                await bridge.begin_turn()
                await websocket.send_text(json.dumps({
                    "type": "state",
                    "value": "listening"
                }))

            elif msg_type == "audio":
                mime_type = msg.get("mime_type", "audio/pcm;rate=16000")
                pcm_bytes = base64.b64decode(msg["data"])
                await bridge.send_audio(pcm_bytes, mime_type=mime_type)

            elif msg_type == "end_turn":
                await bridge.end_turn()
                await websocket.send_text(json.dumps({
                    "type": "state",
                    "value": "thinking"
                }))

            elif msg_type == "reset_topic":
                await bridge.reset_topic()

            else:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}"
                }))

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": str(exc)
            }))
        except Exception:
            pass
    finally:
        await bridge.close()