"""Append-only JSON journal for the review pilot."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from time import monotonic
from uuid import uuid4

import httpx

from .protocol import canonical, checksum, crop_png


DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_TAG = "alphadive-review-pilot-v1"


class Conflict(ValueError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class LocalJournal:
    """A filesystem journal used when exercising the service locally."""
    def __init__(self, path):
        self.path = Path(path)

    def initialize(self):
        self.path.mkdir(parents=True, exist_ok=True)

    def read(self):
        return [json.loads(path.read_text(encoding="utf-8")) for path in self.path.glob("*.json")]

    def append(self, event):
        destination = self.path / f"{uuid4().hex}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(canonical(event), encoding="utf-8")
        temporary.replace(destination)

    def store_crop(self, digest, data):
        directory = self.path / "crops"
        directory.mkdir(exist_ok=True)
        destination = directory / f"{digest}.png"
        if not destination.exists():
            temporary = destination.with_suffix(f".{uuid4().hex}.tmp")
            temporary.write_bytes(data)
            temporary.replace(destination)

    def read_crop(self, digest):
        path = self.path / "crops" / f"{digest}.png"
        return path.read_bytes() if path.is_file() else None


class DriveJournal:
    def __init__(self, folder, client_id, client_secret, refresh_token):
        self.folder = folder
        self.credentials = dict(client_id=client_id, client_secret=client_secret,
                                refresh_token=refresh_token, grant_type="refresh_token")
        self.token = ""
        self.expires = 0
        self.recent = {}
        self.crop_ids = {}
        self.client = httpx.Client(timeout=15)

    def request(self, method, url, *, media=False, **kwargs):
        for attempt in range(2):
            if monotonic() >= self.expires:
                response = self.client.post("https://oauth2.googleapis.com/token", data=self.credentials)
                token = response.raise_for_status().json()
                self.token = token["access_token"]
                self.expires = monotonic() + max(0, int(token["expires_in"]) - 60)
            headers = kwargs.get("headers", {}) | {"Authorization": "Bearer " + self.token}
            response = self.client.request(method, url, **(kwargs | {"headers": headers}))
            if response.status_code != 401 or attempt:
                response.raise_for_status()
                return response.content if media else response.json()
            self.expires = 0

    def initialize(self):
        folder = self.request("GET", f"{DRIVE_FILES}/{self.folder}", params={
            "fields": "mimeType,trashed,capabilities(canAddChildren)",
        })
        if (folder.get("mimeType") != "application/vnd.google-apps.folder" or folder.get("trashed")
                or not folder.get("capabilities", {}).get("canAddChildren")):
            raise ValueError("A pasta de julgamentos do Drive não permite gravar arquivos.")

    def read(self):
        visible = []
        params = {
            "q": f"'{self.folder}' in parents and trashed=false and "
                 f"appProperties has {{ key='app' and value='{DRIVE_TAG}' }}",
            "fields": "nextPageToken,files(id,description)",
            "pageSize": 100,
        }
        while True:
            page = self.request("GET", DRIVE_FILES, params=params)
            for record in page.get("files", []):
                visible.append(json.loads(record["description"]))
                self.recent.pop(record["id"], None)
            token = page.get("nextPageToken")
            if not token:
                return visible + list(self.recent.values())
            params["pageToken"] = token

    def append(self, event):
        content = canonical(event)
        metadata = dict(name=f"{event['kind']}-{event['id']}.json", description=content,
                        mimeType="application/json", appProperties={"app": DRIVE_TAG})
        self.recent[self.upload(metadata, content.encode("utf-8"))] = event

    def upload(self, metadata, data):
        boundary = uuid4().hex
        properties = canonical(dict(metadata, parents=[self.folder]))
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{properties}\r\n"
            f"--{boundary}\r\nContent-Type: {metadata['mimeType']}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        record = self.request("POST", DRIVE_UPLOAD, params={"uploadType": "multipart", "fields": "id"},
                              headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body)
        return record["id"]

    def crop_id(self, digest):
        if digest not in self.crop_ids:
            found = self.request("GET", DRIVE_FILES, params={
                "q": f"'{self.folder}' in parents and trashed=false and "
                     f"appProperties has {{ key='review_crop' and value='{digest}' }}",
                "fields": "files(id)", "pageSize": 10,
            })
            ids = [file["id"] for file in found.get("files", [])]
            if ids:
                self.crop_ids[digest] = min(ids)
        return self.crop_ids.get(digest)

    def store_crop(self, digest, data):
        if not self.crop_id(digest):
            self.crop_ids[digest] = self.upload(dict(name=f"recorte-{digest}.png", mimeType="image/png",
                                                     appProperties={"review_crop": digest}), data)

    def read_crop(self, digest):
        key = self.crop_id(digest)
        if key:
            return self.request("GET", f"{DRIVE_FILES}/{key}", params={"alt": "media"}, media=True)
        return None


class ReviewStore:
    def __init__(self, journal, images):
        self.journal = journal
        self.images = images
        self.lock = threading.RLock()

    def state(self, *, resolve_conflicts=True):
        tasks, decisions, acknowledgements = {}, {}, {}
        for event in sorted(self.journal.read(), key=lambda item: (item["at"], item["id"])):
            kind, data = event["kind"], event["data"]
            if kind == "task":
                tasks.setdefault(event["id"], data)
            elif kind == "review":
                decisions.setdefault(event["id"], dict(data, status="SAVED", reason=""))
            elif kind == "ack":
                previous = acknowledgements.get(event["id"])
                if not previous or previous["status"] == "WAITING_EXCEL":
                    acknowledgements[event["id"]] = data
        for operation, result in acknowledgements.items():
            if operation in decisions:
                decisions[operation].update(result)
        if not resolve_conflicts:
            return tasks, decisions
        # Two deployments can briefly overlap. Keep both entries in the journal
        # and withhold competing reviews until the desktop has an unambiguous result.
        claims = {}
        for review in decisions.values():
            for slot in review["entries"]:
                claims.setdefault((review["task_id"], slot), []).append(review)
        for contenders in claims.values():
            if len(contenders) > 1:
                for review in contenders:
                    if review["status"] in {"SAVED", "WAITING_EXCEL"}:
                        review.update(status="CONFLICT", reason="SOURCE_CHANGED")
        return tasks, decisions

    def record(self, kind, key, data):
        self.journal.append(dict(kind=kind, id=key, data=data, at=timestamp()))

    def publish(self, task, crops=None):
        with self.lock:
            tasks, _ = self.state()
            previous = tasks.get(task["id"])
            if previous is None:
                if task.get("scope") == "operational":
                    limit = 5 if task["kind"] == "time" else 3
                    if sum(item.get("scope") == "operational" and item["kind"] == task["kind"]
                           for item in tasks.values()) >= limit:
                        raise Conflict("O primeiro lote operacional já está completo.")
                    for digest, data in crops.items():
                        self.journal.store_crop(digest, data)
                self.record("task", task["id"], task)
            elif previous["version"] != task["version"]:
                raise Conflict("A tarefa tem outra versão publicada.")

    def image(self, digest):
        if digest in self.images:
            return self.images[digest]
        with self.lock:
            tasks, _ = self.state()
            if any(card["crop"] == digest for task in tasks.values() for card in task["cards"]):
                data = self.journal.read_crop(digest)
                if data and crop_png(data) and checksum(data) == digest:
                    return data
        return None

    def task(self, key):
        with self.lock:
            tasks, _ = self.state()
            return tasks.get(key)

    def tasks(self):
        with self.lock:
            published, decisions = self.state()
        tasks = {key: dict(task, generation=0, reviews={}) for key, task in published.items()}
        for record in decisions.values():
            task = tasks[record["task_id"]]
            task["generation"] += 1
            for slot, entry in record["entries"].items():
                if task["reviews"].get(slot, {}).get("status") == "APPLIED":
                    continue
                task["reviews"][slot] = dict(entry, status=record["status"], reason=record["reason"])
        return [tasks[key] for key in sorted(tasks)]

    def save(self, task, generation, operation, entries, reviewer):
        task_id = task["id"]
        data = dict(id=operation, task_id=task_id, version=task["version"],
                    entries=entries, reviewer=reviewer)
        with self.lock:
            tasks, decisions = self.state()
            if tasks.get(task_id, {}).get("version") != task["version"]:
                raise Conflict("A versão da tarefa mudou. Atualize a fila.")
            existing = decisions.get(operation)
            if existing:
                if any(existing[key] != value for key, value in data.items()):
                    raise Conflict("Este envio já foi usado com outros dados.")
                return existing
            related = [review for review in decisions.values() if review["task_id"] == task_id]
            if len(related) != generation:
                raise Conflict("Outro julgamento já chegou para este bloco. Atualize a fila.")
            if any(set(entries) & review["entries"].keys() for review in related):
                raise Conflict("O recorte já foi julgado.")
            data["reviewed_at"] = timestamp()
            self.record("review", operation, data)
        return dict(data, status="SAVED", reason="")

    def pending(self):
        with self.lock:
            _, decisions = self.state()
        return [record for record in decisions.values() if record["status"] in {"SAVED", "WAITING_EXCEL"}]

    def acknowledge(self, operation, status, reason):
        result = dict(status=status, reason=reason)
        with self.lock:
            _, decisions = self.state(resolve_conflicts=False)
            previous = decisions.get(operation)
            if previous and all(previous[key] == value for key, value in result.items()):
                return
            if not previous or previous["status"] not in {"SAVED", "WAITING_EXCEL"}:
                raise Conflict("Revisão inexistente ou já encerrada com outro resultado.")
            self.record("ack", operation, result)
