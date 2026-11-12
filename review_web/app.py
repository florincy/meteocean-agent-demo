"""Render service for the isolated AlphaDive review pilot."""
from base64 import b64decode, urlsafe_b64encode
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hmac import compare_digest
import json
import os
from pathlib import Path
import re
import secrets
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadData, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .protocol import API, checksum, pilot_fixture, validate_entries
from .store import Conflict, DriveJournal, LocalJournal, ReviewStore


STATIC = Path(__file__).with_name("static")
SESSION = "alphadive_review"
LOGIN = "alphadive_login"
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


@dataclass(frozen=True)
class Settings:
    origin: str
    journal_dir: str
    secret: str
    desktop_token: str
    google_id: str
    google_secret: str
    reviewers: dict
    drive_folder: str = ""
    drive_refresh_token: str = ""

    @classmethod
    def from_env(cls):
        return cls(
            (os.environ.get("REVIEW_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/"),
            os.environ.get("REVIEW_LOCAL_JOURNAL", ""),
            os.environ.get("REVIEW_SESSION_SECRET", ""),
            os.environ.get("REVIEW_DESKTOP_TOKEN", ""),
            os.environ.get("REVIEW_GOOGLE_ID", ""),
            os.environ.get("REVIEW_GOOGLE_SECRET", ""),
            json.loads(os.environ.get("REVIEW_GOOGLE_REVIEWERS", "{}")),
            os.environ.get("REVIEW_DRIVE_FOLDER_ID", ""),
            os.environ.get("REVIEW_DRIVE_REFRESH_TOKEN", ""),
        )

    def validate(self):
        origin = urlsplit(self.origin)
        if not origin.hostname or origin.username or origin.path or origin.query or origin.fragment:
            raise ValueError("Configure a origem pública sem caminho, em REVIEW_PUBLIC_URL.")
        if origin.scheme != "https" and not (
            origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1"}
            and not os.getenv("RENDER")
        ):
            raise ValueError("Use HTTPS para o serviço publicado.")
        if min(len(self.secret), len(self.desktop_token)) < 32 or self.secret == self.desktop_token:
            raise ValueError("Configure dois segredos independentes de pelo menos 32 caracteres.")
        if self.drive_folder or self.drive_refresh_token or os.getenv("RENDER"):
            if (not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", self.drive_folder)
                    or not all((self.drive_refresh_token, self.google_id, self.google_secret))):
                raise ValueError("Configure a pasta dedicada e a credencial de gravação do Google Drive.")
        elif not self.journal_dir:
            raise ValueError("Configure uma pasta de arquivos JSON para o teste local.")
        if not isinstance(self.reviewers, dict) or any(
            not isinstance(email, str) or email != email.lower() or
            not isinstance(name, str) or not 0 < len(name.strip()) <= 120
            for email, name in self.reviewers.items()
        ):
            raise ValueError("Configure os e-mails autorizados e nomes dos revisores em JSON.")
        if os.getenv("RENDER") and not all((self.google_id, self.google_secret, self.reviewers)):
            raise ValueError("Configure o login Google e pelo menos um revisor autorizado.")


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Publish(Payload):
    version: Digest
    crops: dict[Digest, Annotated[str, StringConstraints(max_length=32768)]] = Field(max_length=2)


class Entry(Payload):
    state: Literal["READABLE", "EMPTY", "UNREADABLE"]
    transcription: str = Field(max_length=80)
    operational_value: str = Field(max_length=24)


class Review(Payload):
    id: UUID
    version: Digest
    generation: int = Field(ge=0, le=2)
    entries: dict[str, Entry] = Field(min_length=1, max_length=2)


class Acknowledge(Payload):
    status: Literal["APPLIED", "WAITING_EXCEL", "CONFLICT"]
    reason: Literal["", "EXCEL_LOCKED", "SOURCE_CHANGED", "UNREADABLE", "LOCAL_VALIDATION"]


async def google_claims(config, code, verifier):
    from google.auth.exceptions import GoogleAuthError
    import google.auth.transport.requests
    from google.oauth2 import id_token
    from starlette.concurrency import run_in_threadpool

    async with httpx.AsyncClient(timeout=15) as client:
        result = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": config.google_id, "client_secret": config.google_secret,
            "redirect_uri": config.origin + "/auth/google/callback", "grant_type": "authorization_code",
            "code_verifier": verifier,
        })
        result.raise_for_status()
    try:
        return await run_in_threadpool(
            id_token.verify_oauth2_token, result.json()["id_token"],
            google.auth.transport.requests.Request(), config.google_id,
        )
    except GoogleAuthError as error:
        raise ValueError("Falha na verificação do ID token.") from error


