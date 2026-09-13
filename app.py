"""
WhatsApp Interior Estimate Bot

Updated Stage 3 Flow:
  User sends start
  -> Bot asks if user has a floor plan
  -> If yes: user uploads image/PDF
  -> AI analyzes floor plan room-by-room
  -> AI estimates relevant sqft for rooms that matter
  -> AI generates modular / loose / either furniture classification
  -> AI lists assumptions, missing info, ambiguities, confidence
  -> User confirms or edits analysis
  -> User selects style
  -> User selects budget
  -> Python pricing logic creates rough estimate
  -> Bot returns estimate and room-wise scope
"""

import os
import re
import json
import base64
import logging
import requests
import threading
import time

from dotenv import load_dotenv
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from openai import OpenAI

from pricing import STYLE_TIERS, BUDGET_RANGES, estimate_cost, compare_to_budget


load_dotenv()


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

missing_keys = []

if not OPENAI_API_KEY:
    missing_keys.append("OPENAI_API_KEY")

if not TWILIO_ACCOUNT_SID:
    missing_keys.append("TWILIO_ACCOUNT_SID")

if not TWILIO_AUTH_TOKEN:
    missing_keys.append("TWILIO_AUTH_TOKEN")

if missing_keys:
    raise RuntimeError(
        "Missing environment variables in .env file: "
        + ", ".join(missing_keys)
    )


OPENAI_MODEL = "gpt-4.1-mini"

openai_client = OpenAI(api_key=OPENAI_API_KEY)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = Flask(__name__)


# In-memory session storage.
# Fine for PoC. Resets when server restarts.
SESSIONS = {}

STYLE_KEYS = list(STYLE_TIERS.keys())
BUDGET_KEYS = list(BUDGET_RANGES.keys())


# ---------------- BASIC HELPERS ----------------

def reply_text(text: str):
    twilio_response = MessagingResponse()
    twilio_response.message(text)

    return Response(
        str(twilio_response),
        mimetype="application/xml",
    )


def reset_session(user_id: str):
    SESSIONS[user_id] = {
        "state": "ASK_FLOOR_PLAN",
    }


def get_session(user_id: str):
    if user_id not in SESSIONS:
        reset_session(user_id)

    return SESSIONS[user_id]


def parse_number_choice(text: str, max_choice: int):
    match = re.search(r"\d+", text)

    if not match:
        return None

    choice = int(match.group())

    if choice < 1 or choice > max_choice:
        return None

    return choice


def clamp_confidence(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return 50

    return max(0, min(100, value))


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def download_twilio_media(media_url: str) -> bytes:
    response = requests.get(
        media_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=45,
    )

    response.raise_for_status()
    return response.content


# ---------------- PROMPTS ----------------

def floor_plan_choice_prompt() -> str:
    return (
        "Welcome! Let's estimate your interior fit-out cost.\n\n"
        "Do you have a floor plan ready?\n\n"
        "Reply with a number:\n"
        "1. Yes, I'll upload a floor plan\n"
        "2. No, I'll enter room details manually"
    )


def manual_home_type_prompt() -> str:
    return (
        "No problem.\n\n"
        "What type of home is it?\n\n"
        "Examples:\n"
        "2BHK\n"
        "3BHK\n"
        "4BHK villa\n"
        "Studio apartment"
    )


def manual_room_details_prompt() -> str:
    return (
        "Now send the approximate room sizes.\n\n"
        "Use one room per line like this:\n\n"
        "Living Room = 240 sqft\n"
        "Kitchen = 110 sqft\n"
        "Master Bedroom = 180 sqft\n"
        "Bedroom 2 = 140 sqft\n"
        "Bedroom 3 = 130 sqft\n"
        "Bathroom 1 = 45 sqft\n"
        "Bathroom 2 = 40 sqft\n"
        "Pooja Room = 35 sqft\n"
        "Utility = 50 sqft\n\n"
        "If you don't know exact sizes, write approximate."
    )


def style_prompt() -> str:
    lines = [
        "Which style best matches the home you'd love to build?",
        "",
        "Reply with a number:",
    ]

    for index, key in enumerate(STYLE_KEYS, start=1):
        tier = STYLE_TIERS[key]
        lines.append(f"{index}. {tier['label']} — {tier['tagline']}")

    return "\n".join(lines)


def budget_prompt() -> str:
    lines = [
        "Which investment feels comfortable for your home interiors?",
        "",
        "Reply with a number:",
    ]

    for index, key in enumerate(BUDGET_KEYS, start=1):
        budget = BUDGET_RANGES[key]
        lines.append(f"{index}. {budget['label']}")

    return "\n".join(lines)


# ---------------- JSON EXTRACTION ----------------

def extract_json_from_text(text: str) -> dict:
    cleaned = text.strip()

    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)

    if not match:
        raise ValueError("No JSON object found in model response")

    return json.loads(match.group())


