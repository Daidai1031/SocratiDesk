# SocratiDesk

**A Focus-First AI Study Companion — Voice-first, Socratic, Textbook-aware**

Built for the [Gemini Live Agent Challenge](https://geminiliveagentchallenge.devpost.com/) — Live Agents track.

---

## The Problem

Students increasingly use AI as an answer machine — paste a question, get an answer, move on. This creates shallow learning, kills reasoning skills, and bypasses the textbook entirely.

At the same time, most AI study tools live on laptops and phones, surrounded by notifications and distractions.

## The Solution

SocratiDesk is a dedicated voice-first AI study companion that sits on a student's desk. It uses the **Socratic method** — guiding students through questions, hints, and reasoning — instead of giving direct answers.

Two learning modes:
- **Curiosity Mode** — Free exploration of any topic with a 3-stage Socratic dialogue
- **Textbook Mode** — Guided study from an uploaded PDF, directing students to specific pages

The device has no keyboard, no browser, no distractions. Just a microphone, a speaker, and a small screen.

---

## User Flow

```
Student: "Hey SocratiDesk!"
Tutor:   "Hey there! Do you have a textbook you'd like to study with today?"

         ┌─── "Yes, I have a book" ──────────────────────┐
         │                                                │
         │  Tutor: "Scan the QR code to upload it."       │
         │  [QR code appears on Pi screen]                │
         │  [Student scans, uploads PDF from phone]       │
         │  Tutor: "Got it! What topic shall we start?"   │
         │                                                │
         │  → Textbook-Guided Mode (3 stages)             │
         │    Stage 1: "Open to page 5, read the          │
         │             pH scale section."                  │
         │    Stage 2: "Good! Your book mentions pH 7     │
         │             is neutral. What makes lemon        │
         │             juice acidic?"                      │
         │    Stage 3: "Exactly — acids release H+ ions.  │
         │             Review the indicator section!"      │
         │                                                │
         └────────────────────────────────────────────────┘

         ┌─── "No, just curious" ────────────────────────┐
         │                                                │
         │  Tutor: "No problem! What are you curious      │
         │          about today?"                         │
         │                                                │
         │  → Curiosity Mode (3 stages)                   │
         │    Stage 1: "What do you already know           │
         │             about mammals?"                    │
         │    Stage 2: "Good start! What special thing     │
         │             do mammal mothers do?"              │
         │    Stage 3: "Exactly — mammals produce milk     │
         │             to feed their young."              │
         │                                                │
         └────────────────────────────────────────────────┘
```

---

## System Architecture

```
┌──────────────────────┐
│   Student's Phone    │
│   (upload PDF)       │
│                      │
│  scan QR → upload    │
│  via /upload page    │
└──────────┬───────────┘
           │ HTTP POST + WebSocket notify
           ▼
┌──────────────────────────────────────────────┐
│          Google Cloud Run                     │
│          (live-server)                        │
│                                              │
│  FastAPI WebSocket server                    │
│  ├── /live          Voice session (Pi↔Gemini)│
│  ├── /upload        Mobile upload page       │
│  ├── /upload-textbook  PDF processing        │
│  ├── /upload-notify    Realtime notification │
│  └── /wait-for-upload  Long-poll fallback    │
│                                              │
│  Phase-based state machine:                  │
│  greeting → awaiting_mode → curiosity        │
│                           → awaiting_upload  │
│                             → textbook_ready │
│                               → textbook     │
│                                              │
│  RAG: page-aware textbook chunks             │
│  Socratic system instructions per stage      │
│                                              │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Firestore│  │   GCS    │  │ Gemini    │  │
│  │ (chunks) │  │ (PDFs)   │  │ Live API  │  │
│  └──────────┘  └──────────┘  └───────────┘  │
└──────────────────────────────────────────────┘
           ▲
           │ WebSocket (audio + transcripts)
           ▼
┌──────────────────────┐
│   Raspberry Pi 5     │
│                      │
│  main.py     Voice   │
│  audio.py    I/O     │
│  qr_upload.py QR     │
│                      │
│  USB Mic + Speaker   │
│  MiniPiTFT 1.14"    │
└──────────────────────┘
```

---

## Tech Stack

| Component | Technology |
|---|---|
| AI Model | Gemini 2.5 Flash Native Audio (Live API) |
| SDK | Google GenAI SDK (`google-genai`) |
| Backend | FastAPI + Uvicorn on Cloud Run |
| Storage | Google Cloud Storage (PDFs) |
| Database | Cloud Firestore (textbook chunks) |
| Secrets | Google Secret Manager (API key) |
| Device | Raspberry Pi 5 |
| Audio | `sounddevice` (PCM 16kHz in, 24kHz out) |
| Display | Adafruit MiniPiTFT 1.14" 240x135 (ST7789) |
| RAG | Page-aware keyword chunking + retrieval |
| Upload | Mobile-first HTML page via QR code scan |

---

## Repository Structure

```
voice-study-companion/
│
├── live-server/                 # Cloud Run backend
│   ├── main.py                  # FastAPI server, state machine, RAG, Gemini Live
│   ├── upload.html              # Mobile upload page (served at /upload)
│   ├── requirements.txt         # Server dependencies
│   └── Dockerfile               # Container config
│
├── pi-device/                   # Raspberry Pi client
│   ├── main.py                  # Voice client, keyboard control, WebSocket
│   ├── audio.py                 # Mic input + speaker output (sounddevice)
│   ├── qr_upload.py             # QR code generation + upload listener
│   ├── requirements.txt         # Pi dependencies
│   └── .env                     # Environment config (URLs, device ID)
│
├── deploy.sh                    # One-click Cloud Run deployment (IaC)
├── k12_science_textbook.pdf     # Sample textbook for demo
└── README.md                    # This file
```

---

## Setup & Deployment

### Prerequisites

- Google Cloud project with billing enabled
- Gemini API key from [AI Studio](https://aistudio.google.com/apikey)
- `gcloud` CLI installed and authenticated
- Raspberry Pi 5 with USB mic, speaker, and network access

### 1. Deploy Server (from your laptop)

```bash
# Store API key in Secret Manager (recommended)
echo -n "YOUR_GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Build and deploy
cd live-server
gcloud builds submit --tag gcr.io/YOUR_PROJECT/live-server

gcloud run deploy live-server \
  --image gcr.io/YOUR_PROJECT/live-server \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --timeout 3600 \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest"
```

Or use the automated script:
```bash
chmod +x deploy.sh   # Linux/Mac only
./deploy.sh YOUR_PROJECT_ID YOUR_API_KEY
```

On Windows, run the gcloud commands directly (no chmod needed).

### 2. Verify Deployment

Open in browser:
- `https://YOUR-URL/` → Should return JSON with `"status": "ok"`
- `https://YOUR-URL/upload` → Should show the mobile upload page

### 3. Setup Raspberry Pi

```bash
ssh pi@YOUR_PI_IP
cd ~/voice-study-companion/pi-device

# Install dependencies
source .venv/bin/activate
pip install -r requirements.txt

# For TFT display (optional)
pip install adafruit-circuitpython-st7789 adafruit-circuitpython-rgb-display
```

Create `.env`:
```
SOCRATIDESK_WS=wss://YOUR-CLOUD-RUN-URL/live
SOCRATIDESK_HTTP=https://YOUR-CLOUD-RUN-URL
DEVICE_ID=socratiDesk-001
```

### 4. Run

**Upload a textbook (first time):**
```bash
python qr_upload.py
# Scan QR with phone → upload PDF → Pi confirms receipt
```

**Start voice session:**
```bash
python main.py
# Press Enter to record, Enter again to stop
# Say "Hey SocratiDesk" to begin
```

---

## Conversation Phases (State Machine)

| Phase | Trigger | Tutor Behavior |
|---|---|---|
| `greeting` | User says "Hi SocratiDesk" | Greets warmly, asks "Do you have a textbook?" |
| `awaiting_mode` | After greeting | Listens for "yes" or "no" |
| `awaiting_upload` | User says "yes" + no book uploaded | Says "Scan the QR code", Pi shows QR |
| `textbook_ready` | Book uploaded or already available | Confirms book received, asks first question |
| `curiosity_greeting` | User says "no" | Welcomes to curiosity mode, asks first question |
| `textbook` (stage 1) | User asks a topic question | RAG retrieves pages, directs student to read |
| `textbook` (stage 2) | Student shares understanding | Evaluates against textbook, asks guiding question |
| `textbook` (stage 3) | Student answers again | Feedback + summary citing textbook pages |
| `curiosity` (stage 1) | User asks a topic question | "What do you already know about [topic]?" |
| `curiosity` (stage 2) | Student gives initial answer | Feedback + guiding follow-up question |
| `curiosity` (stage 3) | Student answers again | Clear conclusion + optional next topic |

---

## Textbook RAG Pipeline

1. **Upload**: Student scans QR → phone opens `/upload` → selects PDF → POST to `/upload-textbook`
2. **Storage**: PDF saved to Google Cloud Storage
3. **Extraction**: `pdfplumber` extracts text per page, preserving page numbers
4. **Chunking**: Text split into ~500-char chunks, each tagged with its page number
5. **Persistence**: Chunks stored in Firestore (survives restarts) + in-memory (fast retrieval)
6. **Retrieval**: When student asks a question, keyword search finds top-3 matching chunks
7. **Injection**: Matching chunks (with page numbers) injected into Gemini's system instruction
8. **Guidance**: Gemini references specific pages: "Open to page 5, you'll see..."

---

## Key Features for Competition

### "Beyond Text Box" (40% of score)

- **Voice-first**: No screen, no keyboard — pure voice interaction
- **Barge-in**: Student can interrupt tutor mid-sentence (VAD enabled)
- **Distinct persona**: "SocratiDesk" — warm, encouraging, never robotic
- **QR scan flow**: Phone → scan → upload → voice session (seamless multimodal)
- **Vision-ready**: WebSocket accepts image frames for "see homework" feature

### Technical Implementation (30% of score)

- **Google GenAI SDK**: `client.aio.live.connect()` for streaming audio
- **3 Google Cloud services**: Cloud Run + Firestore + GCS
- **Secret Manager**: API key stored securely (not in env vars)
- **RAG with grounding**: Page-aware textbook chunks prevent hallucination
- **Automated deployment**: `deploy.sh` for IaC (competition bonus)
- **Error handling**: Graceful fallbacks (Firestore optional, GCS optional, TFT optional)

### Demo & Presentation (30% of score)

- Architecture diagram included
- Working demo on physical Raspberry Pi hardware
- Both learning modes demonstrated end-to-end
- Cloud deployment provable via GCP Console

---

## Pi Controls

| Input | Action |
|---|---|
| `Enter` | Start / stop recording |
| `reset` | Reset topic (keep mode) |
| `new` | Full session reset (back to greeting) |
| `mode curiosity` | Skip to curiosity mode directly |
| `mode textbook` | Skip to textbook mode directly |
| `books` | List uploaded textbooks |
| `quit` | Exit |

---

## Demo Script (4 minutes)

**0:00–0:30 — Problem statement**
"Students use AI as an answer machine. SocratiDesk flips this — it makes students think."

**0:30–1:00 — Architecture overview**
Show diagram. Highlight: Gemini Live API, Cloud Run, Firestore, Raspberry Pi.

**1:00–1:30 — QR upload flow**
Run `qr_upload.py` → scan with phone → upload sample textbook → Pi confirms.

**1:30–2:30 — Textbook Mode demo**
"Hey SocratiDesk!" → "Yes I have a textbook" → "What is pH value?"
Show all 3 stages: page reference → comprehension check → feedback.

**2:30–3:30 — Curiosity Mode demo**
`new` → "Hey SocratiDesk" → "No, just curious" → "What is a mammal?"
Show all 3 stages. Demonstrate barge-in (interrupt tutor mid-sentence).

**3:30–4:00 — Impact**
"SocratiDesk encourages reasoning over retrieval. It works for STEM, younger students, and hands-free learning environments."

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `API key not valid` | Check Secret Manager or `--set-env-vars`. Regenerate key at aistudio.google.com/apikey |
| `Connection refused` on Pi | Verify `.env` URLs match your Cloud Run service URL |
| `GPIO busy` on TFT | `sudo systemctl stop piscreen` then retry |
| `UnicodeEncodeError` on Pi | `export PYTHONIOENCODING=utf-8` (add to `~/.bashrc`) |
| No audio output | Check `aplay -l` for devices. Verify speaker is connected |
| Upload page 404 | Redeploy — ensure `upload.html` is in `live-server/` next to `main.py` |
| Textbook not found after upload | Upload goes to server RAM. If server restarted, re-upload |

---

## Future Improvements

- Wake word detection ("Hey SocratiDesk" without pressing Enter)
- Multi-topic memory across sessions
- Emotion detection from voice tone (Gemini affective dialog)
- Adaptive difficulty based on student performance
- Learning progress tracking in Firestore
- Vision mode: point camera at textbook page instead of uploading PDF

---

## Acknowledgments

Built with:
- [Google Gemini Live API](https://ai.google.dev/gemini-api/docs/live-api)
- [Google Cloud Run](https://cloud.google.com/run)
- [Google Cloud Firestore](https://cloud.google.com/firestore)
- [Google GenAI SDK](https://pypi.org/project/google-genai/)
- Raspberry Pi 5
- Adafruit MiniPiTFT 1.14"

**Gemini Live Agent Challenge 2026 — Live Agents Track**
`#GeminiLiveAgentChallenge`
