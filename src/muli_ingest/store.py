import contextlib
import json
import sqlite3
import threading


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS objects(kind TEXT,id TEXT,data TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS files(batch TEXT,path TEXT,source_id TEXT,source_hash TEXT,status TEXT,data TEXT NOT NULL,PRIMARY KEY(batch,path));
        CREATE INDEX IF NOT EXISTS history ON files(source_id,path,source_hash,status);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,batch TEXT,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sequence(id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY,digest TEXT,batch TEXT);
        """)

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def put(self, kind, key, value):
        with self.lock:
            self.db.execute(
                "INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (kind, key, json.dumps(value, ensure_ascii=False)),
            )

    def get(self, kind, key):
        with self.lock:
            row = self.db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, key)).fetchone()
            return json.loads(row["data"]) if row else None

    def all(self, kind):
        with self.lock:
            return [
                json.loads(r["data"])
                for r in self.db.execute("SELECT data FROM objects WHERE kind=? ORDER BY rowid DESC", (kind,))
            ]

    def put_file(self, batch, source_id, file):
        with self.lock:
            self.db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?) ON CONFLICT(batch,path) DO UPDATE SET source_hash=excluded.source_hash,status=excluded.status,data=excluded.data",
                (
                    batch,
                    file["relative_path"],
                    source_id,
                    file.get("source_hash"),
                    file["copy_status"],
                    json.dumps(file, ensure_ascii=False),
                ),
            )

    def files(self, batch):
        with self.lock:
            return [
                json.loads(r["data"])
                for r in self.db.execute("SELECT data FROM files WHERE batch=? ORDER BY path", (batch,))
            ]

    def history(self, source_id, path, hash_value):
        with self.lock:
            return [
                (r["batch"], json.loads(r["data"]))
                for r in self.db.execute(
                    "SELECT batch,data FROM files WHERE source_id=? AND path=? AND source_hash=? AND status='verified' ORDER BY rowid DESC",
                    (source_id, path, hash_value),
                )
            ]

    def has_verified_history(self, source_id, path):
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM files WHERE source_id=? AND path=? AND status='verified' LIMIT 1",
                    (source_id, path),
                ).fetchone()
                is not None
            )

    def event(self, batch, event):
        with self.lock:
            cursor = self.db.execute(
                "INSERT INTO events(batch,data) VALUES(?,?)", (batch, json.dumps(event, ensure_ascii=False))
            )
            return cursor.lastrowid

    def events(self, batch, after=0):
        with self.lock:
            return [
                dict(seq=r["seq"], **json.loads(r["data"]))
                for r in self.db.execute(
                    "SELECT seq,data FROM events WHERE batch=? AND seq>? ORDER BY seq LIMIT 1000",
                    (batch, after),
                )
            ]

    def close(self):
        self.db.close()
