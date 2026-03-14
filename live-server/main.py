"""
SocratiDesk Live Server — Gemini Live Agent Challenge (Live Agents track)

Key features for competition:
- Gemini Live API with real-time audio streaming
- Two learning modes: Curiosity-Driven + Textbook-Guided
- Textbook-Guided 3-stage: page reference → comprehension check → feedback
- Barge-in support (automatic_activity_detection enabled)
- PDF upload to Google Cloud Storage → text extraction → RAG chunks in Firestore
- Vision-ready: accepts image frames for "see homework" feature
- Google GenAI SDK usage
- Hosted on Google Cloud Run
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

# ── PDF extraction ──
try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

# ── Firestore (optional, falls back to in-memory) ──
try:
    from google.cloud import firestore
    db = firestore.AsyncClient()
    FIRESTORE_ENABLED = True
except Exception:
    db = None
    FIRESTORE_ENABLED = False

# ── Google Cloud Storage (optional, falls back to local) ──
try:
    from google.cloud import storage as gcs
    gcs_client = gcs.Client()
    GCS_BUCKET = os.getenv("GCS_BUCKET", "")
    GCS_ENABLED = bool(GCS_BUCKET)
except Exception:
    gcs_client = None
    GCS_ENABLED = False

app = FastAPI()

# CORS for mobile upload page
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (upload.html, qr.html)
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_NAME = os.getenv("LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
VOICE_NAME = os.getenv("VOICE_NAME", "Kore")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/textbooks"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

client = genai.Client(api_key=GEMINI_API_KEY)


# ═══════════════════════════════════════════
# RAG: Textbook Processing & Storage
# ═══════════════════════════════════════════

class TextbookStore:
    """In-memory textbook store with page-aware chunking.
    Each chunk knows which page it came from, enabling "go to page X" guidance.
    """

    def __init__(self):
        self.books: dict[str, dict] = {}

    def add_book(self, book_id: str, name: str, pages: list[dict]):
        """pages: list of {"page": int, "text": str}"""
        chunks = []
        for page_info in pages:
            page_num = page_info["page"]
            text = page_info["text"]
            page_chunks = self._chunk_text(text, chunk_size=500, overlap=60)
            for chunk in page_chunks:
                chunks.append({
                    "page": page_num,
                    "text": chunk,
                })
        self.books[book_id] = {"name": name, "chunks": chunks, "total_pages": len(pages)}
        return len(chunks)

    def remove_book(self, book_id: str):
        self.books.pop(book_id, None)

    def list_books(self) -> list[dict]:
        return [
            {"id": bid, "name": b["name"], "chunks": len(b["chunks"]), "pages": b["total_pages"]}
            for bid, b in self.books.items()
        ]

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """Keyword search returning page-aware chunks."""
        query_words = set(re.findall(r"\w+", query.lower()))
        if not query_words:
            return []

        scored = []
        for book_id, book in self.books.items():
            for i, chunk in enumerate(book["chunks"]):
                chunk_words = set(re.findall(r"\w+", chunk["text"].lower()))
                overlap = len(query_words & chunk_words)
                if overlap > 0:
                    scored.append({
                        "book_id": book_id,
                        "book_name": book["name"],
                        "page": chunk["page"],
                        "text": chunk["text"],
                        "score": overlap,
                    })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 60) -> list[str]:
        paragraphs = re.split(r"\n{2,}", text.strip())
        chunks, current = [], ""
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(current) + len(para) + 1 <= chunk_size:
                current = (current + "\n" + para).strip()
            else:
                if current:
                    chunks.append(current)
                current = para
        if current:
            chunks.append(current)
        return chunks if chunks else [text[:chunk_size]] if text.strip() else []


textbook_store = TextbookStore()


def extract_pages_from_pdf(filepath: str) -> list[dict]:
    """Extract text per page, preserving page numbers."""
    if not PDF_SUPPORT:
        raise RuntimeError("pdfplumber not installed")
    pages = []
    with pdfplumber.open(filepath) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append({"page": i + 1, "text": text})
    return pages


async def save_to_firestore(book_id: str, name: str, pages: list[dict]):
    """Persist textbook metadata to Firestore for cross-session access."""
    if not FIRESTORE_ENABLED:
        return
    try:
        doc_ref = db.collection("textbooks").document(book_id)
        await doc_ref.set({
            "name": name,
            "total_pages": len(pages),
            "uploaded_at": firestore.SERVER_TIMESTAMP,
        })
        # Store chunks in subcollection
        chunks = textbook_store.books.get(book_id, {}).get("chunks", [])
        batch = db.batch()
        for i, chunk in enumerate(chunks):
            chunk_ref = doc_ref.collection("chunks").document(f"chunk_{i:04d}")
            batch.set(chunk_ref, {
                "page": chunk["page"],
                "text": chunk["text"],
                "index": i,
            })
        await batch.commit()
    except Exception as e:
        print(f"[FIRESTORE] Save failed: {e}")


def upload_to_gcs(filepath: str, filename: str) -> str:
    """Upload PDF to GCS and return the GCS URI."""
    if not GCS_ENABLED:
        return f"local://{filepath}"
    try:
        bucket = gcs_client.bucket(GCS_BUCKET)
        blob = bucket.blob(f"textbooks/{filename}")
        blob.upload_from_filename(filepath)
        return f"gs://{GCS_BUCKET}/textbooks/{filename}"
    except Exception as e:
        print(f"[GCS] Upload failed: {e}")
        return f"local://{filepath}"


# ═══════════════════════════════════════════
# Mode & Topic Detection
# ═══════════════════════════════════════════

LEARNING_MODES = {
    "curiosity": ["curiosity", "free", "general", "explore", "free mode",
                   "curiosity mode", "free learning", "ask anything"],
    "textbook": ["textbook", "book", "guided", "textbook mode", "guided mode",
                  "study mode", "study my book", "study with book"],
}


def detect_mode_from_text(text: str) -> Optional[str]:
    lower = (text or "").lower().strip()
    for mode, keywords in LEARNING_MODES.items():
        for kw in keywords:
            if kw in lower:
                return mode
    return None


def infer_topic_from_text(text: str) -> str:
    raw = (text or "").strip()
    lower = raw.lower()
    filler_prefixes = ["oh ", "uh ", "um ", "well ", "so ", "i want to know ",
                       "can you tell me ", "i'd like to learn about "]
    for filler in filler_prefixes:
        if lower.startswith(filler):
            raw = raw[len(filler):].strip()
            lower = raw.lower()
            break
    prefixes = [
        "what is ", "what are ", "who is ", "who are ", "explain ",
        "tell me about ", "how does ", "how do ", "why is ", "why are ",
        "define ", "what does ", "what's ", "describe ",
    ]
    for prefix in prefixes:
        if lower.startswith(prefix):
            return raw[len(prefix):].strip(" ?.")
    return raw.strip(" ?.")


def build_history_summary(history: list[dict]) -> str:
    if not history:
        return "No previous turns yet."
    lines = []
    for item in history[-3:]:
        t = item.get("turn", "?")
        lines.append(f"Turn {t} student: {item.get('user', '').strip()}")
        lines.append(f"Turn {t} tutor: {item.get('tutor', '').strip()}")
    return "\n".join(lines)


# ═══════════════════════════════════════════
# System Instructions (competition-optimized)
# ═══════════════════════════════════════════

PERSONA_RULES = """
Your name is SocratiDesk. You are a warm, encouraging voice-first AI study companion.
You have a distinct personality:
- Speak like a friendly, patient teacher — never robotic.
- Use the student's name if known.
- Celebrate small wins ("Nice thinking!", "You're getting closer!").
- Keep responses to 2-3 short spoken sentences. No bullet points.
- Sound natural — use contractions, conversational phrasing.
- Never say "as an AI" or "I'm a language model".
"""


def build_mode_selection_instruction() -> str:
    books = textbook_store.list_books()
    book_info = ""
    if books:
        names = ", ".join(b["name"] for b in books)
        book_info = f"\nTextbooks available: {names}"
    return f"""{PERSONA_RULES}