# ---------------- NORMALIZATION ----------------

def normalize_analysis(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raw = {}

    home_summary = raw.get("home_summary", {})
    if not isinstance(home_summary, dict):
        home_summary = {}

    rooms_raw = raw.get("rooms", [])
    if not isinstance(rooms_raw, list):
        rooms_raw = []

    rooms = []

    for index, room in enumerate(rooms_raw[:20], start=1):
        if not isinstance(room, dict):
            continue

        room_id = str(room.get("id") or f"room_{index:03d}")
        name = str(room.get("name") or f"Room {index}").strip()
        room_type = str(room.get("type") or "general").strip()

        area_sqft = safe_float(room.get("area_sqft"), 0.0)
        area_source = str(room.get("area_source") or "estimated").strip()
        confidence = clamp_confidence(room.get("confidence", 50))

        usable_wall_runs = room.get("usable_wall_runs", [])
        if not isinstance(usable_wall_runs, list):
            usable_wall_runs = []

        cleaned_wall_runs = []
        for wall_index, wall in enumerate(usable_wall_runs[:8], start=1):
            if not isinstance(wall, dict):
                continue

            cleaned_wall_runs.append(
                {
                    "id": str(wall.get("id") or f"{room_id}_wall_{wall_index:02d}"),
                    "wall_name": str(wall.get("wall_name") or f"Wall {wall_index}"),
                    "length_ft": safe_float(wall.get("length_ft"), 0.0),
                    "source": str(wall.get("source") or "estimated"),
                    "confidence": clamp_confidence(wall.get("confidence", 50)),
                }
            )

        rooms.append(
            {
                "id": room_id,
                "name": name,
                "type": room_type,
                "area_sqft": area_sqft,
                "area_source": area_source,
                "confidence": confidence,
                "usable_wall_runs": cleaned_wall_runs,
            }
        )

    scope_raw = raw.get("suggested_scope", [])
    if not isinstance(scope_raw, list):
        scope_raw = []

    suggested_scope = []

    for index, item in enumerate(scope_raw[:35], start=1):
        if not isinstance(item, dict):
            continue

        classification = str(item.get("classification") or "modular").lower().strip()

        if classification not in ["modular", "loose", "either"]:
            classification = "modular"

        quantity = item.get("quantity", 1)

        try:
            quantity = int(float(quantity))
        except (TypeError, ValueError):
            quantity = 1

        if quantity <= 0:
            quantity = 1

        suggested_scope.append(
            {
                "id": str(item.get("id") or f"item_{index:03d}"),
                "room_id": str(item.get("room_id") or "unknown_room"),
                "room_name": str(item.get("room_name") or "General"),
                "item": str(item.get("item") or "Furniture Item"),
                "classification": classification,
                "quantity": quantity,
                "size": str(item.get("size") or "Standard"),
                "basis": str(item.get("basis") or "inferred"),
                "confidence": clamp_confidence(item.get("confidence", 50)),
            }
        )

    kitchen = raw.get("kitchen", {})
    if not isinstance(kitchen, dict):
        kitchen = {}

    missing_raw = raw.get("missing_information", [])
    assumptions_raw = raw.get("assumptions", [])
    ambiguities_raw = raw.get("ambiguities", [])

    missing_information = normalize_note_list(missing_raw, "missing")
    assumptions = normalize_note_list(assumptions_raw, "assumption")
    ambiguities = normalize_note_list(ambiguities_raw, "ambiguity")

    total_relevant_sqft = safe_float(home_summary.get("total_relevant_sqft"), 0.0)

    if total_relevant_sqft <= 0:
        total_relevant_sqft = sum(room["area_sqft"] for room in rooms if room["area_sqft"] > 0)

    if total_relevant_sqft <= 0:
        total_relevant_sqft = 1200.0

    normalized = {
        "home_summary": {
            "home_type": str(home_summary.get("home_type") or "Unknown"),
            "total_relevant_sqft": round(total_relevant_sqft, 1),
            "area_source": str(home_summary.get("area_source") or "estimated"),
            "confidence": clamp_confidence(home_summary.get("confidence", 55)),
            "summary": str(
                home_summary.get("summary")
                or "Preliminary room-wise interior scope generated."
            ),
        },
        "rooms": rooms,
        "suggested_scope": suggested_scope,
        "kitchen": {
            "exists": bool(kitchen.get("exists", False)),
            "layout": str(kitchen.get("layout") or "Unknown"),
            "layout_source": str(kitchen.get("layout_source") or "estimated"),
            "confidence": clamp_confidence(kitchen.get("confidence", 50)),
            "notes": str(kitchen.get("notes") or ""),
        },
        "missing_information": missing_information,
        "assumptions": assumptions,
        "ambiguities": ambiguities,
    }

    if not normalized["rooms"]:
        normalized["rooms"] = fallback_rooms(total_relevant_sqft)

    if not normalized["suggested_scope"]:
        normalized["suggested_scope"] = fallback_scope(normalized["rooms"])

    return normalized


def normalize_note_list(raw, prefix: str) -> list:
    if not isinstance(raw, list):
        return []

    notes = []

    for index, note in enumerate(raw[:12], start=1):
        if isinstance(note, dict):
            note_id = str(note.get("id") or f"{prefix}_{index:03d}")
            text = str(note.get("text") or note.get("reason") or note.get("field") or "").strip()
            confidence = clamp_confidence(note.get("confidence", 60))
        else:
            note_id = f"{prefix}_{index:03d}"
            text = str(note).strip()
            confidence = 60

        if text:
            notes.append(
                {
                    "id": note_id,
                    "text": text,
                    "confidence": confidence,
                }
            )

    return notes


def fallback_rooms(total_area: float) -> list:
    return [
        {
            "id": "room_001",
            "name": "Living Room",
            "type": "living",
            "area_sqft": round(total_area * 0.22, 1),
            "area_source": "inferred",
            "confidence": 45,
            "usable_wall_runs": [],
        },
        {
            "id": "room_002",
            "name": "Kitchen",
            "type": "kitchen",
            "area_sqft": round(total_area * 0.10, 1),
            "area_source": "inferred",
            "confidence": 45,
            "usable_wall_runs": [],
        },
        {
            "id": "room_003",
            "name": "Master Bedroom",
            "type": "bedroom",
            "area_sqft": round(total_area * 0.16, 1),
            "area_source": "inferred",
            "confidence": 45,
            "usable_wall_runs": [],
        },
    ]


def fallback_scope(rooms: list) -> list:
    scope = []
    item_index = 1

    for room in rooms:
        room_name = room["name"].lower()
        area = room["area_sqft"]

        if "kitchen" in room_name:
            scope.append(make_scope_item(item_index, room, "Modular Kitchen", "modular", 1, "Standard", "inferred", 50))
            item_index += 1

        elif "bedroom" in room_name or "master" in room_name:
            bed_size = "King" if area >= 160 else "Queen"
            scope.append(make_scope_item(item_index, room, f"{bed_size} Size Bed", "either", 1, bed_size, "inferred", 50))
            item_index += 1
            scope.append(make_scope_item(item_index, room, "Wardrobe with Loft", "modular", 1, "Large" if area >= 160 else "Medium", "inferred", 50))
            item_index += 1

        elif "living" in room_name:
            scope.append(make_scope_item(item_index, room, "TV Unit", "modular", 1, "Large" if area >= 220 else "Medium", "inferred", 50))
            item_index += 1
            scope.append(make_scope_item(item_index, room, "Sofa", "loose", 1, "Standard", "inferred", 50))
            item_index += 1

        elif "bath" in room_name:
            scope.append(make_scope_item(item_index, room, "Vanity", "modular", 1, "Standard", "inferred", 50))
            item_index += 1

    return scope


def make_scope_item(index, room, item, classification, quantity, size, basis, confidence):
    return {
        "id": f"item_{index:03d}",
        "room_id": room["id"],
        "room_name": room["name"],
        "item": item,
        "classification": classification,
        "quantity": quantity,
        "size": size,
        "basis": basis,
        "confidence": confidence,
    }



def low_confidence_fallback_analysis(reason: str = "AI analysis failed") -> dict:
    """
    Returns a usable low-confidence room-wise analysis instead of failing completely.
    This keeps the WhatsApp flow alive even if OpenAI/image parsing fails.
    """

    rooms = [
        {
            "id": "room_001",
            "name": "Living Room",
            "type": "living",
            "area_sqft": 240,
            "area_source": "inferred",
            "confidence": 35,
            "usable_wall_runs": [],
        },
        {
            "id": "room_002",
            "name": "Kitchen",
            "type": "kitchen",
            "area_sqft": 110,
            "area_source": "inferred",
            "confidence": 35,
            "usable_wall_runs": [],
        },
        {
            "id": "room_003",
            "name": "Master Bedroom",
            "type": "bedroom",
            "area_sqft": 180,
            "area_source": "inferred",
            "confidence": 35,
            "usable_wall_runs": [],
        },
        {
            "id": "room_004",
            "name": "Bedroom 2",
            "type": "bedroom",
            "area_sqft": 140,
            "area_source": "inferred",
            "confidence": 35,
            "usable_wall_runs": [],
        },
        {
            "id": "room_005",
            "name": "Bathrooms / Utility",
            "type": "bathroom_utility",
            "area_sqft": 90,
            "area_source": "inferred",
            "confidence": 30,
            "usable_wall_runs": [],
        },
    ]

    scope = [
        make_scope_item(1, rooms[0], "TV Unit", "modular", 1, "Large", "inferred", 40),
        make_scope_item(2, rooms[0], "Sofa", "loose", 1, "Standard", "inferred", 35),
        make_scope_item(3, rooms[1], "Modular Kitchen", "modular", 1, "Standard", "inferred", 40),
        make_scope_item(4, rooms[1], "Tall Unit", "modular", 1, "Standard", "inferred", 35),
        make_scope_item(5, rooms[2], "King Size Bed", "either", 1, "King", "inferred", 40),
        make_scope_item(6, rooms[2], "Wardrobe with Loft", "modular", 1, "Large", "inferred", 40),
        make_scope_item(7, rooms[3], "Queen Size Bed", "either", 1, "Queen", "inferred", 40),
        make_scope_item(8, rooms[3], "Wardrobe with Loft", "modular", 1, "Medium", "inferred", 40),
        make_scope_item(9, rooms[4], "Vanity", "modular", 2, "Standard", "inferred", 35),
    ]

    return {
        "home_summary": {
            "home_type": "Unknown apartment",
            "total_relevant_sqft": 760,
            "area_source": "inferred",
            "confidence": 30,
            "summary": "Low-confidence fallback analysis generated because the uploaded floor plan could not be analyzed cleanly.",
        },
        "rooms": rooms,
        "suggested_scope": scope,
        "kitchen": {
            "exists": True,
            "layout": "Unknown",
            "layout_source": "inferred",
            "confidence": 30,
            "notes": "Kitchen exists in fallback scope, but exact layout could not be confirmed.",
        },
        "missing_information": [
            {
                "id": "missing_001",
                "text": "Exact room dimensions could not be read clearly from the uploaded file.",
                "confidence": 90,
            },
            {
                "id": "missing_002",
                "text": "Usable wall lengths for wardrobes, TV unit, and kitchen cabinets need confirmation.",
                "confidence": 85,
            },
        ],
        "assumptions": [
            {
                "id": "assumption_001",
                "text": "Room areas and furniture scope are inferred from a typical Indian apartment layout.",
                "confidence": 70,
            },
            {
                "id": "assumption_002",
                "text": reason,
                "confidence": 90,
            },
        ],
        "ambiguities": [
            {
                "id": "ambiguity_001",
                "text": "Uploaded floor plan could not be converted into reliable structured room-wise data.",
                "confidence": 90,
            }
        ],
    }


# ---------------- OPENAI ANALYSIS ----------------

def build_media_content(media_bytes: bytes, media_type: str) -> dict:
    base64_file = base64.b64encode(media_bytes).decode("utf-8")
    data_url = f"data:{media_type};base64,{base64_file}"

    if media_type.startswith("image/"):
        return {
            "type": "input_image",
            "image_url": data_url,
            "detail": "high",
        }

    if media_type == "application/pdf":
        return {
            "type": "input_file",
            "filename": "floor_plan.pdf",
            "file_data": data_url,
        }

    raise ValueError(f"Unsupported media type: {media_type}")


def analysis_json_instruction() -> str:
    return """
Return ONLY valid JSON. No markdown. No explanation.

Use this exact JSON structure:

{
  "home_summary": {
    "home_type": "2BHK / 3BHK / villa / apartment / unknown",
    "total_relevant_sqft": 1250,
    "area_source": "detected / estimated / inferred / user_provided",
    "confidence": 75,
    "summary": "Short readable summary of the home."
  },
  "rooms": [
    {
      "id": "room_001",
      "name": "Master Bedroom",
      "type": "bedroom",
      "area_sqft": 180,
      "area_source": "detected / estimated / inferred / user_provided",
      "confidence": 80,
      "usable_wall_runs": [
        {
          "id": "wall_001",
          "wall_name": "long wall near entrance",
          "length_ft": 10,
          "source": "detected / estimated / inferred",
          "confidence": 65
        }
      ]
    }
  ],
  "suggested_scope": [
    {
      "id": "item_001",
      "room_id": "room_001",
      "room_name": "Master Bedroom",
      "item": "Wardrobe with Loft",
      "classification": "modular",
      "quantity": 1,
      "size": "Large",
      "basis": "detected / estimated / inferred / user_provided",
      "confidence": 75
    }
  ],
  "kitchen": {
    "exists": true,
    "layout": "L-shaped / U-shaped / parallel / straight / island / unknown",
    "layout_source": "detected / estimated / inferred",
    "confidence": 70,
    "notes": "Short kitchen notes."
  },
  "missing_information": [
    {
      "id": "missing_001",
      "text": "Exact wardrobe wall length is not visible.",
      "confidence": 80
    }
  ],
  "assumptions": [
    {
      "id": "assumption_001",
      "text": "Wardrobe size inferred from bedroom area.",
      "confidence": 70
    }
  ],
  "ambiguities": [
    {
      "id": "ambiguity_001",
      "text": "Bedroom 2 label is unclear.",
      "confidence": 60
    }
  ]
}

Rules:
- Do not silently guess. If something is uncertain, label it as estimated or inferred.
- Use confidence scores from 0 to 100.
- Assign stable IDs to rooms, items, walls, missing information, assumptions, and ambiguities.
- Count only rooms/areas relevant for interior scope. Do not blindly use full SBA if room-wise usable area is clearer.
- Estimate room-wise sqft wherever possible.
- Classify furniture as modular, loose, or either.
- Modular examples: wardrobes, modular kitchen, TV unit, vanity, crockery unit, shoe rack, pooja unit, storage unit, loft.
- Loose examples: sofa, dining table, chairs, mattress, coffee table.
- Either examples: beds, desks, bookshelves.
- Always determine kitchen layout if a kitchen exists.
- Use Indian interior design assumptions.
- Downgrade confidence when the image or details are unclear.
- Do not ask homeowner questions in this output. Put missing details under missing_information.
"""


def analyze_floor_plan_media(media_bytes: bytes, media_type: str) -> dict:
    media_content = build_media_content(media_bytes, media_type)

    prompt = (
        "You are an expert Indian interior-design estimation assistant. "
        "Analyze this floor plan for Stage 3 quotation preparation. "
        "Return valid JSON only. "
        "Focus on room-by-room relevant usable sqft, modular furniture scope, "
        "loose furniture scope, either-category furniture, kitchen layout, "
        "missing information, assumptions, ambiguities, and confidence. "
        "Use Indian interior design assumptions. "
        "Do not silently guess. Label uncertain values as estimated or inferred. "
        + analysis_json_instruction()
    )

    try:
        response = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        },
                        media_content,
                    ],
                }
            ],
            max_output_tokens=1600,
        )

        text = response.output_text or ""

        logger.info("OpenAI model used: %s", OPENAI_MODEL)
        logger.info("OpenAI raw floor plan analysis: %s", text)

        parsed = extract_json_from_text(text)
        return normalize_analysis(parsed)

    except Exception as error:
        logger.exception("OpenAI floor plan analysis failed, using fallback")
        return low_confidence_fallback_analysis(str(error))


