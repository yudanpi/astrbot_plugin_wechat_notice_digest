"""Private stdio reader worker; stdout is reserved for request-correlated JSON."""

import hashlib
import json
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path

from .bootstrap import BootstrapController
from .credentials import CredentialStore
from .messages import LocalMessageReader, list_groups, normalize_record, read_messages
from .snapshot import SnapshotChangedError, decrypt_snapshot, read_snapshot


def signature(path):
    result = []
    for name in (path, Path(str(path) + "-wal")):
        try:
            info = name.stat()
            result.append((info.st_size, info.st_mtime_ns, info.st_ino))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


class ReaderRuntime:
    """Use at most two in-memory database connections; never write plaintext files."""

    def __init__(self, data_dir, storage, account):
        self.account, self.storage = account, Path(storage).resolve()
        self.reader = LocalMessageReader(account, self.storage)
        binding = hashlib.sha256((account + "\0" + str(self.storage)).encode()).hexdigest()[:24]
        self.credentials = CredentialStore(Path(data_dir) / "builtin" / binding)
        self.bootstrap = BootstrapController(self.storage, account, self.credentials)
        self.cache = OrderedDict()
        self.key = None

    def connection(self, path):
        before = signature(path)
        cached = self.cache.get(path)
        if cached and cached[0] == before:
            self.cache.move_to_end(path)
            return cached[1]
        data = read_snapshot(path)
        if signature(path) != before:
            raise SnapshotChangedError("Database changed; retry next scan")
        plaintext = decrypt_snapshot(data, self.key)
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(plaintext)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception:
            connection.close()
            raise
        if cached:
            cached[1].close()
        self.cache[path] = (before, connection)
        self.cache.move_to_end(path)
        while len(self.cache) > 2:
            self.cache.popitem(last=False)[1][1].close()
        return connection

    def status(self):
        state = self.bootstrap.status()
        if state["state"] == "idle" and self.credentials.path.exists():
            try:
                self.credentials.load(self.account)
                state = {"state": "ready", "error": ""}
            except RuntimeError:
                state = {"state": "error", "error": "读取凭据不可用，请在本机重新初始化"}
        return state

    def dispatch(self, method, params):
        if method == "status":
            return self.status()
        if method == "initialize":
            self.bootstrap.start()
            return self.bootstrap.status()
        if method != "get":
            raise ValueError("Unsupported worker method")
        if self.status()["state"] != "ready":
            raise RuntimeError("Reader not initialized")
        self.key = self.credentials.load(self.account)
        path = params.pop("path")
        base = {"status": "success", "account": self.account, "source": "realtime"}
        if path == "/api/chat/accounts":
            return {**base, "default_account": self.account, "accounts": [self.account]}
        if params.get("account") != self.account:
            raise ValueError("Account binding mismatch")
        if path == "/api/chat/sessions":
            groups = list_groups(self.connection(self.storage / "contact/contact.db"))
            limit = min(int(params.get("limit", 2000)), 2000)
            return {**base, "sessions": groups[:limit]}
        if path == "/api/chat/messages":
            group = str(params["username"])
            limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
            if not 1 <= limit <= 500 or not 0 <= offset <= 100000:
                raise ValueError("Unsupported page bounds")
            shards = sorted((self.storage / "message").glob("message_[0-9]*.db"))
            if not shards:
                raise RuntimeError("Message shards unavailable")
            rows = []
            for shard in shards:
                response = read_messages(self.connection(shard), group, min(offset + limit + 1, 200000), 0)
                for row in response["messages"]:
                    item = normalize_record(row, account=self.account, shard=shard.name, table=response["table"])
                    item["_local_id"] = int(row["local_id"])
                    rows.append(item)
            rows.sort(key=lambda row: (row["createTime"], row["_local_id"], row["id"]), reverse=True)
            unique = list({row["id"]: row for row in reversed(rows)}.values())
            unique.sort(key=lambda row: (row["createTime"], row["_local_id"], row["id"]), reverse=True)
            return {**base, "messages": unique[offset:offset + limit], "hasMore": len(unique) > offset + limit}
        raise ValueError("Unsupported reader resource")

    def close(self):
        self.bootstrap.cancel()
        self.bootstrap.wait(2)
        for _, connection in self.cache.values():
            connection.close()
        self.cache.clear()
        self.key = None


def main():
    runtime = None
    try:
        config = json.loads(sys.stdin.buffer.readline(8 * 1024 * 1024))
        runtime = ReaderRuntime(**config)
        for line in sys.stdin.buffer:
            request_id = None
            try:
                if len(line) > 8 * 1024 * 1024:
                    raise ValueError("Request limit")
                request = json.loads(line)
                request_id = request["id"]
                result = runtime.dispatch(request["method"], request.get("params", {}))
                response = {"id": request_id, "result": result}
            except Exception as error:
                response = {"id": request_id, "error": type(error).__name__ + ": reader operation failed; check initialization, login and schema"}
            encoded = json.dumps(response, ensure_ascii=False).encode()
            if len(encoded) >= 8 * 1024 * 1024:
                encoded = json.dumps({"id": request_id, "error": "Reader response exceeds limit"}).encode()
            sys.stdout.buffer.write(encoded + b"\n")
            sys.stdout.buffer.flush()
    finally:
        if runtime:
            runtime.close()


if __name__ == "__main__":
    main()
