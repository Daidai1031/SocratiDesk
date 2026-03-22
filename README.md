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
- **Textbook Mode** — Guided study from an uploaded PDF with a 3-stage learning cycle

The device has no keyboard, no browser, no distractions. Just a microphone, a speaker, and a small screen.

---
## Demo Video
[![Watch the demo](https://img.youtube.com/vi/TNkHT_lynAg/maxresdefault.jpg)](https://www.youtube.com/watch?v=TNkHT_lynAg)

<p align="center">
  Click the preview image to watch the full demo on YouTube.
</p>


## User Flow

```
Student: "Hey SocratiDesk/Socrati/Socratic!"
Tutor:   "Hey there! Do you have a textbook you'd like to study with today?"

         ┌─── "Yes, I have a book" ──────────────────────┐
         │                                                │
         │  Tutor: "Scan the QR code to upload it."       │
         │  [QR code appears on Pi screen]                │
         │  [Student scans, uploads PDF from phone]       │
         │  Tutor: "Got it! What topic shall we start?"   │
         │                                                │
         │  → Textbook-Guided Mode (3 stages)             │
         │                                                │
         │    Stage 1 — Page Direction:                   │
         │      "Open to page 12, look at the middle      │
         │       section. You'll find the pH scale there. │
         │       Read it and tell me what you found."     │
         │                                                │
         │    Stage 2 — Feedback + Answer + Question:     │
         │      "Good! The textbook says pH 7 is neutral. │
         │       Acids are below 7, bases above 7.        │
         │       Quick check: is lemon juice acidic?"     │
         │                                                │
         │    Stage 3 — Final Feedback & Summary:         │
         │      "That's right! Lemon juice is acidic.     │
         │       Great job understanding pH from page 12! │
         │       Want to explore another topic?"          │
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

## Conversation Phases (State Machine)

| Phase | Trigger | Tutor Behavior |
|---|---|---|
| `greeting` | User says "Hi Socrati/Socratic" | Greets warmly, asks "Do you have a textbook?" |
| `awaiting_mode` | After greeting | Listens for "yes" or "no" |
| `awaiting_upload` | User says "yes" + no book uploaded | Says "Scan the QR code", Pi shows QR |
| `textbook_ready` | Book uploaded or already available | Confirms book received, asks first question |
| `curiosity_greeting` | User says "no" | Welcomes to curiosity mode, asks first question |

### Textbook Mode — 4-Stage Learning Cycle

| Stage | Name | Tutor Behavior |
|---|---|---|
| Stage 1 | **Page Direction** | RAG retrieves relevant pages. Tutor tells student which page and section to open. Does NOT give the answer — only directs to the page. Asks student to read and report back. |
| Stage 2 | **Feedback + Answer + Question** | Gives feedback on what student got right/wrong. Explains the concept in simple language. Asks ONE comprehension question to check understanding. |
| Stage 3 | **Final Feedback & Summary** | Evaluates the student's answer. Gives final summary citing the textbook page. Encourages exploring another topic. |

### Curiosity Mode — 3-Stage Socratic Dialogue

| Stage | Name | Tutor Behavior |
|---|---|---|
| Stage 1 | **Prior Knowledge** | "What do you already know about [topic]?" — Never gives the answer. |
| Stage 2 | **Guided Question** | Gives brief feedback, asks ONE guiding follow-up question. Still doesn't give the answer. |
| Stage 3 | **Conclusion** | Gives feedback, provides a clear concise explanation. Topic complete. |

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
│  └── /upload-notify    Realtime notification │
│                                              │
│  Phase-based state machine                   │
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
│   ├── progress.html            # Learning progress dashboard (served at /progress)
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
### visual proof of Cloud deployment
<p align="center">
  <a href="https://youtu.be/_p_rQWRO_hs">
    <img src="https://img.youtube.com/vi/_p_rQWRO_hs/maxresdefault.jpg" width="600">
  </a>
</p>

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

### 2. Run Locally (for development)

```bash
cd live-server
set GEMINI_API_KEY=your-api-key
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

Verify:
- `http://localhost:8080/` → Should return JSON with `"version": "v4"`
- `http://localhost:8080/upload` → Should show the mobile upload page

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
# Say "Hey SocratiDesk" to begin
```

---

## Learning Progress Dashboard

After completing a topic, students can scan a QR code on the Pi screen to view their learning progress on their phone.

**Access**: `https://YOUR-URL/progress?device_id=socratiDesk-001`

### Features

The dashboard has three tabs:

| Tab | Content |
|---|---|
| **Summary** | AI-generated encouraging feedback — specific praise about what the student learned, how they engaged, and their progress |
| **Knowledge** | Knowledge cards for each completed topic — key concepts as bullet points, with textbook page references when applicable |
| **History** | Full conversation timeline showing each student-tutor exchange with phase and stage labels |

### How It Works

1. Student completes a 3-stage (curiosity) or 3-stage (textbook) dialogue
2. Session data is stored on the server with topic, mode, and full conversation history
3. When the student opens the progress page, Gemini generates a personalized summary
4. The Pi TFT screen shows a QR code to the progress page after each completed topic

### Header Stats

The dashboard header shows at-a-glance metrics: topics studied, total conversation turns, session duration in minutes, and learning mode used.

### Pi TFT Display

During a session, the Pi's 1.14" TFT screen shows:
- Current phase and stage (e.g., "Textbook mode Stage 2/3")
- "Topic done! Scan for summary" with QR code after completion
- "Say 'Hey Socrati'" when idle

---

## Textbook RAG Pipeline

1. **Upload**: Student scans QR → phone opens `/upload` → selects PDF → POST to `/upload-textbook`
2. **Storage**: PDF saved to Google Cloud Storage
3. **Extraction**: `pdfplumber` extracts text per page, preserving page numbers
4. **Chunking**: Text split into ~500-char chunks, each tagged with its page number
5. **Persistence**: Chunks stored in Firestore (survives restarts) + in-memory (fast retrieval)
6. **Retrieval**: When student asks a question, keyword search finds top-3 matching chunks
7. **Injection**: Matching chunks (with page numbers) injected into Gemini's system instruction
8. **Guidance**: Gemini references specific pages: "Open to page 12, look at the middle section..."

---

## Audio & Silence Detection

The Pi client uses a dual-threshold system for reliable turn-taking:

| Parameter | Value | Purpose |
|---|---|---|
| `SILENCE_THRESHOLD` | 500 | Raw RMS below this = silence (above mic floor noise ~280) |
| `SPEECH_THRESHOLD` | 800 | Raw RMS above this = confirmed speech |
| `SILENCE_TIMEOUT` | 2.0s | Duration of silence after speech to trigger auto-stop |
| `MIN_RECORD_TIME` | 1.5s | Minimum recording time before auto-stop can trigger |
| `MAX_GAIN` | 4.0x | Software gain applied to audio sent to Gemini (not used for silence detection) |

**Key design**: Silence detection uses **raw RMS** (before gain boost), preventing the gain from masking silence. Auto-stop only triggers after confirmed speech has been detected, preventing false stops when recording starts during silence.

---

## Pi Controls

| Input | Action |
|---|---|
| `Enter` | Manual start / stop recording |
| `new` | Full session reset (back to greeting) |
| `reset` | Reset topic (keep mode) |
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

**1:30–2:45 — Textbook Mode demo (3 stages)**
"Hey SocratiDesk!" → "Yes I have a textbook" → "What is pH value?"
- Stage 1: Tutor directs to page — does NOT give the answer
- Stage 2: Student reports back, tutor gives feedback + answer + asks a question
- Stage 3: Student answers, tutor gives final feedback and summary

**2:45–3:30 — Curiosity Mode demo (3 stages)**
`new` → "Hey SocratiDesk" → "No, just curious" → "What is a mammal?"
- Stage 1: "What do you already know?"
- Stage 2: Guiding question
- Stage 3: Conclusion

**3:30–4:00 — Impact**
"SocratiDesk encourages reasoning over retrieval. It works for STEM, younger students, and hands-free learning environments."

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `API key not valid` | Set `GEMINI_API_KEY` env var. On Windows: `set GEMINI_API_KEY=xxx` |
| Server shows old version | Make sure you replaced the file AND restarted uvicorn (no `--reload`) |
| `Connection refused` on Pi | Verify `.env` URLs match your Cloud Run service URL |
| No auto-stop (keeps recording) | Check RMS values. If floor noise > 500, increase `SILENCE_THRESHOLD` |
| Gemini doesn't respond | Check server logs for `>>> ActivityEnd`. If missing, `end_turn` was skipped |
| Upload page 404 | Ensure `upload.html` is in `live-server/` next to `main.py` |
| Textbook not found after upload | Upload goes to server RAM. If server restarted, re-upload |

---

## Key Features for Competition

### "Beyond Text Box" (40% of score)
- **Voice-first**: No screen, no keyboard — pure voice interaction
- **Barge-in**: Student can interrupt tutor mid-sentence
- **Distinct persona**: "SocratiDesk" — warm, encouraging, never robotic
- **QR scan flow**: Phone → scan → upload → voice session (seamless multimodal)

### Technical Implementation (30% of score)
- **Google GenAI SDK**: `client.aio.live.connect()` for streaming audio
- **3 Google Cloud services**: Cloud Run + Firestore + GCS
- **RAG with grounding**: Page-aware textbook chunks prevent hallucination
- **Manual VAD**: ActivityStart/ActivityEnd for precise turn-taking
- **Automated deployment**: `deploy.sh` for IaC (competition bonus)

### Demo & Presentation (30% of score)
- Architecture diagram included
- Working demo on physical Raspberry Pi hardware
- Both learning modes demonstrated end-to-end (3-stage textbook + 3-stage curiosity)
- Cloud deployment provable via GCP Console

---

## Reproducible Testing

This section explains how to verify that each component of SocratiDesk works correctly — from the API connection to the full voice loop. All tests can be run without a Raspberry Pi unless noted.

### Prerequisites

```bash
# Clone the repo and install server dependencies
cd live-server
pip install -r requirements.txt

# Set your Gemini API key
export GEMINI_API_KEY=your-api-key          # Linux/Mac
set GEMINI_API_KEY=your-api-key             # Windows CMD
$env:GEMINI_API_KEY="your-api-key"          # Windows PowerShell
```

### Test 1 — Gemini API Connection

Verify that your API key works and Gemini responds.

```bash
cd ..
python test_gemini.py
```

**Expected**: A short paragraph explaining what a mammal is. If you see `API key not valid`, double-check your `GEMINI_API_KEY` environment variable.

### Test 2 — Server Health Check

Start the server locally and verify all endpoints.

```bash
cd live-server
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

In a separate terminal:

```bash
# Root endpoint — should return JSON with version "v4"
curl http://localhost:8080/
# Expected: {"status":"ok","version":"v4","textbooks":[]}

# Upload page — should return HTML
curl -s http://localhost:8080/upload | head -5
# Expected: <!DOCTYPE html> ...

# Progress page — should return HTML
curl -s "http://localhost:8080/progress?device_id=test" | head -5
# Expected: <!DOCTYPE html> ...

# Textbook list — should return empty list
curl http://localhost:8080/textbooks
# Expected: {"textbooks":[]}
```

### Test 3 — Textbook Upload & RAG Pipeline

Upload a PDF and verify the chunking pipeline processes it correctly.

```bash
# Upload the sample textbook (or any PDF)
curl -X POST -F "file=@k12_science_textbook.pdf" \
  http://localhost:8080/upload-textbook

# Expected response (values will vary):
# {"status":"ok","book_id":"k12_science_textbook_pdf","name":"k12_science_textbook.pdf","pages":42,"chunks":187}

# Verify the book appears in the list
curl http://localhost:8080/textbooks
# Expected: {"textbooks":[{"id":"k12_science_textbook_pdf","name":"k12_science_textbook.pdf","chunks":187,"pages":42}]}
```

**What to check**:
- `status` is `"ok"`
- `pages` > 0 (PDF was read successfully)
- `chunks` > 0 (text was extracted and split)

If `pages` is 0 or you get an error, the PDF may be scanned images without text. Try a different PDF with selectable text.

### Test 4 — Socratic Chat Logic (Non-Voice)

Test the 3-stage Socratic dialogue using the `/chat` endpoint in `app.py`. This uses the text-based tutor logic (not the Live API), useful for verifying the Socratic flow without audio.

```bash
# Start the text-based server
cd ..
python -m uvicorn app:app --host 0.0.0.0 --port 8081
```

```bash
# Stage 1 — Initial question (tutor should ask what you already know)
curl -X POST http://localhost:8081/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is a mammal?"}'
# Expected: reply asks "What do you already know about mammals?" (does NOT give the answer)
# Save the session_id from the response

# Stage 2 — Student responds (tutor gives feedback + guiding question)
curl -X POST http://localhost:8081/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "SESSION_ID_FROM_ABOVE", "message": "I think mammals are animals with fur"}'
# Expected: feedback on the answer + a follow-up question (still does NOT give the full answer)

# Stage 3 — Student responds again (tutor gives conclusion)
curl -X POST http://localhost:8081/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "SESSION_ID_FROM_ABOVE", "message": "They feed milk to their babies"}'
# Expected: praise + clear final explanation of what a mammal is

# Verify session state
curl http://localhost:8081/session/SESSION_ID_FROM_ABOVE
# Expected: found=true, stage=3, history contains 3 entries
```

**What to verify at each stage**:
- Stage 1: Tutor acknowledges but does **not** define the term. Asks an open-ended question.
- Stage 2: Tutor gives feedback, still withholds the full answer, asks a guiding question.
- Stage 3: Tutor provides a clear, concise explanation.

### Test 5 — WebSocket Live Session

Test the real-time WebSocket connection that the Pi uses. This requires `websocat` or a similar WebSocket CLI tool.

```bash
# Install websocat (https://github.com/vi/websocat)
# Mac: brew install websocat
# Linux: cargo install websocat

# Connect to the live WebSocket (server must be running on port 8080)
websocat ws://localhost:8080/live?device_id=test-device
```

Type the following JSON messages and press Enter after each:

```json
{"type": "hello", "device": "test-device"}
```

**Expected response**: `{"type":"state","value":"ready"}` and `{"type":"phase","value":"idle"}`

```json
{"type": "set_phase", "phase": "greeting"}
```

**Expected**: `{"type":"phase","value":"greeting"}`

```json
{"type": "start_turn"}
```

**Expected**: `{"type":"state","value":"listening"}` followed by audio data (the greeting). You should see `{"type":"audio","data":"..."}` messages and eventually `{"type":"turn_complete"}`.

Press `Ctrl+C` to disconnect.

### Test 6 — Progress Dashboard

After completing at least one Socratic dialogue via the WebSocket (or by manually inserting test data), verify the progress page works.

```bash
# Fetch progress data (returns JSON)
curl "http://localhost:8080/progress-data?device_id=test-device"

# If no sessions completed yet, expected:
# {"topics":[],"stats":{"topics_studied":0,"total_turns":0,"duration_minutes":0,"mode":"—"},...}

# Open in browser to see the visual dashboard:
# http://localhost:8080/progress?device_id=test-device
```

### Test 7 — Raspberry Pi Hardware (On-Device)

These tests require the actual Raspberry Pi with mic, speaker, and optional TFT display.

**Microphone & RMS levels**:

```bash
cd pi-device
python test_mic.py --duration 10
```

**What to check**:
- Max RMS > 300 when speaking (mic is capturing audio)
- Min RMS < 200 when silent (floor noise is reasonable)
- Script prints a suggested `SILENCE_THRESHOLD` value

**Vosk wake word detection** (requires Vosk model downloaded):

```bash
python test_mic.py --vosk ../models/vosk-model-small-en-us-0.15 --duration 15
```

Say "Hey Socrati" during the test. **Expected**: `WAKE WORD DETECTED!` appears in the output.

**Full voice session**:

```bash
# Make sure .env is configured with your server URL
python main.py
```

Run through this sequence:
1. Say "Hey SocratiDesk" → should hear a greeting
2. Say "No" → should enter curiosity mode
3. Say "What is a mammal?" → should ask what you already know (Stage 1)
4. Respond → should give feedback and ask a follow-up (Stage 2)
5. Respond again → should give a conclusion (Stage 3)
6. Type `new` + Enter → full reset
7. Type `quit` + Enter → clean exit

### Test Summary Checklist

| # | Test | Command | Pass Criteria |
|---|------|---------|---------------|
| 1 | API connection | `python test_gemini.py` | Gets a text response from Gemini |
| 2 | Server health | `curl localhost:8080/` | Returns `{"version":"v4"}` |
| 3 | PDF upload | `curl -F file=@book.pdf localhost:8080/upload-textbook` | Returns `pages > 0, chunks > 0` |
| 4 | Socratic flow | 3 sequential `/chat` calls | Stage 1 asks, Stage 2 guides, Stage 3 concludes |
| 5 | WebSocket | `websocat ws://localhost:8080/live` | Receives `ready` state and audio on `start_turn` |
| 6 | Progress | `curl localhost:8080/progress-data` | Returns valid JSON with stats |
| 7 | Pi hardware | `python test_mic.py` | RMS > 300 when speaking |

### Common Test Failures

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `test_gemini.py` fails with key error | `GEMINI_API_KEY` not set | Export the env var in your current shell |
| Server returns old version | Stale process | Kill old uvicorn, restart without `--reload` |
| PDF upload returns `"error":"pdfplumber missing"` | Missing dependency | `pip install pdfplumber` |
| WebSocket connects but no audio response | Gemini session failed to open | Check server logs for connection errors |
| `test_mic.py` shows Max RMS < 100 | Mic not capturing | Run `arecord -l` to check device list |
| Vosk never detects wake word | Wrong model path or model too small | Verify `VOSK_MODEL_PATH` points to a valid model |

---
## Automated Cloud Deployment

SocratiDesk includes an automated deployment script for Google Cloud Run.

This script:
- enables required Google Cloud APIs
- provisions Cloud Storage and Firestore
- builds the backend container
- deploys the service to Cloud Run
- outputs the final service URL for device configuration

Deployment script:  
[deploy.sh](https://github.com/Daidai1031/SocratiDesk/blob/main/deploy.sh)

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