The student just started. Greet them and help choose a mode.
{book_info}

Two modes:
1. Curiosity Mode — explore any topic freely by asking questions.
2. Textbook Mode — study with an uploaded textbook. You'll guide them to specific pages.
{"(A textbook is already uploaded and ready!)" if books else "(No textbook uploaded yet — suggest curiosity mode, or they can upload a book.)"}

Rules:
- Greet warmly in 1 sentence.
- Describe both modes in 1 sentence each.
- Ask which mode. Keep to 3-4 sentences total.
- Do NOT start teaching yet.
"""


def build_curiosity_instruction(topic: str, stage: int, history: list[dict]) -> str:
    ctx = build_history_summary(history)
    base = f"""{PERSONA_RULES}

Mode: Curiosity (free exploration)
Topic: {topic or "unknown"}
Current stage: {stage}
Conversation so far:
{ctx}

Core rule: NEVER give the answer immediately. Guide the student to think.
"""
    if stage == 1:
        return base + """
Stage 1 — Open question:
- Acknowledge the question warmly.
- Ask ONE open question: "What do you already know about [topic]?"
- Encourage them to guess. Do NOT define the concept.
"""
    if stage == 2:
        return base + """
Stage 2 — Guided question:
- Give brief feedback on their answer (correct/partially/not quite).
- Ask ONE guiding follow-up question that hints toward the key concept.
- Do NOT give the full definition yet.
"""
    return base + """