def analyze_manual_room_details(home_type: str, room_details: str) -> dict:
    prompt = (
        "You are an expert Indian interior-design estimation assistant.\n\n"
        "The customer does not have a floor plan. Analyze the manually provided home and room details.\n\n"
        f"Home type: {home_type}\n\n"
        f"Room details:\n{room_details}\n\n"
        "Your job:\n"
        "1. Use the provided room-wise sqft where available.\n"
        "2. Infer missing room sizes only when necessary and label them as inferred.\n"
        "3. Suggest what is good for each room.\n"
        "4. Generate modular, loose, and either furniture scope.\n"
        "5. For bedrooms, suggest king/queen bed based on room size.\n"
        "6. For kitchen, infer likely layout based on size.\n"
        "7. Include missing information, assumptions, ambiguities, and confidence.\n\n"
        + analysis_json_instruction()
    )

    response = openai_client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    }
                ],
            }
        ],
        max_output_tokens=2200,
    )

    text = response.output_text or ""

    logger.info("OpenAI raw manual room analysis: %s", text)

    parsed = extract_json_from_text(text)
    return normalize_analysis(parsed)


# ---------------- FORMATTING ----------------

def format_room_summary(analysis: dict) -> str:
    home = analysis["home_summary"]
    rooms = analysis.get("rooms", [])

    lines = [
        "Apartment / Home Summary:",
        f"- Type: {home['home_type']}",
        f"- Relevant interior area considered: {int(home['total_relevant_sqft'])} sqft",
        f"- Area source: {home['area_source']}",
        f"- Confidence: {home['confidence']}%",
        "",
        "Room-wise area considered:",
    ]

    for room in rooms[:12]:
        lines.append(
            f"- {room['name']}: {int(room['area_sqft'])} sqft "
            f"({room['area_source']}, {room['confidence']}%)"
        )

    return "\n".join(lines)


