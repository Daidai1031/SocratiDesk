"""
SocratiDesk Live Server — clean rewrite for local debugging
Run locally: GEMINI_API_KEY=xxx uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""

import asyncio, base64, json, os, re, time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

try:
    from google.cloud import storage as gcs
    gcs_client = gcs.Client()
    GCS_BUCKET = os.getenv("GCS_BUCKET", "")
    GCS_ENABLED = bool(GCS_BUCKET)
except Exception:
    gcs_client = None
    GCS_ENABLED = False

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_NAME     = os.getenv("LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
VOICE_NAME     = os.getenv("VOICE_NAME", "Kore")
UPLOAD_DIR     = Path(os.getenv("UPLOAD_DIR", "/tmp/textbooks"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

client = genai.Client(api_key=GEMINI_API_KEY)

# ── Phase constants ──
PHASE_IDLE            = "idle"
PHASE_GREETING        = "greeting"
PHASE_AWAITING_MODE   = "awaiting_mode"
PHASE_AWAITING_UPLOAD = "awaiting_upload"
PHASE_TEXTBOOK_READY  = "textbook_ready"
PHASE_TEXTBOOK        = "textbook"
PHASE_CURIOSITY       = "curiosity"

YES_PATTERNS = ["yes","yeah","yep","yup","sure","i have","i do","got one",
                "have one","have a book","have a textbook","textbook mode",
                # accent/misrecognition variants
                "ja","ya","yа","ye","yes i","affirmative","correct","right",
                "i do have","i've got","got a book","have the book"]
NO_PATTERNS  = ["no","nope","nah","not really","don't have","do not have",
                "no book","no textbook","just curious","curious","without",
                # accent/misrecognition variants
                "nah","nope","na","nein","non","don't","dont","not have",
                "i don't","haven't","no i","no thank"]

def is_yes(t):
    t = t.lower().strip()
    # single word exact match first
    if t in ("yes","yeah","yep","yup","ja","ya","ye","sure"):
        return True
    return any(p in t for p in YES_PATTERNS)

def is_no(t):
    t = t.lower().strip()
    if t in ("no","nope","nah","na","nein","non"):
        return True
    return any(p in t for p in NO_PATTERNS)
def clean_transcript(t): return re.sub(r"<[^>]+>","",t or "").strip().lower()


# ═══════════════════════════════════
# Textbook RAG
# ═══════════════════════════════════

class TextbookStore:
    def __init__(self):
        self.books: dict[str, dict] = {}

    def add_book(self, book_id, name, pages):
        chunks = []
        for p in pages:
            for c in self._chunk(p["text"]):
                chunks.append({"page": p["page"], "text": c})
        self.books[book_id] = {"name": name, "chunks": chunks, "total_pages": len(pages)}
        return len(chunks)

    def remove_book(self, book_id):
        self.books.pop(book_id, None)

    def list_books(self):
        return [{"id": k, "name": v["name"], "chunks": len(v["chunks"]),
                 "pages": v["total_pages"]} for k, v in self.books.items()]

    def search(self, query, top_k=3):
        words = set(re.findall(r"\w+", query.lower()))
        if not words: return []
        scored = []
        for bid, book in self.books.items():
            for chunk in book["chunks"]:
                score = len(words & set(re.findall(r"\w+", chunk["text"].lower())))
                if score: scored.append({**chunk, "book_id": bid,
                                          "book_name": book["name"], "score": score})
        return sorted(scored, key=lambda x: -x["score"])[:top_k]

    @staticmethod
    def _chunk(text, size=500):
        chunks, cur = [], ""
        for para in re.split(r"\n{2,}", text.strip()):
            para = para.strip()
            if not para: continue
            if len(cur) + len(para) + 1 <= size:
                cur = (cur + "\n" + para).strip()
            else:
                if cur: chunks.append(cur)
                cur = para
        if cur: chunks.append(cur)
        return chunks or ([text[:size]] if text.strip() else [])

textbook_store = TextbookStore()


# ═══════════════════════════════════
# System Instructions
# ═══════════════════════════════════

PERSONA = """You are SocratiDesk, a warm encouraging voice-first AI study companion.
Rules: speak conversationally, 2-3 short sentences max, no bullet points,
use contractions, never say "as an AI"."""

def instr_for_phase(phase, stage=1, topic="", history=None, rag=None, book_name=""):
    history = history or []
    rag = rag or []

    if phase == PHASE_GREETING:
        books = textbook_store.list_books()
        book_hint = f"(Textbook '{books[0]['name']}' already uploaded!)" if books else "(No textbook uploaded yet.)"
        return f"""{PERSONA}
{book_hint}
The student just woke you up with a wake word.
Greet them warmly in 1 sentence, then ask: "Do you have a textbook you would like to study with today?"
2 sentences total. Do NOT explain anything else yet."""

    if phase == PHASE_AWAITING_MODE:
        return f"""{PERSONA}