Stage 3 — Conclusion:
- Give brief feedback on their latest answer.
- Provide a clear, concise definition or explanation (2-3 sentences max).
- Optionally ask if they want to explore a related topic.
"""


def build_textbook_instruction(
    topic: str, stage: int, history: list[dict],
    rag_results: list[dict], book_name: str
) -> str:
    ctx = build_history_summary(history)

    # Build page-aware context
    if rag_results:
        pages_mentioned = sorted(set(r["page"] for r in rag_results))
        page_str = ", ".join(str(p) for p in pages_mentioned)
        rag_text = "\n---\n".join(
            f"[Page {r['page']}] {r['text']}" for r in rag_results
        )
    else:
        pages_mentioned = []
        page_str = "unknown"
        rag_text = "(No matching content found in the textbook for this topic.)"

    base = f"""{PERSONA_RULES}

Mode: Textbook-Guided Study
Textbook: "{book_name}"
Topic: {topic or "unknown"}
Relevant pages: {page_str}
Current stage: {stage}

Textbook excerpts:
---
{rag_text}
---

Conversation so far:
{ctx}

Core rule: Guide the student TO their textbook first, then check understanding.
"""
    if stage == 1:
        return base + f"""
Stage 1 — Page reference:
- Tell the student which page(s) to look at: "Open your book to page {pages_mentioned[0] if pages_mentioned else '?'}."
- Briefly describe what they'll find there (e.g., "You'll see a diagram of..." or "There's a section about...").
- Ask them to read that section and then tell you what they understood.
- Say something like: "Take a moment to read it, then tell me what you think."
- Do NOT explain the concept yet. Let the textbook do the teaching.
"""
    if stage == 2:
        return base + """
Stage 2 — Comprehension check:
- The student has read the textbook section and shared their understanding.
- Evaluate what they said against the textbook content.
- Point out what they got right and what's missing.
- Ask ONE guiding question to deepen understanding.
- Reference specific textbook details (figures, key terms, examples from the pages).
- Do NOT give the complete answer yet.
"""
    return base + f"""