def format_scope_summary(analysis: dict) -> str:
    scope = analysis.get("suggested_scope", [])

    if not scope:
        return "Suggested scope:\n- No scope identified."

    lines = ["Suggested furniture / interior scope:"]

    for item in scope[:20]:
        label = item["classification"].upper()

        lines.append(
            f"- {item['room_name']}: {item['item']} = {item['quantity']} "
            f"({item['size']}, {label}, {item['basis']}, {item['confidence']}%)"
        )

    return "\n".join(lines)


def format_kitchen_summary(analysis: dict) -> str:
    kitchen = analysis.get("kitchen", {})

    if not kitchen.get("exists"):
        return "Kitchen: not clearly detected."

    return (
        "Kitchen:\n"
        f"- Layout: {kitchen.get('layout', 'Unknown')}\n"
        f"- Source: {kitchen.get('layout_source', 'estimated')}\n"
        f"- Confidence: {kitchen.get('confidence', 50)}%\n"
        f"- Notes: {kitchen.get('notes', '')}"
    )


def format_notes(title: str, notes: list) -> str:
    if not notes:
        return f"{title}:\n- None listed."

    lines = [f"{title}:"]

    for note in notes[:6]:
        lines.append(
            f"- {note['text']} ({note['confidence']}%)"
        )

    return "\n".join(lines)