The student is answering yes or no. STAY COMPLETELY SILENT.
Do not say anything. Do not respond. Just listen.
You are in listening-only mode."""

    if phase == PHASE_AWAITING_UPLOAD:
        return f"""{PERSONA}
The student needs to upload a textbook.
Tell them to scan the QR code on the screen to upload their PDF.
Sound encouraging. 2 sentences."""

    if phase == PHASE_TEXTBOOK_READY:
        return f"""{PERSONA}
The textbook "{book_name}" was just uploaded.
The student will now tell you what topic they want to study.
When they speak:
1. Confirm you received the book in 1 sentence.
2. Acknowledge their topic warmly.
3. Guide them to open the book to the relevant page.
Keep it to 2-3 sentences. Be encouraging."""

    if phase == PHASE_CURIOSITY:
        ctx = "\n".join(f"Turn {h['turn']} student: {h['user']}\nTurn {h['turn']} tutor: {h['tutor']}"
                        for h in (history or [])[-3:]) or "No history yet."
        if stage == 1:
            return f"""{PERSONA}
Mode: Curiosity. Topic: {topic or "unknown"}. Stage 1/3.
{ctx}
NEVER give the answer. Ask: "What do you already know about {topic or 'this'}?"
Acknowledge warmly first. 2 sentences."""
        if stage == 2:
            return f"""{PERSONA}
Mode: Curiosity. Topic: {topic}. Stage 2/3.
{ctx}
Give brief feedback, then ask ONE guiding follow-up question. Do NOT give the answer yet. 2 sentences."""
        return f"""{PERSONA}
Mode: Curiosity. Topic: {topic}. Stage 3/3.
{ctx}
Give feedback, then provide a clear concise explanation. 2-3 sentences."""

    if phase == PHASE_TEXTBOOK:
        pages = sorted(set(r["page"] for r in rag)) if rag else []
        page_str = ", ".join(str(p) for p in pages) or "?"
        rag_text = "\n---\n".join(f"[Page {r['page']}] {r['text']}" for r in rag) or "(no content found)"
        ctx = "\n".join(f"Turn {h['turn']} student: {h['user']}\nTurn {h['turn']} tutor: {h['tutor']}"
                        for h in (history or [])[-3:]) or "No history yet."
        first_page = pages[0] if pages else "?"
        if stage == 1:
            return f"""{PERSONA}
Mode: Textbook. Book: "{book_name}". Topic: {topic}. Stage 1/3.
Relevant pages: {page_str}
Content:
{rag_text}
{ctx}
Tell student to open page {first_page}, describe what they'll find, ask them to read and report back. 3 sentences."""
        if stage == 2:
            return f"""{PERSONA}
Mode: Textbook. Book: "{book_name}". Topic: {topic}. Stage 2/3.
Relevant pages: {page_str}
Content:
{rag_text}
{ctx}
Evaluate their understanding vs textbook. Ask ONE guiding question. 2 sentences."""
        return f"""{PERSONA}
Mode: Textbook. Book: "{book_name}". Topic: {topic}. Stage 3/3.
Relevant pages: {page_str}
Content:
{rag_text}
{ctx}
Give clear feedback, summarize key concept citing page {first_page}. 2-3 sentences."""

    # fallback
    return f"{PERSONA}\nHelp the student."