Stage 3 — Feedback + summary:
- Give clear feedback on the student's understanding.
- Provide a concise summary of the key concept, drawing from the textbook content.
- Mention specific details from the textbook (page numbers, diagrams, formulas).
- If they understood well, congratulate them and suggest a related topic or the next section.
- If they struggled, gently clarify and suggest re-reading page {pages_mentioned[0] if pages_mentioned else '?'}.
"""


# ═══════════════════════════════════════════
# WebSocket Session Bridge
# ═══════════════════════════════════════════

class LiveSessionBridge:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket

        self.live_cm = None
        self.live_session = None
        self.receiver_task: Optional[asyncio.Task] = None

        # Session state
        self.mode: Optional[str] = None
        self.topic: str = ""
        self.stage: int = 1
        self.turn_count: int = 0
        self.history: list[dict] = []

        # Textbook state
        self.active_book_id: Optional[str] = None
        self.active_book_name: str = ""
        self.last_rag_results: list[dict] = []

        # Transcript tracking
        self.current_input_transcript: str = ""
        self.current_output_text: str = ""
        self.last_topic_sent: str = ""
        self.last_output_text_sent: str = ""

        # Audio buffer for efficient streaming
        self.audio_buffer = bytearray()
        self.last_audio_flush_time = time.monotonic()
        self.audio_flush_bytes = 12000
        self.audio_flush_interval = 0.12

    async def _open_turn_session(self, system_instruction: str):
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
            # Barge-in: enabled! Student can interrupt the tutor naturally.
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "disabled": False
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
            except (asyncio.CancelledError, Exception):
                pass
            self.receiver_task = None

        if self.live_cm is not None:
            try:
                await self.live_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self.live_cm = None
        self.live_session = None

    def _get_system_instruction(self) -> str:
        if self.mode is None:
            return build_mode_selection_instruction()

        if self.mode == "textbook":
            self.last_rag_results = []
            if self.topic and textbook_store.books:
                self.last_rag_results = textbook_store.search(self.topic, top_k=3)
            return build_textbook_instruction(
                topic=self.topic,
                stage=self.stage,
                history=self.history,
                rag_results=self.last_rag_results,
                book_name=self.active_book_name or "uploaded textbook",
            )

        return build_curiosity_instruction(
            topic=self.topic,
            stage=self.stage,
            history=self.history,
        )

    async def begin_turn(self):
        self.turn_count += 1
        self.current_input_transcript = ""
        self.current_output_text = ""
        self.last_output_text_sent = ""
        self.audio_buffer = bytearray()

        instruction = self._get_system_instruction()
        await self._open_turn_session(instruction)

    async def send_audio(self, pcm_bytes: bytes, mime_type: str = "audio/pcm;rate=16000"):
        if self.live_session is None:
            return
        await self.live_session.send_realtime_input(
            audio=types.Blob(data=pcm_bytes, mime_type=mime_type)
        )

    async def send_image(self, image_bytes: bytes, mime_type: str = "image/jpeg"):
        """Send a camera/screen frame for vision-enabled tutoring."""
        if self.live_session is None:
            return
        await self.live_session.send_realtime_input(
            video=types.Blob(data=image_bytes, mime_type=mime_type)
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
        self.last_rag_results = []
        self.current_input_transcript = ""
        self.current_output_text = ""
        self.last_topic_sent = ""
        self.last_output_text_sent = ""
        self.audio_buffer = bytearray()
        await self._send({"type": "state", "value": "ready"})

    async def reset_session(self):
        await self._close_turn_session()
        self.mode = None
        self.topic = ""
        self.stage = 1
        self.turn_count = 0
        self.history = []
        self.active_book_id = None
        self.active_book_name = ""
        self.last_rag_results = []
        self.current_input_transcript = ""
        self.current_output_text = ""
        self.last_topic_sent = ""
        self.last_output_text_sent = ""
        self.audio_buffer = bytearray()
        await self._send({"type": "state", "value": "ready"})
        await self._send({"type": "mode", "value": None})

    async def close(self):
        await self._close_turn_session()

    async def _send(self, payload: dict):
        try:
            await self.websocket.send_text(json.dumps(payload))
        except Exception:
            pass

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
            await self._send({"type": "audio", "data": payload})
            self.audio_buffer = bytearray()
            self.last_audio_flush_time = now

    async def _receiver_loop(self):
        async for response in self.live_session.receive():
            try:
                content = response.server_content
                if content is None:
                    continue

                # ── User transcript ──
                if content.input_transcription:
                    transcript = (content.input_transcription.text or "").strip()
                    if transcript:
                        self.current_input_transcript = transcript

                        # Mode detection
                        if self.mode is None:
                            detected = detect_mode_from_text(transcript)
                            if detected:
                                self.mode = detected
                                if detected == "textbook":
                                    books = textbook_store.list_books()
                                    if books:
                                        self.active_book_id = books[0]["id"]
                                        self.active_book_name = books[0]["name"]
                                await self._send({"type": "mode", "value": self.mode})

                        # Topic detection (only after mode is set)
                        if self.mode and not self.topic:
                            inferred = infer_topic_from_text(transcript)
                            if inferred and len(inferred) >= 3:
                                self.topic = inferred

                        if self.topic and self.topic != self.last_topic_sent:
                            self.last_topic_sent = self.topic
                            await self._send({"type": "topic", "value": self.topic})

                        await self._send({"type": "user_transcript", "value": transcript})
                        await self._send({"type": "state", "value": "thinking"})

                # ── Tutor transcript ──
                if content.output_transcription:
                    text = (content.output_transcription.text or "").strip()
                    if text and text != self.last_output_text_sent:
                        self.last_output_text_sent = text
                        self.current_output_text = text
                        await self._send({"type": "tutor_transcript", "value": text})

                # ── Streaming audio ──
                if content.model_turn:
                    for part in content.model_turn.parts:
                        if getattr(part, "inline_data", None):
                            self.audio_buffer.extend(part.inline_data.data)
                            await self._flush_audio_buffer(force=False)

                # ── Turn complete ──
                if content.generation_complete is True:
                    await self._flush_audio_buffer(force=True)

                    self.history.append({
                        "turn": self.turn_count,
                        "user": self.current_input_transcript,
                        "tutor": self.current_output_text,
                        "stage_used": self.stage,
                        "mode": self.mode,
                    })

                    # Advance stage
                    if self.mode and self.topic and self.stage < 3:
                        self.stage += 1

                    # Send page reference info for textbook mode
                    rag_pages = []
                    if self.last_rag_results:
                        rag_pages = sorted(set(r["page"] for r in self.last_rag_results))

                    await self._send({
                        "type": "turn_meta",
                        "turn": self.turn_count,
                        "mode": self.mode,
                        "stage_used": self.history[-1]["stage_used"],
                        "next_stage": self.stage,
                        "topic": self.topic,
                        "textbook_pages": rag_pages,
                    })

                    await self._send({"type": "state", "value": "ready"})
                    await self._send({"type": "turn_complete"})
                    await self._close_turn_session()
                    return

            except Exception as inner_exc:
                await self._send({
                    "type": "error",
                    "message": f"Receiver error: {str(inner_exc)}"
                })
                return


# ═══════════════════════════════════════════
# HTTP Endpoints
# ═══════════════════════════════════════════

@app.get("/")
async def root():
    return JSONResponse({
        "status": "ok",
        "service": "SocratiDesk",
        "model": MODEL_NAME,
        "voice": VOICE_NAME,
        "features": {
            "firestore": FIRESTORE_ENABLED,
            "gcs": GCS_ENABLED,
            "pdf": PDF_SUPPORT,
            "barge_in": True,
            "vision": True,
        },
        "textbooks": textbook_store.list_books(),
    })


@app.post("/upload-textbook")
async def upload_textbook(file: UploadFile = File(...)):
    """Upload textbook → GCS → extract pages → chunk → Firestore + in-memory RAG."""
    filename = file.filename or "unknown"
    suffix = Path(filename).suffix.lower()

    if suffix not in (".pdf", ".txt", ".md"):
        return JSONResponse({"error": f"Unsupported: {suffix}. Use .pdf or .txt"}, status_code=400)

    # Save locally
    save_path = UPLOAD_DIR / filename
    content = await file.read()
    save_path.write_bytes(content)

    # Upload to GCS
    gcs_uri = upload_to_gcs(str(save_path), filename)

    # Extract pages
    try:
        if suffix == ".pdf":
            pages = extract_pages_from_pdf(str(save_path))
        else:
            text = save_path.read_text(encoding="utf-8", errors="replace")
            pages = [{"page": 1, "text": text}]
    except Exception as e:
        return JSONResponse({"error": f"Extraction failed: {e}"}, status_code=500)

    if not pages:
        return JSONResponse({"error": "No text extracted"}, status_code=400)

    # Store in RAG
    book_id = re.sub(r"[^a-z0-9_-]", "_", filename.lower())
    num_chunks = textbook_store.add_book(book_id, filename, pages)

    # Persist to Firestore
    await save_to_firestore(book_id, filename, pages)

    return JSONResponse({
        "status": "ok",
        "book_id": book_id,
        "name": filename,
        "pages": len(pages),
        "chunks": num_chunks,
        "gcs_uri": gcs_uri,
    })


@app.get("/textbooks")
async def list_textbooks():
    return JSONResponse({"textbooks": textbook_store.list_books()})


@app.delete("/textbook/{book_id}")
async def delete_textbook(book_id: str):
    textbook_store.remove_book(book_id)
    return JSONResponse({"status": "ok", "deleted": book_id})


# ═══════════════════════════════════════════
# Device Notification (QR upload flow)
# ═══════════════════════════════════════════

# In-memory event queues per device (for long-polling)
_device_events: dict[str, asyncio.Queue] = {}


@app.post("/notify-device")
async def notify_device(request: Request):
    """Called by mobile upload page after successful upload.
    Pushes an event to the Pi device's long-poll queue."""
    data = await request.json()
    device_id = data.get("device_id", "")
    if device_id and device_id in _device_events:
        await _device_events[device_id].put(data)
    return JSONResponse({"status": "ok"})


