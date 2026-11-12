"""Persistent queue and acknowledgements. SQLite is used for local smoke tests."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3

from .protocol import canonical


class Conflict(ValueError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class ReviewStore:
    def __init__(self, database):
        self.database = str(database)
        self.postgres = self.database.startswith(("postgres://", "postgresql://"))

    @contextmanager
    def transaction(self, *, write=False):
        if self.postgres:
            import psycopg
            connection = psycopg.connect(self.database, connect_timeout=8)
        else:
            connection = sqlite3.connect(self.database, timeout=8)
            connection.execute("PRAGMA foreign_keys=ON")
        try:
            if write and not self.postgres:
                connection.execute("BEGIN IMMEDIATE")
            yield lambda sql, args=(): connection.execute(sql.replace("?", "%s") if self.postgres else sql, args)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.transaction(write=True) as db:
            db("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, version TEXT NOT NULL, data TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0)""")
            db("""CREATE TABLE IF NOT EXISTS crops (
                digest TEXT PRIMARY KEY, image BYTEA NOT NULL)""")
            db("""CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                generation INTEGER NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '', UNIQUE(task_id, generation))""")

    def ping(self):
        with self.transaction() as db:
            db("SELECT 1").fetchone()

    def publish(self, task, images):
        with self.transaction(write=True) as db:
            db("INSERT INTO tasks (id,version,data) VALUES (?,?,?) ON CONFLICT (id) DO NOTHING",
               (task["id"], task["version"], canonical(task)))
            version, = db("SELECT version FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if version != task["version"]:
                raise Conflict("A tarefa tem outra versão publicada.")
            for digest, content in images.items():
                db("INSERT INTO crops VALUES (?,?) ON CONFLICT (digest) DO NOTHING", (digest, content))

    def image(self, digest):
        with self.transaction() as db:
            row = db("SELECT image FROM crops WHERE digest=?", (digest,)).fetchone()
            return bytes(row[0]) if row else None

    def tasks(self):
        with self.transaction() as db:
            rows = db("""SELECT t.data,t.generation,d.data,d.status,d.reason FROM tasks t
                         LEFT JOIN decisions d ON d.task_id=t.id
                         ORDER BY t.id,d.generation""").fetchall()
        tasks = {}
        for data, generation, decision, status, reason in rows:
            task = json.loads(data)
            target = tasks.setdefault(task["id"], dict(task, generation=generation, reviews={}))
            if decision:
                record = json.loads(decision)
                target["reviews"].update({
                    slot: dict(entry, status=status, reason=reason)
                    for slot, entry in record["entries"].items()
                })
        return list(tasks.values())

    def save(self, task, generation, operation, entries, reviewer):
        task_id = task["id"]
        data = dict(id=operation, task_id=task_id, version=task["version"],
                    entries=entries, reviewer=reviewer)
        with self.transaction(write=True) as db:
            # Serialize updates on this task on Postgres as well as SQLite.
            locked = db("UPDATE tasks SET generation=generation WHERE id=? AND version=?",
                        (task_id, task["version"])).rowcount
            if not locked:
                raise Conflict("A versão da tarefa mudou. Atualize a fila.")
            saved = db("SELECT data,status,reason FROM decisions WHERE id=?", (operation,)).fetchone()
            if saved:
                existing = json.loads(saved[0])
                if any(existing[key] != value for key, value in data.items()):
                    raise Conflict("Este envio já foi usado com outros dados.")
                return dict(existing, status=saved[1], reason=saved[2])
            current, = db("SELECT generation FROM tasks WHERE id=?", (task_id,)).fetchone()
            if current != generation:
                raise Conflict("Outro julgamento já chegou para este bloco. Atualize a fila.")
            rows = db("SELECT data FROM decisions WHERE task_id=?", (task_id,)).fetchall()
            if any(set(entries) & json.loads(row[0])["entries"].keys() for row in rows):
                raise Conflict("O recorte já foi julgado.")
            data["reviewed_at"] = timestamp()
            inserted = db("""INSERT INTO decisions (id,task_id,generation,data,status) VALUES (?,?,?,?,?)
                             ON CONFLICT (id) DO NOTHING""",
                          (operation, task_id, generation + 1, canonical(data), "SAVED")).rowcount
            if not inserted:
                raise Conflict("Este envio já foi usado com outra tarefa.")
            db("UPDATE tasks SET generation=? WHERE id=?", (generation + 1, task_id))
        return dict(data, status="SAVED", reason="")

    def pending(self):
        with self.transaction() as db:
            rows = db("""SELECT data,status,reason FROM decisions WHERE status IN ('SAVED','WAITING_EXCEL')
                         ORDER BY task_id,generation""").fetchall()
        return [dict(json.loads(data), status=status, reason=reason) for data, status, reason in rows]

    def acknowledge(self, operation, status, reason):
        with self.transaction(write=True) as db:
            count = db("""UPDATE decisions SET status=?,reason=? WHERE id=?
                          AND (status IN ('SAVED','WAITING_EXCEL') OR (status=? AND reason=?))""",
                       (status, reason, operation, status, reason)).rowcount
            if not count:
                raise Conflict("Revisão inexistente ou já encerrada com outro resultado.")