# ═══════════════════════════════════
# Live Session Bridge
# ═══════════════════════════════════

class LiveSessionBridge:
    def __init__(self, ws: WebSocket, device_id: str = "default"):
        self.ws = ws
        self.device_id = device_id

        # State
        self.phase    = PHASE_IDLE
        self.topic    = ""
        self.stage    = 1
        self.history  = []
        self.turn_count = 0

        self.active_book_id   = ""
        self.active_book_name = ""
        self.last_rag         = []

        self.current_user_text  = ""
        self.current_tutor_text = ""
        self.last_tutor_sent    = ""
        self._yes_no_detected   = False
        self._should_close      = False
        self._curiosity_intro_done = False

        # Gemini session
        self._live_cm      = None
        self._live_session = None
        self._recv_task: Optional[asyncio.Task] = None

        # Audio buffer
        self._audio_buf  = bytearray()
        self._last_flush = time.monotonic()

    # ── send to Pi ──
    async def _send(self, payload: dict):
        try:
            await self.ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _flush_audio(self, force=False):
        now = time.monotonic()
        if not self._audio_buf: return
        if force or len(self._audio_buf) >= 12000 or now - self._last_flush >= 0.12:
            await self._send({"type": "audio",
                              "data": base64.b64encode(bytes(self._audio_buf)).decode()})
            self._audio_buf = bytearray()
            self._last_flush = now

    # ── open/close Gemini session ──
    def _build_config(self, system_instruction: str) -> dict:
        return {
            "response_modalities": ["AUDIO"],
            "input_audio_transcription":  {},
            "output_audio_transcription": {},
            "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": VOICE_NAME}}},
            "realtime_input_config": {"automatic_activity_detection": {"disabled": False}},
            "system_instruction": system_instruction,
        }

    async def _open(self, instruction: str):
        self._live_cm = client.aio.live.connect(model=MODEL_NAME,
                                                 config=self._build_config(instruction))
        self._live_session = await self._live_cm.__aenter__()
        self._recv_task = asyncio.create_task(self._recv_loop())
        print(f"[SERVER] Gemini session opened | phase={self.phase}")

    async def _close(self):
        if self._recv_task:
            self._recv_task.cancel()
            try: await self._recv_task
            except: pass
            self._recv_task = None
        if self._live_cm:
            try: await self._live_cm.__aexit__(None, None, None)
            except: pass
        self._live_cm = None
        self._live_session = None

    # ── Gemini receiver loop ──
    async def _recv_loop(self):
        async for resp in self._live_session.receive():
            try:
                sc = resp.server_content
                if sc is None: continue

                if sc.input_transcription:
                    t = (sc.input_transcription.text or "").strip()
                    if t:
                        self.current_user_text = t
                        clean = clean_transcript(t)
                        print(f"[SERVER] user_transcript: {t!r}  phase={self.phase}  "
                              f"is_yes={is_yes(clean)}  is_no={is_no(clean)}")
                        await self._send({"type": "user_transcript", "value": t})
                        await self._handle_transcript(clean)

                if sc.output_transcription:
                    t = (sc.output_transcription.text or "").strip()
                    if t and t != self.last_tutor_sent:
                        self.last_tutor_sent = t
                        self.current_tutor_text = t
                        await self._send({"type": "tutor_transcript", "value": t})
                        # No tutor-output detection for yes/no
                        # Only Vosk and user transcript handle yes/no routing

                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if getattr(part, "inline_data", None):
                            self._audio_buf.extend(part.inline_data.data)
                            await self._flush_audio()

                if sc.generation_complete:
                    await self._flush_audio(force=True)
                    self.history.append({
                        "turn": self.turn_count,
                        "user": self.current_user_text,
                        "tutor": self.current_tutor_text,
                        "phase": self.phase, "stage": self.stage,
                    })
                    # Only advance stage if user actually spoke a topic
                    # Don't advance on intro turn (curiosity stage 1 with no topic yet)
                    is_intro_turn = (self.phase == PHASE_CURIOSITY and 
                                     not self.topic and self.stage == 1)
                    if self.phase in (PHASE_TEXTBOOK, PHASE_CURIOSITY) and self.stage < 3:
                        if not is_intro_turn:
                            self.stage += 1

                    # Phase transitions after turn completes
                    if self.phase == PHASE_GREETING:
                        self.phase = PHASE_AWAITING_MODE
                        print(f"[SERVER] phase → {self.phase}")

                    rag_pages = sorted(set(r["page"] for r in self.last_rag)) if self.last_rag else []

                    # Close session FIRST, then notify Pi (prevents race condition)
                    await self._close()
                    print(f"[SERVER] Turn {self.turn_count} complete, session closed")

                    await self._send({"type": "phase",     "value": self.phase})
                    await self._send({"type": "turn_meta", "turn": self.turn_count,
                                      "phase": self.phase, "stage": self.stage,
                                      "topic": self.topic, "textbook_pages": rag_pages})
                    await self._send({"type": "state",        "value": "ready"})
                    await self._send({"type": "turn_complete"})
                    return

                # Exit cleanly if yes/no was detected mid-turn
                if self._should_close:
                    self._should_close = False
                    return

            except Exception as e:
                if "1000" in str(e) or "ConnectionClosedOK" in str(e):
                    return  # normal close
                if "1000" in str(e) or "ConnectionClosedOK" in str(e):
                    # Normal close, not an error
                    return
                print(f"[SERVER] recv_loop error: {e}")
                await self._send({"type": "error", "message": str(e)})
                return

    async def _handle_transcript(self, clean: str):
        """Phase transitions driven by what user says."""
        if not clean or len(clean) < 2: return

        print(f"[SERVER] _handle_transcript: phase={self.phase!r} clean={clean!r}")

        if self.phase == PHASE_AWAITING_MODE:
            if is_yes(clean):
                await self._route_yes()
                await self._send({"type": "phase", "value": self.phase})
                await self._send({"type": "state", "value": "ready"})
                await self._send({"type": "turn_complete"})
                print(f"[SERVER] transcript yes → turn_complete sent")
                self._should_close = True  # signal _recv_loop to exit cleanly
            elif is_no(clean):
                await self._route_no()
                await self._send({"type": "phase", "value": self.phase})
                await self._send({"type": "state", "value": "ready"})
                await self._send({"type": "turn_complete"})
                print(f"[SERVER] transcript no → turn_complete sent")
                self._should_close = True  # signal _recv_loop to exit cleanly
            else:
                print(f"[SERVER] ambiguous response: {clean!r} — Gemini will handle")
            return

        if self.phase == PHASE_TEXTBOOK_READY:
            topic = self._infer_topic(clean)
            if topic:
                self.topic = topic
                self.stage = 1
                self.phase = PHASE_TEXTBOOK
                await self._send({"type": "topic",  "value": self.topic})
                await self._send({"type": "phase",  "value": self.phase})
            return

        if self.phase == PHASE_CURIOSITY and not self.topic:
            topic = self._infer_topic(clean)
            if topic:
                self.topic = topic
                await self._send({"type": "topic", "value": self.topic})
            return

    @staticmethod
    def _infer_topic(text: str) -> str:
        for prefix in ["what is ","what are ","who is ","explain ","tell me about ",
                       "how does ","how do ","why is ","define "]:
            if text.startswith(prefix):
                return text[len(prefix):].strip(" ?.")
        return text.strip(" ?.") if len(text) >= 3 else ""

    # ── Public API called by WebSocket handler ──

    async def _route_yes(self):
        if self._yes_no_detected: return
        self._yes_no_detected = True
        books = textbook_store.list_books()
        if books:
            self.active_book_id   = books[0]["id"]
            self.active_book_name = books[0]["name"]
            self.phase = PHASE_TEXTBOOK_READY
        else:
            self.phase = PHASE_AWAITING_UPLOAD
            await self._send({"type": "show_qr", "value": True})
        await self._send({"type": "phase", "value": self.phase})
        print(f"[SERVER] routed YES → phase={self.phase}")

    async def _route_no(self):
        if self._yes_no_detected: return
        self._yes_no_detected = True
        self.phase = PHASE_CURIOSITY
        self.stage = 1
        await self._send({"type": "phase", "value": self.phase})
        print(f"[SERVER] routed NO → phase={self.phase}")

    async def begin_turn(self):
        """Start a new Gemini turn. For GREETING/AWAITING_UPLOAD/TEXTBOOK_READY,
        we inject a silent trigger so Gemini speaks first without waiting for user audio."""
        self.turn_count += 1
        self.current_user_text  = ""
        self.current_tutor_text = ""
        self.last_tutor_sent    = ""
        self._yes_no_detected   = False
        self._should_close      = False
        # NOTE: _curiosity_intro_done intentionally NOT reset here
        # It persists across turns within a session
        self._audio_buf = bytearray()

        # Build system instruction
        rag = []
        if self.phase == PHASE_TEXTBOOK and self.topic:
            rag = textbook_store.search(self.topic, top_k=3)
            self.last_rag = rag

        instruction = instr_for_phase(
            phase=self.phase, stage=self.stage, topic=self.topic,
            history=self.history, rag=rag,
            book_name=self.active_book_name,
        )
        print(f"[SERVER] begin_turn #{self.turn_count} phase={self.phase}")
        await self._open(instruction)

        # For phases where Gemini should speak first, send a text trigger.
        SPEAK_FIRST_PHASES = {
            PHASE_GREETING:        "Please greet the student warmly and ask: do you have a textbook you would like to study with today?",
            PHASE_AWAITING_UPLOAD: "Please tell the student to scan the QR code on the screen to upload their textbook PDF.",
            PHASE_CURIOSITY:       "The student chose curiosity mode and has no textbook. Immediately say: No problem! What topic are you curious about today? Be warm and encouraging. 1-2 sentences only.",
        }
        # textbook_ready: user speaks first, Gemini responds to their topic
        # Curiosity intro: only fire ONCE (first turn after entering curiosity mode)
        is_curiosity_intro = (self.phase == PHASE_CURIOSITY and 
                               not self._curiosity_intro_done and
                               not self.topic)
        if is_curiosity_intro:
            self._curiosity_intro_done = True
            print(f"[SERVER] curiosity intro turn (intro_done → True)")
        elif self.phase == PHASE_CURIOSITY:
            print(f"[SERVER] curiosity turn (intro_done={self._curiosity_intro_done}, topic={self.topic!r})")

        should_speak_first = (self.phase in SPEAK_FIRST_PHASES and
                               self.phase != PHASE_CURIOSITY) or is_curiosity_intro
        if should_speak_first:
            await asyncio.sleep(0.3)
            trigger_text = SPEAK_FIRST_PHASES[self.phase]
            print(f"[SERVER] Sending text trigger for phase={self.phase}...")
            if self._live_session is None:
                print(f"[SERVER] ERROR: live_session is None! Cannot send trigger.")
            else:
                try:
                    await self._live_session.send_client_content(
                        turns=[{"role": "user", "parts": [{"text": trigger_text}]}],
                        turn_complete=True,
                    )
                    print(f"[SERVER] Sent text trigger for phase={self.phase}: {trigger_text[:60]}")
                except Exception as e:
                    print(f"[SERVER] send_client_content ERROR: {e}")
                    import traceback; traceback.print_exc()

    async def send_audio(self, pcm: bytes, mime: str = "audio/pcm;rate=16000"):
        if self._live_session:
            await self._live_session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=mime))

    async def end_turn(self):
        if self._live_session:
            await self._live_session.send_realtime_input(
                activity_end=types.ActivityEnd())

    async def set_phase(self, phase: str):
        self.phase = phase
        await self._send({"type": "phase", "value": phase})
        print(f"[SERVER] set_phase → {phase}")

    async def notify_upload(self, book_id: str, book_name: str):
        self.active_book_id   = book_id
        self.active_book_name = book_name
        self.phase = PHASE_TEXTBOOK_READY
        await self._send({"type": "show_qr",           "value": False})
        await self._send({"type": "textbook_received", "name":  book_name})
        await self._send({"type": "phase",             "value": self.phase})
        print(f"[SERVER] textbook uploaded → phase={self.phase}")
        # Pi will receive textbook_received → auto-trigger next turn

    async def reset_topic(self):
        await self._close()
        self.topic = ""; self.stage = 1; self.turn_count = 0
        self.history = []; self.last_rag = []
        await self._send({"type": "state", "value": "ready"})

    async def reset_session(self):
        await self._close()
        self.phase = PHASE_IDLE; self.topic = ""; self.stage = 1
        self.turn_count = 0; self.history = []
        self.active_book_id = ""; self.active_book_name = ""; self.last_rag = []
        self._curiosity_intro_done = False
        await self._send({"type": "state", "value": "ready"})
        await self._send({"type": "phase", "value": self.phase})

    async def close(self):
        await self._close()