def create_app(settings=None):
    tasks, images = pilot_fixture()

    @asynccontextmanager
    async def lifespan(service):
        config = settings or Settings.from_env()
        config.validate()
        service.state.settings = config
        journal = (DriveJournal(config.drive_folder, config.google_id, config.google_secret, config.drive_refresh_token)
                   if config.drive_folder else LocalJournal(config.journal_dir))
        service.state.store = ReviewStore(journal, images)
        try:
            journal.initialize()
            yield
        finally:
            if isinstance(journal, DriveJournal):
                journal.client.close()

    service = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def signer(request, purpose):
        return URLSafeTimedSerializer(request.app.state.settings.secret, salt=purpose)

    def set_cookie(request, response, name, data, age):
        response.set_cookie(name, signer(request, name).dumps(data), max_age=age, httponly=True,
                            secure=request.app.state.settings.origin.startswith("https:"), samesite="lax")

    def read_cookie(request, name, age):
        try:
            return signer(request, name).loads(request.cookies.get(name, ""), max_age=age)
        except BadData:
            return {}

    def reviewer(request):
        identity = read_cookie(request, SESSION, 28800)
        allowed = request.app.state.settings.reviewers
        name = allowed.get(identity.get("sub")) or allowed.get(identity.get("email"))
        if not name or not identity.get("sub"):
            raise HTTPException(401, "Entre com uma conta autorizada.")
        return dict(id=identity["sub"], name=name)

    def desktop(request):
        authorization = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + request.app.state.settings.desktop_token).encode()
        if not compare_digest(authorization, expected):
            raise HTTPException(401, "Credencial do desktop inválida.")
        return request.app.state.store

    def same_origin(request):
        if request.headers.get("origin") != request.app.state.settings.origin:
            raise HTTPException(403, "Envio fora da origem autorizada.")

    @service.middleware("http")
    async def response_policy(request, next_handler):
        if request.method in {"POST", "PUT"} and request.url.path.startswith(API):
            try:
                size = int(request.headers.get("content-length", "-1"))
            except ValueError:
                size = -1
            if not 0 <= size <= 80_000 or request.headers.get("transfer-encoding"):
                return JSONResponse({"detail": "Envio excede o limite do piloto."}, status_code=413)
        response = await next_handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @service.exception_handler(Conflict)
    async def conflict_response(_, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @service.exception_handler(httpx.HTTPError)
    async def drive_error(_, error):
        return JSONResponse({"detail": "O Google Drive está indisponível. Tente novamente."}, status_code=503)

    @service.get("/healthz")
    def health():
        return {"status": "ok"}

    @service.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @service.get("/review.css")
    def stylesheet():
        return FileResponse(STATIC / "review.css", media_type="text/css")

    @service.get("/review.js")
    def script():
        return FileResponse(STATIC / "review.js", media_type="application/javascript")

    @service.get("/auth/google")
    def login(request: Request):
        config = request.app.state.settings
        if not config.google_id or not config.google_secret or not config.reviewers:
            raise HTTPException(503, "O login Google ainda precisa ser configurado no piloto.")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = urlsafe_b64encode(bytes.fromhex(checksum(verifier.encode()))).rstrip(b"=").decode()
        query = urlencode(dict(
            client_id=config.google_id, redirect_uri=config.origin + "/auth/google/callback",
            response_type="code", scope="openid email", state=state, nonce=nonce,
            code_challenge=challenge, code_challenge_method="S256", prompt="select_account",
        ))
        response = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + query)
        set_cookie(request, response, LOGIN, dict(state=state, nonce=nonce, verifier=verifier), 600)
        return response

    @service.get("/auth/google/callback")
    async def callback(request: Request, code: str = "", state: str = ""):
        config = request.app.state.settings
        pending = read_cookie(request, LOGIN, 600)
        if not code or not state or state != pending.get("state"):
            raise HTTPException(400, "Sessão de login expirada. Volte à fila e tente novamente.")
        try:
            claims = await google_claims(config, code, pending["verifier"])
        except (httpx.HTTPError, ValueError, KeyError, OSError):
            raise HTTPException(401, "Não foi possível validar a conta Google.") from None
        email = str(claims.get("email", "")).lower()
        subject = claims.get("sub")
        email_allowed = email in config.reviewers and (email.endswith("@gmail.com") or claims.get("hd"))
        if (not (subject in config.reviewers or email_allowed) or claims.get("email_verified") is not True
                or claims.get("nonce") != pending["nonce"] or not subject):
            response = JSONResponse({"detail": "Conta Google fora da lista autorizada."}, status_code=403)
        else:
            response = RedirectResponse("/", status_code=303)
            set_cookie(request, response, SESSION, dict(sub=subject, email=email if email_allowed else ""), 28800)
        response.delete_cookie(LOGIN)
        return response

    @service.get(API + "/session")
    def session(request: Request):
        return {"reviewer": reviewer(request)["name"]}

    @service.post(API + "/logout")
    def logout(request: Request):
        same_origin(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION)
        return response

    @service.get(API + "/tasks")
    def list_tasks(request: Request):
        reviewer(request)
        return {"tasks": request.app.state.store.tasks()}

    @service.get(API + "/crops/{digest}")
    def crop(request: Request, digest: Digest):
        reviewer(request)
        data = request.app.state.store.image(digest)
        if data is None:
            raise HTTPException(404, "Recorte indisponível.")
        return Response(data, media_type="image/png")

    @service.post(API + "/tasks/{task_id}/reviews")
    def save(request: Request, task_id: str, payload: Review):
        who = reviewer(request)
        same_origin(request)
        task = tasks.get(task_id)
        if task is None or task["version"] != payload.version:
            raise HTTPException(409, "Tarefa ou versão indisponível.")
        try:
            entries = validate_entries(task, {slot: entry.model_dump() for slot, entry in payload.entries.items()})
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        saved = request.app.state.store.save(task, payload.generation, str(payload.id), entries, who)
        return {"status": saved["status"]}

    @service.put(API + "/sync/tasks/{task_id}")
    def publish(request: Request, task_id: str, payload: Publish):
        store = desktop(request)
        task = tasks.get(task_id)
        expected = {card["crop"] for card in task["cards"]} if task else set()
        if not task or task["version"] != payload.version or set(payload.crops) != expected:
            raise HTTPException(422, "Este piloto aceita apenas tarefas fictícias fornecidas com a aplicação.")
        try:
            uploaded = {digest: b64decode(content, validate=True) for digest, content in payload.crops.items()}
        except ValueError:
            raise HTTPException(422, "Recorte de teste inválido.") from None
        if any(data != images[digest] for digest, data in uploaded.items()):
            raise HTTPException(422, "Recorte fora do conjunto fictício.")
        store.publish(task)
        return {"status": "PUBLISHED"}

    @service.get(API + "/sync/reviews")
    def pending(request: Request):
        return {"reviews": desktop(request).pending()}

    @service.put(API + "/sync/reviews/{operation}")
    def acknowledge(request: Request, operation: UUID, payload: Acknowledge):
        store = desktop(request)
        reasons = {
            "APPLIED": {""},
            "WAITING_EXCEL": {"EXCEL_LOCKED"},
            "CONFLICT": {"SOURCE_CHANGED", "UNREADABLE", "LOCAL_VALIDATION"},
        }
        if payload.reason not in reasons[payload.status]:
            raise HTTPException(422, "O motivo não corresponde ao resultado.")
        store.acknowledge(str(operation), payload.status, payload.reason)
        return {"status": payload.status}

    return service


app = create_app()
