from fastapi import FastAPI
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv
import os
import uuid

from tutor_logic import build_tutor_prompt

load_dotenv()

app = FastAPI()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

sessions = {}


class TutorRequest(BaseModel):
    topic: str | None = None
    message: str
    session_id: str | None = None
    reset: bool = False
    force_new_topic: bool = False


def looks_like_new_topic(message: str) -> bool:
    text = message.strip().lower()

    new_topic_starters = [
        "what is",
        "what are",
        "who is",
        "who are",
        "explain",
        "tell me about",
        "how does",
        "how do",
        "why is",
        "why are",
        "define"
    ]

    return any(text.startswith(starter) for starter in new_topic_starters)


def infer_topic(topic: str | None, message: str) -> str:
    if topic and topic.strip():
        return topic.strip()
    return message.strip()


def create_new_session(topic: str):
    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "topic": topic,
        "stage": 1,
        "history": []
    }
    return session_id, 1


@app.get("/")
def root():
    return {
        "status": "ok",
        "project": "voice-study-companion"
    }


@app.get("/session/{session_id}")
def get_session(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return {"found": False, "session_id": session_id}

    return {
        "found": True,
        "session_id": session_id,
        "topic": session["topic"],
        "stage": session["stage"],
        "history": session["history"]
    }


@app.post("/chat")
def chat(req: TutorRequest):
    requested_topic = infer_topic(req.topic, req.message)

    # 1. Explicitly force a brand-new topic/session
    if req.force_new_topic:
        session_id, stage = create_new_session(requested_topic)

    # 2. Explicit reset or missing/invalid session
    elif req.reset or not req.session_id or req.session_id not in sessions:
        session_id, stage = create_new_session(requested_topic)

    else:
        current_session = sessions[req.session_id]
        current_stage = current_session["stage"]

        # 3. Auto-start a new topic after finishing previous one
        if current_stage == 3 and looks_like_new_topic(req.message):
            session_id, stage = create_new_session(requested_topic)
        else:
            session_id = req.session_id
            stage = current_stage

    active_topic = sessions[session_id]["topic"]

    prompt = build_tutor_prompt(
        topic=active_topic,
        student_message=req.message,
        stage=stage
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "temperature": 0.4
        }
    )

    reply_text = response.text.strip() if response.text else ""

    sessions[session_id]["history"].append({
        "stage": stage,
        "student": req.message,
        "tutor": reply_text
    })

    if stage == 1:
        next_stage = 2
    elif stage == 2:
        next_stage = 3
    else:
        next_stage = 3

    sessions[session_id]["stage"] = next_stage

    return {
        "session_id": session_id,
        "topic": active_topic,
        "stage_used": stage,
        "next_stage": next_stage,
        "reply": reply_text
    }


@app.post("/reset/{session_id}")
def reset_session(session_id: str):
    if session_id in sessions:
        del sessions[session_id]
    return {
        "status": "reset",
        "session_id": session_id
    }