# ═══════════════════════════════════
# Active bridges registry
# ═══════════════════════════════════
_bridges: dict[str, LiveSessionBridge] = {}
_device_events: dict[str, asyncio.Queue] = {}


# ═══════════════════════════════════
# HTTP endpoints
# ═══════════════════════════════════

@app.get("/")
async def root():
    return JSONResponse({"status": "ok", "service": "SocratiDesk",
                         "model": MODEL_NAME, "voice": VOICE_NAME,
                         "textbooks": textbook_store.list_books()})

@app.post("/upload-textbook")
async def upload_textbook(file: UploadFile = File(...), device_id: str = "default"):
    filename = file.filename or "unknown"
    suffix   = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".txt", ".md"):
        return JSONResponse({"error": f"Unsupported: {suffix}"}, status_code=400)

    save_path = UPLOAD_DIR / filename
    save_path.write_bytes(await file.read())

    try:
        if suffix == ".pdf":
            if not PDF_SUPPORT:
                return JSONResponse({"error": "pdfplumber not installed"}, status_code=500)
            pages = []
            with pdfplumber.open(str(save_path)) as pdf:
                for i, pg in enumerate(pdf.pages):
                    t = pg.extract_text() or ""
                    if t.strip(): pages.append({"page": i+1, "text": t})
        else:
            pages = [{"page": 1, "text": save_path.read_text(encoding="utf-8", errors="replace")}]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    if not pages:
        return JSONResponse({"error": "No text extracted"}, status_code=400)

    book_id   = re.sub(r"[^a-z0-9_-]", "_", filename.lower())
    n_chunks  = textbook_store.add_book(book_id, filename, pages)

    bridge = _bridges.get(device_id)
    if bridge and bridge.phase == PHASE_AWAITING_UPLOAD:
        await bridge.notify_upload(book_id, filename)

    if device_id in _device_events:
        await _device_events[device_id].put(
            {"type": "textbook_uploaded", "book_id": book_id,
             "name": filename, "pages": len(pages), "chunks": n_chunks})

    return JSONResponse({"status": "ok", "book_id": book_id, "name": filename,
                         "pages": len(pages), "chunks": n_chunks})