def format_full_analysis_for_whatsapp(analysis: dict) -> str:
    return (
        f"{format_room_summary(analysis)}\n\n"
        f"{format_kitchen_summary(analysis)}\n\n"
        f"{format_scope_summary(analysis)}\n\n"
        f"{format_notes('Missing information', analysis.get('missing_information', []))}\n\n"
        f"{format_notes('Assumptions', analysis.get('assumptions', []))}\n\n"
        "Please confirm:\n"
        "1. Yes, this looks good\n"
        "2. I want to edit / correct details"
    )


def format_estimate_summary(result: dict, analysis: dict) -> str:
    home = analysis["home_summary"]

    return (
        "Estimate Summary:\n\n"
        f"- Relevant area used: {int(home['total_relevant_sqft'])} sqft\n"
        f"- Style: {result['style_label']}\n"
        f"- Rate used: ₹{result['rate_per_sqft']} / sqft\n"
        f"- Estimated Investment: ₹{result['estimated_investment_lakhs']} Lakhs\n"
        f"- Confidence: {result['confidence']}%\n\n"
        f"{format_scope_summary(analysis)}"
    )


def build_budget_response(comparison: str, result: dict) -> str:
    if comparison == "over":
        return (
            "Budget check:\n\n"
            f"The estimate is around ₹{result['estimated_investment_lakhs']} lakh, "
            "which is above the selected budget range.\n\n"
            "Next options:\n"
            "1. Talk to an expert\n"
            "2. Show ways to reduce cost\n"
            "3. Explore later"
        )

    if comparison == "under":
        return (
            "Budget check:\n\n"
            f"The estimate is around ₹{result['estimated_investment_lakhs']} lakh, "
            "which is within or below the selected budget range.\n\n"
            "Next options:\n"
            "1. Show upgrade ideas\n"
            "2. Connect me with an expert\n"
            "3. I'm happy with this"
        )

    if comparison == "match":
        return (
            "Budget check:\n\n"
            "The selected style and expected investment are closely aligned.\n\n"
            "Next options:\n"
            "1. Talk to an expert\n"
            "2. Find interior partners\n"
            "3. Download estimate"
        )

    return (
        "Budget check:\n\n"
        "Since you're not sure about the budget yet, this estimate can be used as a starting point.\n\n"
        "Next options:\n"
        "1. Talk to an expert\n"
        "2. Explore later"
    )