@app.get("/wait-for-upload")
async def wait_for_upload(device_id: str = "socratiDesk-001"):
    """Long-poll endpoint. Pi calls this and blocks until a textbook is uploaded.
    Returns immediately if an event is already queued."""
    if device_id not in _device_events:
        _device_events[device_id] = asyncio.Queue()

    queue = _device_events[device_id]
    try:
        event = await asyncio.wait_for(queue.get(), timeout=55.0)
        return JSONResponse(event)
    except asyncio.TimeoutError:
        return JSONResponse({"event": "timeout"}, status_code=204)


# ═══════════════════════════════════════════
# Mobile Upload Page (QR scan flow)
# ═══════════════════════════════════════════

UPLOAD_HTML_PATH = Path(__file__).parent / "upload.html"


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(session: str = "default"):
    """Serve the mobile upload page. QR code on Pi points here."""
    if UPLOAD_HTML_PATH.exists():
        html = UPLOAD_HTML_PATH.read_text(encoding="utf-8")
    else:
        html = "<h1>Upload page not found</h1><p>Ensure upload.html is deployed alongside main.py</p>"
    return HTMLResponse(html)


# WebSocket hub for upload notifications (Pi subscribes, mobile publishes)
_upload_notify_clients: dict[str, list[WebSocket]] = {}


