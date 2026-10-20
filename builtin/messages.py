"""Read a supported WeChat 4 message schema without interpreting chat as code."""

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_TEXT = 1024 * 1024


def decode_text(value):
    """Decode bounded UTF-8/Zstandard fields."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        if value.startswith(b"\x28\xb5\x2f\xfd"):
            import zstandard
            value = zstandard.ZstdDecompressor().decompress(value, max_output_size=MAX_TEXT)
        if len(value) > MAX_TEXT:
            raise ValueError("Message field exceeds supported size")
        return value.decode("utf-8", errors="replace").strip("\0")
    text = str(value)
    if len(text) > MAX_TEXT:
        raise ValueError("Message field exceeds supported size")
    return text


def normalize_record(row: dict, *, account: str, shard: str, table: str) -> dict:
    """Use authenticated metadata for mentions, never the displayed message body."""
    content = decode_text(row.get("message_content"))
    if not content and row.get("compress_content"):
        content = decode_text(row["compress_content"])
    sender = str(row.get("sender_username") or "")
    if sender and content.startswith(sender + ":\n"):
        content = content[len(sender) + 2:]
    mentions = []
    source = decode_text(row.get("source"))
    if source:
        try:
            root = ET.fromstring(source)
        except ET.ParseError as error:
            raise ValueError("Unsupported message source format") from error
        if root.tag != "msgsource":
            raise ValueError("Unsupported message source schema")
        for node in root.findall("atuserlist"):
            mentions.extend(s.strip() for s in (node.text or "").split(",") if s.strip())
    server_id = int(row.get("server_id") or 0)
    if server_id > 0:
        identity = str(server_id)
    else:
        if not row.get("local_id"):
            raise ValueError("Message lacks stable identity")
        identity = "local:" + hashlib.sha256(f"{account}\0{shard}\0{table}\0{row['local_id']}".encode()).hexdigest()
    return {"id": identity, "createTime": int(row.get("create_time") or 0),
            "content": content or "[非文本消息]", "senderUsername": sender,
            "atUsernames": list(dict.fromkeys(mentions))}


def list_groups(connection: sqlite3.Connection) -> list[dict]:
    """Resolve group names from the account's contact database."""
    columns = {row[1] for row in connection.execute('PRAGMA table_info("contact")')}
    if not {"username", "nick_name", "remark"}.issubset(columns):
        raise ValueError("Unsupported contact schema")
    rows = connection.execute("SELECT username,nick_name,remark FROM contact WHERE username LIKE ? ORDER BY username", ("%@chatroom",))
    return [{"username": row[0], "name": row[2] or row[1] or row[0], "isGroup": True} for row in rows]


def read_messages(connection: sqlite3.Connection, group_id: str, limit: int, offset: int) -> dict:
    """Page one shard using only a deterministic, hashed table identifier."""
    if not re.fullmatch(r"[^\s\x00'\";]+@chatroom", group_id):
        raise ValueError("Invalid group identifier")
    if not 1 <= limit <= 200000 or offset < 0:
        raise ValueError("Invalid page bounds")
    table = "Msg_" + hashlib.md5(group_id.encode()).hexdigest()
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if not columns:
        return {"messages": [], "hasMore": False}
    if not {"local_id", "server_id", "create_time", "message_content", "real_sender_id", "source"}.issubset(columns):
        raise ValueError("Unsupported message schema: structured source required")
    extra = ",m.compress_content" if "compress_content" in columns else ""
    cursor = connection.execute(
        f'SELECT m.local_id,m.server_id,m.create_time,m.message_content,m.source,n.user_name AS sender_username{extra} '
        f'FROM "{table}" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id '
        'ORDER BY m.create_time DESC,m.local_id DESC LIMIT ? OFFSET ?', (limit + 1, offset))
    names = [column[0] for column in cursor.description]
    rows = [dict(zip(names, row)) for row in cursor]
    return {"messages": rows[:limit], "hasMore": len(rows) > limit, "table": table}


class LocalMessageReader:
    """Keep explicit account binding for a set of database shards."""

    def __init__(self, account: str, storage: Path):
        if not account or storage.name != "db_storage" or storage.parent.name != account:
            raise ValueError("Account storage does not match explicit binding")
        self.account, self.storage = account, storage.resolve()