# ---------------- BACKGROUND WHATSAPP SENDING ----------------

def split_message(text: str, limit: int = 1400) -> list:
    """
    Splits long WhatsApp messages so Twilio does not reject very long replies.
    """

    chunks = []
    current = ""

    for line in text.splitlines():
        if len(current) + len(line) + 1 > limit:
            if current.strip():
                chunks.append(current.strip())
            current = line
        else:
            current += "\n" + line if current else line

    if current.strip():
        chunks.append(current.strip())

    return chunks


def send_whatsapp_message(to_number: str, from_number: str, text: str):
    """
    Sends WhatsApp message using Twilio REST API.
    Used after long-running AI analysis finishes.
    """

    for chunk in split_message(text):
        twilio_client.messages.create(
            from_=from_number,
            to=to_number,
            body=chunk,
        )
        time.sleep(0.5)


def process_floor_plan_in_background(
    user_number: str,
    twilio_number: str,
    media_url: str,
    media_content_type: str,
):
    """
    Downloads and analyzes the floor plan in the background.
    This prevents Twilio webhook timeout.
    """

    try:
        logger.info("Background analysis started for %s", user_number)

        media_bytes = download_twilio_media(media_url)
        analysis = analyze_floor_plan_media(media_bytes, media_content_type)

        session = get_session(user_number)
        session["analysis"] = analysis
        session["state"] = "CONFIRM_ANALYSIS"

        message = format_full_analysis_for_whatsapp(analysis)

        send_whatsapp_message(
            to_number=user_number,
            from_number=twilio_number,
            text=message,
        )

        logger.info("Background analysis completed for %s", user_number)

    except Exception:
        logger.exception("Background floor plan analysis failed")

        send_whatsapp_message(
            to_number=user_number,
            from_number=twilio_number,
            text=(
                "I had trouble analyzing that floor plan.\n\n"
                "Please try uploading a clearer JPG/PNG image, or send 'restart' "
                "and choose manual room entry."
            ),
        )