@app.websocket("/upload-notify")
async def upload_notify_ws(websocket: WebSocket, session: str = "default"):
    """WebSocket for real-time upload notifications.
    - Pi device connects and waits for events.
    - Mobile upload page connects and sends upload-complete events.
    Both sides use the same session ID to match."""
    await websocket.accept()

    if session not in _upload_notify_clients:
        _upload_notify_clients[session] = []
    _upload_notify_clients[session].append(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            # Broadcast to all other clients in this session (Pi gets the notification)
            for ws in _upload_notify_clients.get(session, []):
                if ws is not websocket:
                    try:
                        await ws.send_text(json.dumps(msg))
                    except Exception:
                        pass

            # Also push to long-poll queue as fallback
            if msg.get("type") == "textbook_uploaded":
                device_id = session
                if device_id in _device_events:
                    await _device_events[device_id].put(msg)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if session in _upload_notify_clients:
            _upload_notify_clients[session] = [
                ws for ws in _upload_notify_clients[session] if ws is not websocket
            ]


# ═══════════════════════════════════════════
# WebSocket Endpoint (Main Live Session)
# ═══════════════════════════════════════════

@app.websocket("/live")
async def live_endpoint(websocket: WebSocket):
    await websocket.accept()
    bridge = LiveSessionBridge(websocket)

    try:
        await bridge._send({"type": "state", "value": "ready"})
        await bridge._send({"type": "textbooks", "value": textbook_store.list_books()})

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "hello":
                await bridge._send({"type": "state", "value": "ready"})

            elif msg_type == "set_mode":
                mode = msg.get("mode")
                if mode in ("curiosity", "textbook"):
                    bridge.mode = mode
                    if mode == "textbook":
                        book_id = msg.get("book_id")
                        if book_id and book_id in textbook_store.books:
                            bridge.active_book_id = book_id
                            bridge.active_book_name = textbook_store.books[book_id]["name"]
                        else:
                            books = textbook_store.list_books()
                            if books:
                                bridge.active_book_id = books[0]["id"]
                                bridge.active_book_name = books[0]["name"]
                    await bridge._send({"type": "mode", "value": bridge.mode})

            elif msg_type == "start_turn":
                await bridge.begin_turn()
                await bridge._send({"type": "state", "value": "listening"})

            elif msg_type == "audio":
                mime = msg.get("mime_type", "audio/pcm;rate=16000")
                pcm = base64.b64decode(msg["data"])
                await bridge.send_audio(pcm, mime_type=mime)

            elif msg_type == "image":
                # Vision: accept camera frames
                mime = msg.get("mime_type", "image/jpeg")
                img_bytes = base64.b64decode(msg["data"])
                await bridge.send_image(img_bytes, mime_type=mime)

            elif msg_type == "end_turn":
                await bridge.end_turn()
                await bridge._send({"type": "state", "value": "thinking"})

            elif msg_type == "reset_topic":
                await bridge.reset_topic()

            elif msg_type == "reset_session":
                await bridge.reset_session()

            else:
                await bridge._send({
                    "type": "error",
                    "message": f"Unknown type: {msg_type}"
                })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await bridge._send({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        await bridge.close()