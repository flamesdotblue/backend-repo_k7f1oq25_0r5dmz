import os
import json
import hashlib
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from database import db, create_document
from schemas import Room, Report

app = FastAPI(title="TalkTangle Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- Utilities --------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def random_room_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(random.choice(alphabet) for _ in range(length))


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def get_prev_hash() -> Optional[str]:
    if db is None:
        return None
    last = db["auditledger"].find_one(sort=[("created_at", -1)])
    return last.get("record_hash") if last else None


def append_audit(record: Dict[str, Any]) -> str:
    prev_hash = get_prev_hash() or ""
    record_json_str = json.dumps(record, separators=(",", ":"), sort_keys=True)
    record_hash = sha256_hex(prev_hash + record_json_str)
    doc = {
        "record_json": record,
        "record_hash": record_hash,
        "prev_hash": prev_hash or None,
        "created_at": now_utc(),
    }
    result = db["auditledger"].insert_one(doc)
    return str(result.inserted_id)


def request_ip_ua(req: Request) -> Dict[str, Optional[str]]:
    # Try headers first (supports proxies), then client host
    xff = req.headers.get("x-forwarded-for") or req.headers.get("X-Forwarded-For")
    ip = (xff.split(",")[0].strip() if xff else None) or (req.client.host if req.client else None)
    ua = req.headers.get("user-agent")
    return {"ip": ip, "user_agent": ua}


# -------------------- WebSocket Manager --------------------

class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[str, set[WebSocket]] = {}
        self.socket_meta: Dict[str, Dict[str, Any]] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()

    def add_to_room(self, room: str, websocket: WebSocket):
        self.rooms.setdefault(room, set()).add(websocket)

    def remove_from_room(self, room: str, websocket: WebSocket):
        if room in self.rooms and websocket in self.rooms[room]:
            self.rooms[room].remove(websocket)
            if not self.rooms[room]:
                del self.rooms[room]

    async def broadcast(self, room: str, message: Dict[str, Any]):
        if room not in self.rooms:
            return
        dead = []
        for ws in list(self.rooms[room]):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.rooms[room].discard(ws)

manager = ConnectionManager()


# -------------------- HTTP Endpoints --------------------

@app.get("/")
def read_root():
    return {"message": "TalkTangle Backend Running"}


@app.get("/create-room")
async def create_room(room_type: str = Query("private", pattern="^(public|private)$")):
    room_id = "GLOBAL" if room_type == "public" else random_room_code(8)

    # Upsert room
    expires = now_utc() + timedelta(hours=24) if room_type == "private" else None
    room_doc = {
        "id": room_id,
        "type": room_type,
        "created_at": now_utc(),
        "expires_at": expires,
    }
    db["room"].update_one({"id": room_id}, {"$setOnInsert": room_doc}, upsert=True)

    append_audit({
        "event_type": "create_room",
        "room_id": room_id,
        "timestamp": now_utc().isoformat(),
    })
    return {"room": room_id}


def generate_snippet(language: Optional[str]) -> Dict[str, str]:
    lang = (language or "javascript").lower()
    if lang in ["js", "javascript"]:
        code = "\n".join([
            "// 8-line demo server",
            "const http = require('http')",
            "const s = http.createServer((q,r)=>{",
            "  r.writeHead(200,{ 'Content-Type':'application/json' })",
            "  r.end(JSON.stringify({ ok:true, t: Date.now() }))",
            "})",
            "s.listen(3000, ()=> console.log('ok'))",
            "module.exports = s",
        ])
        return {"language": "javascript", "code": code}
    if lang in ["py", "python"]:
        code = "\n".join([
            "# 8-line demo server",
            "import json, http.server, socketserver",
            "class H(http.server.SimpleHTTPRequestHandler):",
            "  def do_GET(self):",
            "    self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()",
            "    self.wfile.write(json.dumps({'ok':True,'t':__import__('time').time()}).encode())",
            "socketserver.TCPServer(('',3000),H).serve_forever()",
            "",
        ])
        return {"language": "python", "code": code}
    if lang in ["ts", "typescript"]:
        code = "\n".join([
            "// 8-line TS demo",
            "import http from 'http'",
            "const s = http.createServer((_q, r)=>{",
            "  r.writeHead(200,{ 'Content-Type':'application/json' })",
            "  r.end(JSON.stringify({ ok:true, t: Date.now() }))",
            "})",
            "s.listen(3000, ()=> console.log('ok'))",
            "export default s",
        ])
        return {"language": "typescript", "code": code}
    # default minimal
    code = "\n".join([
        "// minimal snippet",
        "console.log('hello')",
        "for(let i=0;i<3;i++) console.log(i)",
        "function add(a,b){return a+b}",
        "console.log(add(2,3))",
        "// end",
        "",
        "",
    ])
    return {"language": "javascript", "code": code}


@app.post("/generate")
async def generate(request: Request, payload: Dict[str, Any] = Body(...)):
    room_id = payload.get("room")
    language = payload.get("language")
    if not room_id:
        raise HTTPException(status_code=400, detail="room is required")

    snippet = generate_snippet(language)

    # Broadcast to room
    await manager.broadcast(room_id, {"type": "new_code", "room": room_id, **snippet})

    # Audit
    meta = request_ip_ua(request)
    event = {
        "event_type": "generate",
        "room_id": room_id,
        "socket_id": None,
        "local_id": payload.get("local_id"),
        "ip": meta.get("ip"),
        "user_agent": meta.get("user_agent"),
        "timestamp": now_utc().isoformat(),
        "payload_hash": sha256_hex(json.dumps(snippet, sort_keys=True)),
    }
    append_audit(event)

    return {"ok": True, **snippet}


@app.post("/report")
async def report(request: Request, payload: Dict[str, Any] = Body(...)):
    room_id = payload.get("room")
    socket_id = payload.get("socket_id")
    reason = payload.get("reason")
    if not room_id or not reason:
        raise HTTPException(status_code=400, detail="room and reason are required")

    report_doc = Report(
        room_id=room_id,
        reported_socket=socket_id,
        reporter_local_id=payload.get("local_id"),
        report_text=reason,
    )
    create_document("report", report_doc)

    meta = request_ip_ua(request)
    append_audit({
        "event_type": "report",
        "room_id": room_id,
        "socket_id": socket_id,
        "local_id": payload.get("local_id"),
        "ip": meta.get("ip"),
        "user_agent": meta.get("user_agent"),
        "timestamp": now_utc().isoformat(),
        "payload_hash": sha256_hex(json.dumps({"reason": reason}, sort_keys=True)),
    })

    return {"ok": True}


# -------------------- WebSocket Endpoint --------------------

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    sock_id = id(websocket)
    try:
        # Log connection
        meta_headers = websocket.headers
        ip = meta_headers.get("x-forwarded-for") or meta_headers.get("X-Forwarded-For") or (websocket.client.host if websocket.client else None)
        ua = meta_headers.get("user-agent")
        append_audit({
            "event_type": "connection",
            "socket_id": str(sock_id),
            "ip": ip,
            "user_agent": ua,
            "timestamp": now_utc().isoformat(),
        })

        room_joined: Optional[str] = None
        local_id: Optional[str] = None

        while True:
            data = await websocket.receive_json()
            evt = data.get("type")

            if evt == "join_room":
                room = data.get("room")
                local_id = data.get("local_id")
                if not room:
                    await websocket.send_json({"type": "error", "error": "room is required"})
                    continue
                manager.add_to_room(room, websocket)
                room_joined = room
                append_audit({
                    "event_type": "join_room",
                    "room_id": room,
                    "socket_id": str(sock_id),
                    "local_id": local_id,
                    "ip": ip,
                    "user_agent": ua,
                    "timestamp": now_utc().isoformat(),
                    "payload_hash": sha256_hex(json.dumps({"room": room, "local_id": local_id}, sort_keys=True)),
                })
                await websocket.send_json({"type": "joined", "room": room})

            elif evt == "leave_room":
                room = data.get("room") or room_joined
                manager.remove_from_room(room, websocket)
                append_audit({
                    "event_type": "leave_room",
                    "room_id": room,
                    "socket_id": str(sock_id),
                    "local_id": local_id,
                    "ip": ip,
                    "user_agent": ua,
                    "timestamp": now_utc().isoformat(),
                    "payload_hash": sha256_hex(json.dumps({"room": room, "local_id": local_id}, sort_keys=True)),
                })
                await websocket.send_json({"type": "left", "room": room})

            elif evt == "send_message":
                room = data.get("room") or room_joined
                text = data.get("text")
                if not room or text is None:
                    await websocket.send_json({"type": "error", "error": "room and text required"})
                    continue
                message_payload = {"type": "message", "room": room, "text": text, "local_id": local_id}
                await manager.broadcast(room, message_payload)
                append_audit({
                    "event_type": "message_send",
                    "room_id": room,
                    "socket_id": str(sock_id),
                    "local_id": local_id,
                    "ip": ip,
                    "user_agent": ua,
                    "timestamp": now_utc().isoformat(),
                    "payload_hash": sha256_hex(json.dumps({"text": text}, sort_keys=True)),
                })

            else:
                await websocket.send_json({"type": "error", "error": f"unknown event {evt}"})

    except WebSocketDisconnect:
        # disconnect log
        append_audit({
            "event_type": "disconnect",
            "socket_id": str(sock_id),
            "timestamp": now_utc().isoformat(),
        })
    except Exception as e:
        await websocket.close(code=1011)
        append_audit({
            "event_type": "ws_error",
            "socket_id": str(sock_id),
            "timestamp": now_utc().isoformat(),
            "error": str(e),
        })


# -------------------- Diagnostics --------------------

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    import os as _os
    response["database_url"] = "✅ Set" if _os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if _os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