# ---------------- ROUTES ----------------

@app.route("/", methods=["GET"])
def home():
    return "WhatsApp Interior Estimate Bot is running."


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    from_number = request.form.get("From", "unknown")
    body = (request.form.get("Body") or "").strip()
    num_media = int(request.form.get("NumMedia", "0"))

    logger.info("Incoming message from %s: %s", from_number, body)
    logger.info("Incoming media count: %s", num_media)

    session = get_session(from_number)

    if body.lower() in ["start", "/start", "hi", "hello", "restart"]:
        reset_session(from_number)
        return reply_text(floor_plan_choice_prompt())

    state = session.get("state")

    # ---------- Floor plan upload ----------
    if num_media > 0:
        media_url = request.form.get("MediaUrl0")
        media_content_type = request.form.get("MediaContentType0", "")
        twilio_number = request.form.get("To", "")

        logger.info("Media URL: %s", media_url)
        logger.info("Media content type: %s", media_content_type)
        logger.info("Twilio WhatsApp number: %s", twilio_number)

        if not media_url:
            return reply_text(
                "I received media but couldn't access it. Please try uploading again."
            )

        is_image = media_content_type.startswith("image/")
        is_pdf = media_content_type == "application/pdf"

        if not is_image and not is_pdf:
            return reply_text(
                "Please upload a floor plan as JPG, PNG, or PDF."
            )

        if not twilio_number:
            return reply_text(
                "I couldn't identify the Twilio sender number. Please try again."
            )

        session["state"] = "ANALYZING_FLOOR_PLAN"

        thread = threading.Thread(
            target=process_floor_plan_in_background,
            args=(
                from_number,
                twilio_number,
                media_url,
                media_content_type,
            ),
            daemon=True,
        )

        thread.start()

        return reply_text(
            "Got it — analyzing your floor plan room by room.\n\n"
            "This may take 20–60 seconds. I'll send the room-wise area, furniture scope, assumptions, and confidence once it's ready."
        )

    # ---------- Ask floor plan availability ----------
    if state == "ASK_FLOOR_PLAN":
        choice = parse_number_choice(body, 2)

        if choice == 1:
            session["state"] = "WAITING_FLOOR_PLAN"

            return reply_text(
                "Great. Please upload your floor plan now.\n\n"
                "Supported formats:\n"
                "- JPG\n"
                "- PNG\n"
                "- PDF"
            )

        if choice == 2:
            session["state"] = "ASK_HOME_TYPE"

            return reply_text(manual_home_type_prompt())

        return reply_text(floor_plan_choice_prompt())

    # ---------- Waiting for floor plan ----------
    if state == "WAITING_FLOOR_PLAN":
        return reply_text(
            "Please upload your floor plan as JPG, PNG, or PDF.\n\n"
            "Or send 'restart' to start again."
        )

    # ---------- Manual no-floor-plan branch ----------
    if state == "ASK_HOME_TYPE":
        if not body:
            return reply_text(manual_home_type_prompt())

        session["manual_home_type"] = body
        session["state"] = "ASK_MANUAL_ROOM_DETAILS"

        return reply_text(manual_room_details_prompt())

    if state == "ASK_MANUAL_ROOM_DETAILS":
        if not body:
            return reply_text(manual_room_details_prompt())

        try:
            analysis = analyze_manual_room_details(
                session.get("manual_home_type", "Unknown"),
                body,
            )

        except Exception:
            logger.exception("Manual room analysis failed")

            return reply_text(
                "I had trouble analyzing those room details.\n\n"
                "Please send them again like this:\n\n"
                "Living Room = 240 sqft\n"
                "Kitchen = 110 sqft\n"
                "Master Bedroom = 180 sqft"
            )

        session["analysis"] = analysis
        session["state"] = "CONFIRM_ANALYSIS"

        return reply_text(format_full_analysis_for_whatsapp(analysis))

    # ---------- Confirm / edit analysis ----------
    if state == "CONFIRM_ANALYSIS":
        normalized = body.lower()

        if normalized in ["1", "yes", "y", "correct", "looks good", "good"]:
            session["state"] = "ASK_STYLE"
            return reply_text(style_prompt())

        if normalized in ["2", "no", "n", "edit", "correct"]:
            session["state"] = "EDIT_ANALYSIS"

            return reply_text(
                "Send the corrected details in one message.\n\n"
                "Example:\n"
                "Living Room = 260 sqft, TV Unit Large\n"
                "Kitchen = 120 sqft, L-shaped modular kitchen\n"
                "Master Bedroom = 180 sqft, King bed, wardrobe with loft\n"
                "Bedroom 2 = 140 sqft, Queen bed, wardrobe"
            )

        return reply_text(
            "Please reply with:\n"
            "1. Yes, this looks good\n"
            "2. I want to edit / correct details"
        )

    if state == "EDIT_ANALYSIS":
        if not body:
            return reply_text(
                "Please send the corrected room and furniture details."
            )

        try:
            analysis = analyze_manual_room_details(
                "Corrected user details",
                body,
            )

        except Exception:
            logger.exception("Correction analysis failed")

            return reply_text(
                "I had trouble reading that correction. Please send it again in simple room-wise lines."
            )

        session["analysis"] = analysis
        session["state"] = "ASK_STYLE"

        return reply_text(
            "Got it. We'll use this corrected room-wise analysis.\n\n"
            f"{format_room_summary(analysis)}\n\n"
            f"{format_scope_summary(analysis)}\n\n"
            f"{style_prompt()}"
        )

    # ---------- Style ----------
    if state == "ASK_STYLE":
        choice = parse_number_choice(body, len(STYLE_KEYS))

        if choice is None:
            return reply_text(style_prompt())

        style_key = STYLE_KEYS[choice - 1]
        session["style_key"] = style_key
        session["state"] = "ASK_BUDGET"

        tier = STYLE_TIERS[style_key]

        return reply_text(
            f"Style selected: {tier['label']} — {tier['tagline']}\n\n"
            f"{budget_prompt()}"
        )

    # ---------- Budget ----------
    if state == "ASK_BUDGET":
        choice = parse_number_choice(body, len(BUDGET_KEYS))

        if choice is None:
            return reply_text(budget_prompt())

        budget_key = BUDGET_KEYS[choice - 1]
        session["budget_key"] = budget_key

        analysis = session["analysis"]
        area = analysis["home_summary"]["total_relevant_sqft"]
        style_key = session["style_key"]

        result = estimate_cost(area, style_key)

        comparison = compare_to_budget(
            result["estimated_investment"],
            budget_key,
        )

        session["estimate"] = result
        session["comparison"] = comparison
        session["state"] = "POST_ESTIMATE"

        return reply_text(
            f"{format_estimate_summary(result, analysis)}\n\n"
            f"{build_budget_response(comparison, result)}"
        )

    # ---------- After estimate ----------
    if state == "POST_ESTIMATE":
        if body.lower() in ["1", "talk", "expert", "consult"]:
            return reply_text(
                "Great — the next step would be a short consultation with an interior expert.\n\n"
                "For this PoC, this is where we would connect the user to a sales/design team.\n\n"
                "Send 'restart' to try another floor plan."
            )

        if body.lower() in [
            "2",
            "3",
            "later",
            "download",
            "partners",
            "upgrade",
            "reduce",
        ]:
            return reply_text(
                "Got it. In the full version, this option would trigger the next workflow.\n\n"
                "Send 'restart' to try another home."
            )

        return reply_text(
            "Thanks for trying the estimate flow.\n\n"
            "Send 'restart' to try another home."
        )

    return reply_text(
        "Something went wrong. Send 'restart' to start again."
    )


if __name__ == "__main__":
    print("🚀 WhatsApp Interior Estimate Bot running on Flask...")
    print("Local URL: http://localhost:5001")
    print("Webhook path: /whatsapp")

    app.run(
        host="0.0.0.0",
        port=5001,
        debug=True,
        use_reloader=False,
    )
    