@app.get("/textbooks")
async def list_textbooks():
    return JSONResponse({"textbooks": textbook_store.list_books()})

@app.get("/upload", response_class=HTMLResponse)
async def upload_page():
    p = Path(__file__).parent / "upload.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists() else "<h1>upload.html not found</h1>")

_upload_notify_clients: dict[str, list[WebSocket]] = {}

@app.websocket("/upload-notify")
async def upload_notify_ws(websocket: WebSocket, session: str = "default"):
    await websocket.accept()
    _upload_notify_clients.setdefault(session, []).append(websocket)
    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            for ws in _upload_notify_clients.get(session, []):
                if ws is not websocket:
                    try: await ws.send_text(json.dumps(msg))
                    except: pass
            if msg.get("type") == "textbook_uploaded":
                bridge = _bridges.get(session)
                if bridge and bridge.phase == PHASE_AWAITING_UPLOAD:
                    await bridge.notify_upload(msg.get("book_id",""), msg.get("name","textbook"))
    except WebSocketDisconnect:
        pass
    finally:
        _upload_notify_clients[session] = [
            w for w in _upload_notify_clients.get(session,[]) if w is not websocket]


# ═══════════════════════════════════
# Main WebSocket endpoint
# ═══════════════════════════════════

@app.websocket("/live")
async def live_ws(websocket: WebSocket):
    await websocket.accept()
    device_id = websocket.query_params.get("device_id", "default")
    bridge    = LiveSessionBridge(websocket, device_id)
    _bridges[device_id] = bridge
    print(f"[SERVER] Pi connected: device_id={device_id}")

    try:
        await bridge._send({"type": "state",     "value": "ready"})
        await bridge._send({"type": "phase",     "value": bridge.phase})
        await bridge._send({"type": "textbooks", "value": textbook_store.list_books()})

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            t   = msg.get("type")

            if t == "hello":
                print(f"[SERVER] hello from {msg.get('device','?')}")
                await bridge._send({"type": "state", "value": "ready"})
                await bridge._send({"type": "phase", "value": bridge.phase})

            elif t == "start_turn":
                print(f"[SERVER] start_turn received, phase={bridge.phase}")
                try:
                    await bridge.begin_turn()
                    await bridge._send({"type": "state", "value": "listening"})
                    print(f"[SERVER] begin_turn OK")
                except Exception as e:
                    import traceback
                    print(f"[SERVER] begin_turn ERROR: {e}")
                    traceback.print_exc()
                    await bridge._send({"type": "error", "message": str(e)})

            elif t == "audio":
                pcm  = base64.b64decode(msg["data"])
                mime = msg.get("mime_type", "audio/pcm;rate=16000")
                await bridge.send_audio(pcm, mime)

            elif t == "end_turn":
                # For Gemini-speaks-first phases, server already sent ActivityEnd
                # via silence trigger — ignore Pi's end_turn to avoid double-close
                if bridge.phase not in (PHASE_GREETING,
                                          PHASE_AWAITING_UPLOAD, PHASE_TEXTBOOK_READY):
                    await bridge.end_turn()
                await bridge._send({"type": "state", "value": "thinking"})

            elif t == "set_phase":
                await bridge.set_phase(msg.get("phase", PHASE_GREETING))

            elif t == "vosk_answer":
                answer = msg.get("answer", "")
                print(f"[SERVER] vosk_answer: {answer!r} phase={bridge.phase}")
                if bridge.phase == PHASE_AWAITING_MODE:
                    if answer == "yes":
                        await bridge._route_yes()
                    elif answer == "no":
                        await bridge._route_no()
                    # Close Gemini session and notify Pi turn is done
                    await bridge._close()
                    await bridge._send({"type": "phase",        "value": bridge.phase})
                    await bridge._send({"type": "state",        "value": "ready"})
                    await bridge._send({"type": "turn_complete"})
                    print(f"[SERVER] vosk_answer handled → phase={bridge.phase}")

            elif t == "reset_topic":
                await bridge.reset_topic()

            elif t == "reset_session":
                await bridge.reset_session()

            else:
                print(f"[SERVER] unknown msg type: {t}")

    except WebSocketDisconnect:
        print(f"[SERVER] Pi disconnected: {device_id}")
    except Exception as e:
        print(f"[SERVER] error: {e}")
        try: await bridge._send({"type": "error", "message": str(e)})
        except: pass
    finally:
        _bridges.pop(device_id, None)
        await bridge.close()