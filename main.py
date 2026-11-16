import os
import re
import imaplib
import time
import secrets
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
API_SECRET = os.environ["API_SECRET"]
DEFAULT_TO = os.environ.get("DEFAULT_TO", "")

app = FastAPI(title="API Borradores Reglamento UP")


class Borrador(BaseModel):
    asunto: str
    cuerpo: str
    para: str | None = None


class BorradorTexto(BaseModel):
    texto: str


def verificar_clave(x_api_key: str) -> None:
    if not secrets.compare_digest(x_api_key or "", API_SECRET):
        raise HTTPException(401, "clave inválida")


def _decodificar(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, (bytes, bytearray)):
        return valor.decode("utf-8", "replace")
    return str(valor)


def _tiene_emoji(texto: str) -> bool:
    for ch in texto:
        o = ord(ch)
        if (
            0x1F300 <= o <= 0x1FAFF
            or 0x2600 <= o <= 0x26FF
            or 0x2700 <= o <= 0x27BF
            or 0xFE00 <= o <= 0xFE0F
            or 0x1F1E6 <= o <= 0x1F1FF
            or o == 0x200D
        ):
            return True
    return False


def _es_linea_intro(linea: str) -> bool:
    s = linea.strip()
    if not s:
        return False
    bajo = s.lower()
    frases = (
        "aquí tienes",
        "aqui tienes",
        "te dejo",
        "borrador:",
        "el correo formal",
        "redacté",
        "redacte",
        "a continuación",
        "a continuacion",
        "este es el borrador",
    )
    if any(f in bajo for f in frases):
        return True
    if _tiene_emoji(s) and len(s) < 200:
        return True
    return False


def parsear_texto(texto: str) -> tuple[str, str]:
    raw = (texto or "").replace("\r\n", "\n").replace("\r", "\n")
    lineas = raw.split("\n")

    asunto = None
    asunto_idx = None
    estimados_idx = None
    fragmentos_idx = None

    for i, linea in enumerate(lineas):
        stripped = linea.strip()
        bajo = stripped.lower()
        if asunto is None and bajo.startswith("asunto"):
            resto = stripped[6:].lstrip()
            if resto.startswith(":"):
                asunto = resto[1:].strip()
                asunto_idx = i
        if estimados_idx is None and bajo.startswith("estimados"):
            estimados_idx = i
        if fragmentos_idx is None and bajo.startswith("fragmentos"):
            fragmentos_idx = i

    fin = fragmentos_idx if fragmentos_idx is not None else len(lineas)

    if estimados_idx is not None and estimados_idx < fin:
        inicio = estimados_idx
    elif asunto_idx is not None:
        inicio = asunto_idx + 1
    else:
        inicio = 0
        while inicio < fin and not lineas[inicio].strip():
            inicio += 1
        if inicio < fin and _es_linea_intro(lineas[inicio]):
            inicio += 1
            while inicio < fin and not lineas[inicio].strip():
                inicio += 1

    cuerpo = "\n".join(lineas[inicio:fin]).strip()
    if not asunto:
        asunto = "Consulta académica"
    if not cuerpo:
        raise HTTPException(400, "no se pudo extraer el cuerpo del borrador")
    return asunto, cuerpo


def encontrar_carpeta_borradores(im: imaplib.IMAP4_SSL) -> str:
    typ, data = im.list()
    if typ != "OK" or not data:
        raise RuntimeError("No se pudo listar las carpetas IMAP")
    fallbacks: list[str] = []
    for raw in data:
        line = _decodificar(raw)
        flags_match = re.match(r"^\(([^)]*)\)\s+(\S+)\s+(.+)$", line)
        if not flags_match:
            continue
        flags = flags_match.group(1)
        mailbox = flags_match.group(3).strip()
        if mailbox.startswith('"') and mailbox.endswith('"'):
            mailbox = mailbox[1:-1]
        flags_upper = {f.upper() for f in flags.split() if f}
        if "\\DRAFTS" in flags_upper:
            return mailbox
        low = mailbox.lower().rstrip("/")
        if low in {
            "drafts",
            "borradores",
            "[gmail]/drafts",
            "[gmail]/borradores",
        } or low.endswith("/drafts") or low.endswith("/borradores"):
            fallbacks.append(mailbox)
    if fallbacks:
        return fallbacks[0]
    raise RuntimeError("No se encontró la carpeta de Borradores")


def guardar_borrador(asunto: str, cuerpo: str, para: str) -> None:
    msg = EmailMessage()
    msg["From"] = GMAIL_USER
    msg["To"] = para
    msg["Subject"] = asunto.strip()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(cuerpo.strip())
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as im:
            im.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            folder = encontrar_carpeta_borradores(im)
            mailbox = folder if (folder.startswith('"') and folder.endswith('"')) else f'"{folder}"'
            r, data = im.append(
                mailbox,
                r"\Draft",
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes(),
            )
            if r != "OK":
                raise RuntimeError(f"IMAP append falló: {r} {data}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"No se pudo crear el borrador: {e}")


@app.get("/")
def health():
    return {"ok": True}


@app.post("/crear-borrador")
def crear_borrador(b: Borrador, x_api_key: str = Header(default="")):
    verificar_clave(x_api_key)
    to = (b.para or DEFAULT_TO).strip()
    if not to:
        raise HTTPException(400, "falta destinatario")
    guardar_borrador(b.asunto, b.cuerpo, to)
    return {
        "ok": True,
        "para": to,
        "asunto": b.asunto.strip(),
        "mensaje": "Borrador guardado en Gmail",
    }


@app.post("/crear-borrador-texto")
def crear_borrador_texto(b: BorradorTexto, x_api_key: str = Header(default="")):
    verificar_clave(x_api_key)
    to = (DEFAULT_TO or "").strip()
    if not to:
        raise HTTPException(400, "falta destinatario")
    asunto, cuerpo = parsear_texto(b.texto)
    guardar_borrador(asunto, cuerpo, to)
    return {
        "ok": True,
        "para": to,
        "asunto": asunto,
        "mensaje": "Borrador guardado en Gmail",
    }
