"""
SocratiDesk Live Server — v4.2
Run: set GEMINI_API_KEY=xxx && python -m uvicorn main:app --host 0.0.0.0 --port 8080
"""

# ══════ VERSION CHECK — you MUST see this on startup ══════
import sys
print("=" * 60)
print("  SocratiDesk Server v4.2  —  FIXED textbook mode prompts")
print("=" * 60, flush=True)
sys.stdout.flush()
# ═══════════════════════════════════════════════════════════

import asyncio, base64, json, os, re, time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

try:
    import pdfplumber; PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

try:
    from google.cloud import storage as gcs
    gcs_client = gcs.Client()
    GCS_BUCKET = os.getenv("GCS_BUCKET", "")
    GCS_ENABLED = bool(GCS_BUCKET)
except Exception:
    gcs_client = None; GCS_ENABLED = False

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_NAME = os.getenv("LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
VOICE_NAME = os.getenv("VOICE_NAME", "Kore")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/textbooks"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

client = genai.Client(api_key=GEMINI_API_KEY)

# Phases
PHASE_IDLE            = "idle"
PHASE_GREETING        = "greeting"
PHASE_AWAITING_MODE   = "awaiting_mode"
PHASE_AWAITING_UPLOAD = "awaiting_upload"
PHASE_TEXTBOOK_READY  = "textbook_ready"
PHASE_TOPIC_CAPTURE   = "topic_capture"
PHASE_TEXTBOOK        = "textbook"
PHASE_CURIOSITY       = "curiosity"

YES_PATTERNS = ["yes","yeah","yep","yup","sure","i have","i do","got one",
                "have one","have a book","have a textbook","textbook mode",
                "ja","ya","ye","yes i","affirmative","correct","right",
                "i do have","i've got","got a book","have the book"]
NO_PATTERNS  = ["no","nope","nah","not really","don't have","do not have",
                "no book","no textbook","just curious","curious","without",
                "nah","na","nein","non","don't","dont","not have",
                "i don't","haven't","no i","no thank"]

def is_yes(t):
    t = t.lower().strip()
    if t in ("yes","yeah","yep","yup","ja","ya","ye","sure"): return True
    return any(p in t for p in YES_PATTERNS)

def is_no(t):
    t = t.lower().strip()
    if t in ("no","nope","nah","na","nein","non"): return True
    return any(p in t for p in NO_PATTERNS)

def clean_transcript(t):
    return re.sub(r"<[^>]+>", "", t or "").strip().lower()


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
                if score:
                    scored.append({**chunk, "book_id": bid, "book_name": book["name"], "score": score})
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
# System Instructions — FIXED 3-STAGE TEXTBOOK MODE
# ═══════════════════════════════════

PERSONA = """You are SocratiDesk, a warm encouraging voice-first AI study companion for students.
Rules:
- Speak conversationally, use short sentences.
- Use contractions and a friendly tone.
- Never say "as an AI" or "as a language model".
- IMPORTANT: Always respond in English only.
- IMPORTANT: Speak at a natural pace. Do NOT rush. Complete ALL your sentences before stopping.
- IMPORTANT: You must finish your ENTIRE response. Do not stop mid-sentence."""

def instr_for_phase(phase, stage=1, topic="", history=None, rag=None, book_name=""):
    history = history or []
    rag = rag or []

    if phase == PHASE_GREETING:
        books = textbook_store.list_books()
        hint = f"(Textbook '{books[0]['name']}' already uploaded!)" if books else "(No textbook uploaded yet.)"
        return f"""{PERSONA}
{hint}
Greet the student warmly in 1 sentence, then ask: "Do you have a textbook you would like to study with today?"
2 sentences total."""

    if phase == PHASE_AWAITING_MODE:
        return f"""{PERSONA}\nSTAY COMPLETELY SILENT. Do not say anything. Listen only."""

    if phase == PHASE_AWAITING_UPLOAD:
        return f"""{PERSONA}\nTell the student to scan the QR code on the screen to upload their PDF. 2 sentences."""

    if phase == PHASE_TEXTBOOK_READY:
        return f"""{PERSONA}
The textbook "{book_name}" was just uploaded.
Confirm receipt enthusiastically in 1 sentence, then ask what topic they'd like to study. 2 sentences total.
Do NOT answer any questions. Do NOT explain any concepts."""

    # ── TOPIC CAPTURE: Gemini is silent listener, only transcribes ──
    if phase == PHASE_TOPIC_CAPTURE:
        return f"""You are a silent listener. The student will tell you their topic.
STAY COMPLETELY SILENT. Do not speak. Do not make any sound. Do not respond.
Just listen. That is all."""

    # ── CURIOSITY MODE (3 stages) ──
    if phase == PHASE_CURIOSITY:
        ctx = "\n".join(f"Student: {h['user']}\nTutor: {h['tutor']}" for h in history[-3:]) or "No history."
        if stage == 1:
            return f"""{PERSONA}
Mode: Curiosity. Topic: {topic or "unknown"}. Stage 1/3.
{ctx}
Acknowledge the topic warmly. NEVER give the answer.
Ask: "What do you already know about {topic or 'this'}?" 2 sentences."""
        if stage == 2:
            return f"""{PERSONA}
Mode: Curiosity. Topic: {topic}. Stage 2/3.
{ctx}
Give brief feedback, then ask ONE guiding follow-up question. Do NOT give the answer yet. 2 sentences."""
        return f"""{PERSONA}
Mode: Curiosity. Topic: {topic}. Stage 3/3.
{ctx}
Give feedback, then provide a clear concise explanation. 2-3 sentences."""

    # ══════════════════════════════════════════════════════
    # TEXTBOOK MODE (3 stages) — FIXED v4.2
    # ══════════════════════════════════════════════════════
    if phase == PHASE_TEXTBOOK:
        pages = sorted(set(r["page"] for r in rag)) if rag else []
        page_str = ", ".join(str(p) for p in pages) or "?"
        rag_text = "\n---\n".join(f"[Page {r['page']}] {r['text']}" for r in rag) or "(no content found)"
        ctx = "\n".join(f"Student: {h['user']}\nTutor: {h['tutor']}" for h in history[-3:]) or "No history."
        first_page = pages[0] if pages else "?"

        if stage == 1:
            # ── Stage 1: Force exact script via TTS-only mode ──
            # Gemini audio model ignores all constraints about not explaining.
            # Solution: Make system instruction a PURE REPEATER.
            # The trigger message contains the exact script to read aloud.
            return f"""You are a voice actor reading a script aloud.
Your ONLY job is to read the user's message out loud, word for word, exactly as written.
Do NOT add anything. Do NOT explain anything. Do NOT improvise.
Just read the script naturally and warmly, like a friendly teacher would say it.
Read it exactly. Nothing more. Nothing less."""

        if stage == 2:
            return f"""{PERSONA}
Mode: Textbook. Book: "{book_name}". Topic: {topic}. Stage 2 of 3.

Textbook content:
{rag_text}

Conversation so far:
{ctx}

The student just told you what they read. Do these 3 things BRIEFLY:
1. FEEDBACK: One sentence on what they got right or missed.
2. EXPLAIN: One sentence explaining the key concept simply.
3. QUESTION: Ask ONE simple yes/no or short-answer question.

Example: "You got the main idea! pH measures acidity on a scale of 0 to 14. Quick question — is lemon juice acidic or basic?"

KEEP IT SHORT — 3 sentences max. The audio WILL cut off if you speak too long."""

        # stage 3
        return f"""{PERSONA}
Mode: Textbook. Book: "{book_name}". Topic: {topic}. Stage 3 of 3.

Conversation so far:
{ctx}

The student just answered your question. Do these 3 things BRIEFLY:
1. If correct: praise them. If wrong: say "Not quite!" and give the right answer.
2. One sentence summary mentioning page {first_page}.
3. Say "Great job! Want to explore another topic?"

KEEP IT SHORT — 3 sentences max."""

    return f"{PERSONA}\nHelp the student."


# ═══════════════════════════════════
# Live Session Bridge
# ═══════════════════════════════════

class LiveSessionBridge:
    def __init__(self, ws: WebSocket, device_id: str = "default"):
        self.ws = ws
        self.device_id = device_id
        self.phase = PHASE_IDLE
        self.topic = ""
        self.stage = 1
        self.history = []
        self.turn_count = 0
        self.active_book_id = ""
        self.active_book_name = ""
        self.last_rag = []
        self.current_user_text = ""
        self.current_tutor_text = ""
        self.last_tutor_sent = ""
        self._yes_no_detected = False
        self._curiosity_intro_done = False
        self._textbook_intro_done = False
        self._activity_started = False
        self._audio_msg_count = 0
        self._accumulated_user_text = ""
        self._topic_intercepted = False
        self._live_cm = None
        self._live_session = None
        self._recv_task: Optional[asyncio.Task] = None
        self._audio_buf = bytearray()
        self._last_flush = time.monotonic()

    async def _send(self, payload: dict):
        try: await self.ws.send_text(json.dumps(payload))
        except: pass

    async def _flush_audio(self, force=False):
        now = time.monotonic()
        if not self._audio_buf: return
        if force or len(self._audio_buf) >= 12000 or now - self._last_flush >= 0.12:
            await self._send({"type": "audio", "data": base64.b64encode(bytes(self._audio_buf)).decode()})
            self._audio_buf = bytearray()
            self._last_flush = now

    def _build_config(self, si: str) -> dict:
        return {
            "response_modalities": ["AUDIO"],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": VOICE_NAME}}},
            "realtime_input_config": {"automatic_activity_detection": {"disabled": True}},
            "system_instruction": si,
        }

    async def _open(self, instruction: str):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self._live_cm = client.aio.live.connect(model=MODEL_NAME, config=self._build_config(instruction))
                self._live_session = await self._live_cm.__aenter__()
                self._recv_task = asyncio.create_task(self._recv_loop())
                self._activity_started = False
                print(f"[v4.2] Gemini session opened | phase={self.phase} stage={self.stage}")
                return
            except Exception as e:
                print(f"[v4.2] _open attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"[v4.2] retrying in {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    print(f"[v4.2] _open failed after {max_retries} attempts")
                    await self._send({"type": "error", "message": f"Gemini connection failed: {e}"})
                    raise

    async def _close(self):
        current = asyncio.current_task()
        if self._recv_task and self._recv_task is not current:
            self._recv_task.cancel()
            try: await self._recv_task
            except: pass
        self._recv_task = None
        if self._live_cm:
            try: await self._live_cm.__aexit__(None, None, None)
            except: pass
        self._live_cm = None
        self._live_session = None
        self._activity_started = False

    # ── Gemini receive loop ──
    async def _recv_loop(self):
        try:
            async for resp in self._live_session.receive():
                try:
                    sc = resp.server_content
                    if sc is None: continue

                    if sc.input_transcription:
                        t = (sc.input_transcription.text or "").strip()
                        if t:
                            if self._accumulated_user_text and not self._accumulated_user_text.endswith(" "):
                                self._accumulated_user_text += " "
                            self._accumulated_user_text += t
                            self.current_user_text = self._accumulated_user_text
                            await self._send({"type": "user_transcript", "value": self._accumulated_user_text})
                            # Yes/no detection for awaiting_mode
                            if self.phase == PHASE_AWAITING_MODE and not self._yes_no_detected:
                                full = clean_transcript(self._accumulated_user_text)
                                if is_yes(full):
                                    print(f"[v4.2] transcript YES: {full!r}")
                                    await self._handle_yes_no("yes")
                                    return
                                elif is_no(full):
                                    print(f"[v4.2] transcript NO: {full!r}")
                                    await self._handle_yes_no("no")
                                    return


                    if sc.output_transcription:
                        t = (sc.output_transcription.text or "").strip()
                        if t and t != self.last_tutor_sent:
                            self.last_tutor_sent = t
                            if self.current_tutor_text and not self.current_tutor_text.endswith(" "):
                                self.current_tutor_text += " "
                            self.current_tutor_text += t
                            await self._send({"type": "tutor_transcript", "value": self.current_tutor_text})

                    if sc.model_turn:
                        for part in sc.model_turn.parts:
                            if getattr(part, "inline_data", None):
                                self._audio_buf.extend(part.inline_data.data)
                                await self._flush_audio()

                    if sc.generation_complete:
                        print(f"[v4.2] generation_complete turn {self.turn_count}, user={self._accumulated_user_text!r}")
                        await self._flush_audio(force=True)

                        # ── IMPROVED topic inference ──
                        full = clean_transcript(self._accumulated_user_text)
                        if self.phase == PHASE_CURIOSITY and not self.topic and full:
                            self.topic = self._infer_topic(full)
                            if self.topic:
                                await self._send({"type": "topic", "value": self.topic})
                                print(f"[v4.2] curiosity topic: {self.topic!r}")

                        # topic_capture → extract topic → textbook stage 1
                        elif self.phase == PHASE_TOPIC_CAPTURE and full:
                            raw_topic = self._infer_topic(full)
                            if raw_topic and len(raw_topic) >= 2:
                                self.topic = raw_topic
                            else:
                                self.topic = full.strip(" ?.!,")
                            if self.topic:
                                self.stage = 1
                                self.phase = PHASE_TEXTBOOK
                                self.last_rag = textbook_store.search(self.topic, top_k=3)
                                await self._send({"type": "topic", "value": self.topic})
                                await self._send({"type": "phase", "value": self.phase})
                                print(f"[v4.2] topic_capture → topic={self.topic!r}, RAG={len(self.last_rag)} chunks")

                        self.history.append({
                            "turn": self.turn_count,
                            "user": self._accumulated_user_text,
                            "tutor": self.current_tutor_text,
                            "phase": self.phase, "stage": self.stage,
                        })

                        # Stage advancement
                        is_intro = (self.phase == PHASE_CURIOSITY and not self.topic and self.stage == 1)
                        max_stage = 3
                        was_final = (self.stage == max_stage and
                                     self.phase in (PHASE_TEXTBOOK, PHASE_CURIOSITY) and
                                     not is_intro and self._accumulated_user_text.strip())
                        if self.phase in (PHASE_TEXTBOOK, PHASE_CURIOSITY) and self.stage < max_stage:
                            if not is_intro:
                                self.stage += 1

                        if self.phase == PHASE_GREETING:
                            self.phase = PHASE_AWAITING_MODE

                        # After textbook_ready speak-first, go to topic_capture
                        if self.phase == PHASE_TEXTBOOK_READY:
                            self.phase = PHASE_TOPIC_CAPTURE
                            print(f"[v4.2] textbook_ready done → topic_capture")

                        rag_pages = sorted(set(r["page"] for r in self.last_rag)) if self.last_rag else []
                        await self._close()
                        print(f"[v4.2] Turn {self.turn_count} done{' (TOPIC DONE)' if was_final else ''} "
                              f"stage→{self.stage}")

                        # Record completed topic for progress dashboard
                        if was_final and self.topic:
                            mode = "textbook" if self.phase == PHASE_TEXTBOOK else "curiosity"
                            session_store.record_topic(
                                self.device_id, self.topic, mode, list(self.history),
                                pages=rag_pages, book_name=self.active_book_name)
                            print(f"[v4.2] recorded topic '{self.topic}' to session store")

                        await self._send({"type": "phase", "value": self.phase})
                        await self._send({"type": "turn_meta", "turn": self.turn_count,
                                          "phase": self.phase, "stage": self.stage,
                                          "topic": self.topic, "textbook_pages": rag_pages,
                                          "topic_complete": was_final})
                        await self._send({"type": "state", "value": "ready"})
                        await self._send({"type": "turn_complete"})
                        return

                except Exception as e:
                    if "1000" in str(e) or "ConnectionClosedOK" in str(e): return
                    print(f"[v4.2] recv error: {e}")
                    import traceback; traceback.print_exc()
                    await self._send({"type": "error", "message": str(e)})
                    return
        except asyncio.CancelledError: raise
        except Exception as e:
            print(f"[v4.2] recv outer error: {e}")
            import traceback; traceback.print_exc()

    @staticmethod
    def _infer_topic(text: str) -> str:
        """Extract the core topic from user's spoken text.
        Handles fragmented ASR robustly: 'wha t is p H va lu e' → 'ph value'.

        Strategy: collapse the text into a space-free string, then regex match
        known question prefixes to strip them. This bypasses ASR fragmentation
        entirely because 'wha t is' and 'what is' both collapse to 'whatis'."""
        text = text.strip().lower()
        if not text:
            return ""

        # ── STEP 1: Build a collapsed (no-space) version for prefix matching ──
        collapsed = re.sub(r"\s+", "", text)  # "wha t is p h va lu e" → "whatisphvalue"
        print(f"[TOPIC] collapsed: {collapsed!r}")

        # ── STEP 2: Strip question prefixes from the collapsed form ──
        prefix_patterns = [
            r"^iwanttoknowwhatis",
            r"^iwanttoknowwhatare",
            r"^iwanttoknowabout",
            r"^iwanttoknow",
            r"^wanttoknowwhatis",
            r"^wanttoknowabout",
            r"^wanttoknow",
            r"^iwanttolearn",
            r"^idliketolearnabout",
            r"^teachmeabout",
            r"^letslearnabout",
            r"^tellmemoreabout",
            r"^tellmeabout",
            r"^canyouexplain",
            r"^canyoutellmeabout",
            r"^imcuriousabout",
            r"^whatabout",
            r"^whatis",
            r"^whatare",
            r"^whois",
            r"^whoare",
            r"^explain",
            r"^howdoes",
            r"^howdo",
            r"^whyis",
            r"^whyare",
            r"^define",
            r"^describe",
        ]

        topic_collapsed = collapsed
        for pat in prefix_patterns:
            m = re.match(pat, topic_collapsed)
            if m:
                topic_collapsed = topic_collapsed[m.end():]
                break

        # Remove leading/trailing filler
        for filler in ["uh", "um", "yeah", "so", "like", "well", "okay", "ok"]:
            if topic_collapsed.startswith(filler) and len(topic_collapsed) > len(filler) + 2:
                topic_collapsed = topic_collapsed[len(filler):]

        if not topic_collapsed or len(topic_collapsed) < 2:
            return ""

        # ── STEP 3: Re-insert spaces using known vocabulary ──
        # Match known science terms first, then split remaining by common words
        known_terms = {
            "phvalue": "ph value", "phevalue": "ph value", "phevalue": "ph value",
            "ph": "ph", "phscale": "ph scale",
            "dna": "dna", "rna": "rna", "atp": "atp",
            "co2": "co2", "h2o": "h2o", "o2": "o2",
            "newton": "newton", "newtonslaws": "newton's laws",
            "photosynthesis": "photosynthesis",
            "atomicstructure": "atomic structure",
            "statesofmatter": "states of matter",
            "plantcell": "plant cell", "cellstructure": "cell structure",
            "boilingpoint": "boiling point", "meltingpoint": "melting point",
            "mammals": "mammals", "mammal": "mammal",
            "volcano": "volcano", "volcanoes": "volcanoes",
            "gravity": "gravity", "acceleration": "acceleration",
            "inertia": "inertia", "force": "force",
            "electron": "electron", "proton": "proton", "neutron": "neutron",
            "atom": "atom", "atoms": "atoms", "molecule": "molecule",
            "chloroplast": "chloroplast", "mitochondria": "mitochondria",
            "nucleus": "nucleus", "vacuole": "vacuole",
            "acid": "acid", "base": "base", "neutral": "neutral",
            "indicator": "indicator", "neutralization": "neutralization",
        }

        result = topic_collapsed
        for collapsed_term, spaced_term in sorted(known_terms.items(), key=lambda x: -len(x[0])):
            if result == collapsed_term or result.startswith(collapsed_term):
                result = spaced_term
                remaining = topic_collapsed[len(collapsed_term):]
                if remaining:
                    result += " " + remaining
                break

        # If no known term matched, just return the collapsed string as-is
        # (it's still better than the fragmented version)
        result = result.strip(" ?.!,")

        # Remove trailing filler
        for suffix in [" something", " stuff", " thing", " please"]:
            if result.endswith(suffix):
                result = result[:-len(suffix)].strip()

        print(f"[TOPIC] result: {result!r}")
        return result if len(result) >= 2 else ""

    async def _route_yes(self):
        if self._yes_no_detected: return
        self._yes_no_detected = True
        books = textbook_store.list_books()
        if books:
            self.active_book_id = books[0]["id"]
            self.active_book_name = books[0]["name"]
            self.phase = PHASE_TEXTBOOK_READY
        else:
            self.phase = PHASE_AWAITING_UPLOAD
            await self._send({"type": "show_qr", "value": True})
        await self._send({"type": "phase", "value": self.phase})
        print(f"[v4.2] YES → {self.phase}")

    async def _route_no(self):
        if self._yes_no_detected: return
        self._yes_no_detected = True
        self.phase = PHASE_CURIOSITY; self.stage = 1
        await self._send({"type": "phase", "value": self.phase})
        print(f"[v4.2] NO → {self.phase}")

    async def _handle_yes_no(self, answer: str):
        if self.phase != PHASE_AWAITING_MODE or self._yes_no_detected: return
        print(f"[v4.2] yes/no: {answer!r}")
        if answer == "yes": await self._route_yes()
        elif answer == "no": await self._route_no()
        else: return
        await self._close()
        await self._send({"type": "stop_recording"})
        await self._send({"type": "phase", "value": self.phase})
        await self._send({"type": "state", "value": "ready"})
        await self._send({"type": "turn_complete"})
        print(f"[v4.2] yes/no handled → {self.phase}")

    async def begin_turn(self):
        self.turn_count += 1
        self.current_user_text = ""
        self.current_tutor_text = ""
        self.last_tutor_sent = ""
        self._yes_no_detected = False
        self._activity_started = False
        self._audio_buf = bytearray()
        self._audio_msg_count = 0
        self._accumulated_user_text = ""
        self._topic_intercepted = False

        rag = []
        if self.phase == PHASE_TEXTBOOK and self.topic:
            rag = textbook_store.search(self.topic, top_k=3)
            self.last_rag = rag
            print(f"[v4.2] RAG search for '{self.topic}': {len(rag)} chunks found")
            for r in rag:
                print(f"  [RAG] page {r['page']}: {r['text'][:80]}...")

        instr = instr_for_phase(self.phase, self.stage, self.topic,
                                self.history, rag, self.active_book_name)
        print(f"[v4.2] begin_turn #{self.turn_count} phase={self.phase} stage={self.stage}")
        print(f"[v4.2] system instruction preview: {instr[:200]}...")
        await self._open(instr)

        # ── Speak-first logic ──
        # Textbook Stage 1 is ALSO speak-first: we already know the topic from
        # the textbook_ready turn, so we trigger Gemini to say the page direction
        # without waiting for user audio (which would cause Gemini to explain).
        is_textbook_stage1 = (self.phase == PHASE_TEXTBOOK and self.stage == 1 and self.topic)

        rag_pages = sorted(set(r["page"] for r in self.last_rag)) if self.last_rag else []
        first_page = rag_pages[0] if rag_pages else "?"

        TRIGGERS = {
            PHASE_GREETING:       "Greet the student warmly and ask: do you have a textbook?",
            PHASE_AWAITING_UPLOAD:"Tell the student to scan the QR code to upload their textbook PDF.",
            PHASE_TEXTBOOK_READY: f'The textbook "{self.active_book_name}" was uploaded. Confirm and ask what topic to study.',
            PHASE_CURIOSITY:      "Say: No problem! What topic are you curious about today?",
        }

        # Special trigger for textbook stage 1 — EXACT SCRIPT to read aloud
        if is_textbook_stage1:
            TRIGGERS[PHASE_TEXTBOOK] = (
                f'Read this script out loud exactly: '
                f'"Great question! Let\'s find the answer together in your textbook. '
                f'Please open your book to page {first_page}. '
                f'Take a moment to read that section about {self.topic}, '
                f'and then tell me what you learned!"'
            )

        is_curiosity_intro = (self.phase == PHASE_CURIOSITY and not self._curiosity_intro_done and not self.topic)
        if is_curiosity_intro: self._curiosity_intro_done = True

        is_textbook_intro = (self.phase == PHASE_TEXTBOOK_READY and not self._textbook_intro_done)
        if is_textbook_intro: self._textbook_intro_done = True

        speak_first = (self.phase in (PHASE_GREETING, PHASE_AWAITING_UPLOAD)
                       or is_curiosity_intro or is_textbook_intro
                       or is_textbook_stage1)
        print(f"[v4.2] speak_first={speak_first} is_textbook_stage1={is_textbook_stage1}")

        if speak_first:
            await asyncio.sleep(0.3)
            trigger = TRIGGERS.get(self.phase, "Help the student.")
            if self._live_session:
                try:
                    await self._live_session.send_client_content(
                        turns=[{"role": "user", "parts": [{"text": trigger}]}], turn_complete=True)
                    print(f"[v4.2] sent trigger: {trigger[:50]}")
                except Exception as e:
                    print(f"[v4.2] trigger error: {e}")
        else:
            print(f"[v4.2] waiting for user audio")

    async def send_audio(self, pcm: bytes, mime: str = "audio/pcm;rate=16000"):
        self._audio_msg_count += 1
        if self._audio_msg_count <= 3 or self._audio_msg_count % 100 == 0:
            print(f"[v4.2] audio #{self._audio_msg_count} session={'OK' if self._live_session else 'NONE'}")
        if not self._live_session: return
        if not self._activity_started:
            self._activity_started = True
            try:
                await self._live_session.send_realtime_input(activity_start=types.ActivityStart())
                print(f"[v4.2] >>> ActivityStart")
            except Exception as e:
                print(f"[v4.2] ActivityStart err: {e}")
        try:
            await self._live_session.send_realtime_input(audio=types.Blob(data=pcm, mime_type=mime))
        except Exception as e:
            if self._audio_msg_count <= 5: print(f"[v4.2] audio send err: {e}")

    async def end_turn(self):
        print(f"[v4.2] end_turn() session={'OK' if self._live_session else 'NONE'} activity={self._activity_started}")
        if self._live_session and self._activity_started:
            try:
                await self._live_session.send_realtime_input(activity_end=types.ActivityEnd())
                print(f"[v4.2] >>> ActivityEnd")
            except Exception as e:
                print(f"[v4.2] ActivityEnd err: {e}")
            self._activity_started = False

    async def set_phase(self, phase: str):
        self.phase = phase
        await self._send({"type": "phase", "value": phase})
        print(f"[v4.2] set_phase → {phase}")

    async def notify_upload(self, book_id: str, book_name: str):
        self.active_book_id = book_id
        self.active_book_name = book_name
        self.phase = PHASE_TEXTBOOK_READY
        await self._send({"type": "show_qr", "value": False})
        await self._send({"type": "textbook_received", "name": book_name})
        await self._send({"type": "phase", "value": self.phase})
        print(f"[v4.2] textbook uploaded → {self.phase}")

    async def reset_topic(self):
        await self._close()
        self.topic = ""; self.stage = 1; self.turn_count = 0; self.history = []; self.last_rag = []
        await self._send({"type": "state", "value": "ready"})

    async def reset_session(self):
        await self._close()
        self.phase = PHASE_IDLE; self.topic = ""; self.stage = 1
        self.turn_count = 0; self.history = []; self.last_rag = []
        self.active_book_id = ""; self.active_book_name = ""
        self._curiosity_intro_done = False; self._textbook_intro_done = False
        self._activity_started = False
        await self._send({"type": "state", "value": "ready"})
        await self._send({"type": "phase", "value": self.phase})

    async def close(self): await self._close()


_bridges: dict[str, LiveSessionBridge] = {}

# ═══════════════════════════════════
# Session History Store (for progress page)
# ═══════════════════════════════════

class SessionStore:
    """Stores completed topic sessions for the progress dashboard."""
    def __init__(self):
        self.sessions: dict[str, list] = {}
        self.start_times: dict[str, float] = {}

    def record_start(self, device_id: str):
        if device_id not in self.start_times:
            self.start_times[device_id] = time.time()

    def record_topic(self, device_id: str, topic: str, mode: str, history: list,
                     pages: list = None, book_name: str = ""):
        self.sessions.setdefault(device_id, []).append({
            "topic": topic, "mode": mode, "history": history,
            "pages": pages or [], "book_name": book_name,
            "completed_at": time.time(),
        })
        for ws in _progress_ws_clients.get(device_id, []):
            try:
                asyncio.ensure_future(ws.send_text(json.dumps({"type": "update"})))
            except: pass

    def get_data(self, device_id: str) -> dict:
        topics = self.sessions.get(device_id, [])
        total_turns = sum(len(t["history"]) for t in topics)
        modes = set(t["mode"] for t in topics)
        mode_str = " & ".join(sorted(modes)) if modes else "—"

        if topics:
            start = self.start_times.get(device_id, topics[0]["completed_at"])
            end = topics[-1]["completed_at"]
            minutes = max(1, round((end - start) / 60))
        else:
            minutes = 0

        return {
            "topics": topics,
            "stats": {
                "topics_studied": len(topics),
                "total_turns": total_turns,
                "duration_minutes": minutes,
                "mode": mode_str,
            }
        }

session_store = SessionStore()
_progress_ws_clients: dict[str, list[WebSocket]] = {}


async def generate_summary(device_id: str) -> dict:
    """Use Gemini to generate knowledge summaries and encouraging feedback."""
    data = session_store.get_data(device_id)
    topics = data["topics"]
    if not topics:
        return {"feedback": None, "knowledge": [], **data}

    topic_summaries = []
    for t in topics:
        convos = []
        for h in t["history"]:
            if h.get("user"): convos.append(f"Student: {h['user']}")
            if h.get("tutor"): convos.append(f"Tutor: {h['tutor']}")
        topic_summaries.append({
            "topic": t["topic"], "mode": t["mode"],
            "pages": t.get("pages", []),
            "conversation": "\n".join(convos[-8:])
        })

    prompt = f"""You are SocratiDesk's learning analyst. A student just completed {len(topics)} topic(s).

Generate a JSON response with two parts:

1. "feedback" — A warm 1-2 sentence summary of the session. Use <strong> tags to bold key knowledge concepts mentioned. Be specific about what they learned.

2. "knowledge" — For EACH topic, create a card with:
   - "topic": Extract ONE keyword that captures the core concept (e.g. "Mammals" not "what is mammal", "pH Scale" not "i want to know what is ph value")
   - "summary": Detailed bullet points using HTML <ul><li> format. Each bullet should explain one key concept. Use <strong> to bold the most important terms. 3-4 bullets.
   - Keep the mode and pages from input.

Topics studied:
{json.dumps(topic_summaries, indent=2)}

Respond ONLY with valid JSON, no markdown fences, no extra text:
{{
  "feedback": {{
    "title": "short encouraging title (e.g. 'Awesome Curiosity!')",
    "message": "1-2 sentences with <strong>bolded key concepts</strong>. Be warm and specific.",
    "meta": "Studied {len(topics)} topic(s) · {data['stats']['total_turns']} turns"
  }},
  "knowledge": [
    {{
      "topic": "One Keyword (capitalized)",
      "mode": "textbook or curiosity",
      "summary": "<ul><li><strong>Key term</strong>: explanation of concept</li><li>...</li></ul>",
      "pages": "page numbers string or null"
    }}
  ]
}}"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
            config={"temperature": 0.3})
        text = response.text.strip()
        if text.startswith("```"): text = text.split("\n", 1)[1]
        if text.endswith("```"): text = text.rsplit("```", 1)[0]
        text = text.strip()
        result = json.loads(text)
        return {**data, **result}
    except Exception as e:
        print(f"[v4.2] summary generation error: {e}")
        knowledge = []
        for t in topics:
            knowledge.append({
                "topic": t["topic"], "mode": t["mode"],
                "summary": f"<ul><li>Studied via {t['mode']} mode</li><li>{len(t['history'])} exchanges completed</li></ul>",
                "pages": ", ".join(str(p) for p in t.get("pages", []))
            })
        return {
            **data,
            "feedback": {
                "title": "Keep Going! 🌟",
                "message": f"You've studied {len(topics)} topic(s) today. Every question you ask builds understanding!",
                "meta": f"{data['stats']['total_turns']} turns · {data['stats']['duration_minutes']}min"
            },
            "knowledge": knowledge,
        }

@app.get("/")
async def root():
    return JSONResponse({"status": "ok", "version": "v4.2", "textbooks": textbook_store.list_books()})

@app.post("/upload-textbook")
async def upload_textbook(file: UploadFile = File(...), device_id: str = "default"):
    filename = file.filename or "unknown"
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".txt", ".md"):
        return JSONResponse({"error": f"Unsupported: {suffix}"}, status_code=400)
    save_path = UPLOAD_DIR / filename
    save_path.write_bytes(await file.read())
    try:
        if suffix == ".pdf":
            if not PDF_SUPPORT: return JSONResponse({"error": "pdfplumber missing"}, 500)
            pages = []
            with pdfplumber.open(str(save_path)) as pdf:
                for i, pg in enumerate(pdf.pages):
                    t = pg.extract_text() or ""
                    if t.strip(): pages.append({"page": i+1, "text": t})
        else:
            pages = [{"page": 1, "text": save_path.read_text(encoding="utf-8", errors="replace")}]
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)
    if not pages: return JSONResponse({"error": "No text"}, 400)
    book_id = re.sub(r"[^a-z0-9_-]", "_", filename.lower())
    n = textbook_store.add_book(book_id, filename, pages)
    bridge = _bridges.get(device_id)
    if bridge and bridge.phase == PHASE_AWAITING_UPLOAD:
        await bridge.notify_upload(book_id, filename)
    return JSONResponse({"status": "ok", "book_id": book_id, "name": filename, "pages": len(pages), "chunks": n})

@app.get("/textbooks")
async def list_textbooks():
    return JSONResponse({"textbooks": textbook_store.list_books()})

@app.get("/logo.png")
async def logo_image():
    from fastapi.responses import FileResponse
    p = Path(__file__).parent / "logo.png"
    if p.exists():
        return FileResponse(p, media_type="image/png")
    return JSONResponse({"error": "not found"}, 404)

@app.get("/upload", response_class=HTMLResponse)
async def upload_page():
    p = Path(__file__).parent / "upload.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists() else "<h1>upload.html not found</h1>")

@app.get("/progress", response_class=HTMLResponse)
async def progress_page(device_id: str = "default"):
    p = Path(__file__).parent / "progress.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists() else "<h1>progress.html not found</h1>")

@app.get("/progress-data")
async def progress_data(device_id: str = "default"):
    result = await generate_summary(device_id)
    bridge = _bridges.get(device_id)
    if bridge:
        result["history"] = bridge.history
    elif not result.get("history"):
        result["history"] = []
    return JSONResponse(result)

@app.websocket("/progress-ws")
async def progress_ws(websocket: WebSocket, device_id: str = "default"):
    await websocket.accept()
    _progress_ws_clients.setdefault(device_id, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _progress_ws_clients[device_id] = [
            w for w in _progress_ws_clients.get(device_id, []) if w is not websocket]

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
                b = _bridges.get(session)
                if b and b.phase == PHASE_AWAITING_UPLOAD:
                    await b.notify_upload(msg.get("book_id",""), msg.get("name","textbook"))
    except WebSocketDisconnect: pass
    finally:
        _upload_notify_clients[session] = [w for w in _upload_notify_clients.get(session,[]) if w is not websocket]


# ═══════════════════════════════════
# WebSocket
# ═══════════════════════════════════

@app.websocket("/live")
async def live_ws(websocket: WebSocket):
    await websocket.accept()
    device_id = websocket.query_params.get("device_id", "default")
    bridge = LiveSessionBridge(websocket, device_id)
    _bridges[device_id] = bridge
    session_store.record_start(device_id)
    print(f"[v4.2] Pi connected: {device_id}")
    try:
        await bridge._send({"type": "state", "value": "ready"})
        await bridge._send({"type": "phase", "value": bridge.phase})
        await bridge._send({"type": "textbooks", "value": textbook_store.list_books()})

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "hello":
                print(f"[v4.2] hello from {msg.get('device','?')}")
                await bridge._send({"type": "state", "value": "ready"})
                await bridge._send({"type": "phase", "value": bridge.phase})

            elif t == "start_turn":
                print(f"[v4.2] start_turn phase={bridge.phase}")
                try:
                    await bridge.begin_turn()
                    await bridge._send({"type": "state", "value": "listening"})
                    print(f"[v4.2] begin_turn OK")
                except Exception as e:
                    import traceback; print(f"[v4.2] begin_turn ERR: {e}"); traceback.print_exc()
                    await bridge._send({"type": "error", "message": str(e)})

            elif t == "audio":
                await bridge.send_audio(base64.b64decode(msg["data"]),
                                        msg.get("mime_type", "audio/pcm;rate=16000"))

            elif t == "end_turn":
                print(f"[v4.2] end_turn from Pi, phase={bridge.phase}")
                if bridge.phase not in (PHASE_GREETING, PHASE_AWAITING_UPLOAD):
                    await bridge.end_turn()
                else:
                    print(f"[v4.2] skipping end_turn (always speak-first)")
                await bridge._send({"type": "state", "value": "thinking"})

            elif t == "set_phase":
                await bridge.set_phase(msg.get("phase", PHASE_GREETING))

            elif t == "vosk_answer":
                print(f"[v4.2] vosk: {msg.get('answer')!r}")
                await bridge._handle_yes_no(msg.get("answer", ""))

            elif t == "reset_topic": await bridge.reset_topic()
            elif t == "reset_session": await bridge.reset_session()

    except WebSocketDisconnect: print(f"[v4.2] disconnected: {device_id}")
    except Exception as e:
        print(f"[v4.2] error: {e}"); import traceback; traceback.print_exc()
    finally:
        _bridges.pop(device_id, None); await bridge.close()