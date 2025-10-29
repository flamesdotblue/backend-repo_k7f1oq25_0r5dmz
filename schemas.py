from pydantic import BaseModel, Field
from typing import Optional, Any, Dict
from datetime import datetime

# Collection: room
class Room(BaseModel):
    id: str = Field(..., description="Room identifier (e.g., 8-char code or GLOBAL)")
    type: str = Field(..., description="public|private")
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

# Collection: message (payload not stored here; reference only)
class Message(BaseModel):
    id: Optional[str] = None
    room_id: str
    payload_ref: Optional[str] = None
    created_at: Optional[datetime] = None
    ephemeral_flag: bool = True

# Collection: audit_ledger (append-only)
class AuditLedger(BaseModel):
    record_json: Dict[str, Any]
    record_hash: str
    prev_hash: Optional[str] = None
    created_at: Optional[datetime] = None

# Collection: report
class Report(BaseModel):
    room_id: str
    reported_socket: Optional[str] = None
    reporter_local_id: Optional[str] = None
    report_text: Optional[str] = None
    status: str = "open"
    created_at: Optional[datetime] = None

# Collection: connection
class Connection(BaseModel):
    socket_id: str
    local_id: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    connected_at: Optional[datetime] = None
    disconnected_at: Optional[datetime] = None
    room_id: Optional[str] = None
