"""wa_bridge.py — ciclo de vida del bridge de WhatsApp (el proceso Go aparte).

El bridge habla el protocolo de WhatsApp (whatsmeow); el motor y la UI le pegan
por HTTP en loopback. Hasta acá era un paso manual ("abrí otra terminal y corré
el binario"), que es exactamente el muro que Botata no le quiere poner a nadie:
este módulo hace que nadie tenga que lanzarlo a mano.

- `ensure_bridge()` — si el bridge no responde, lo levanta y espera. Lo usa el
  motor al arrancar con CHANNEL=whatsapp y la UI con el botón del panel.
- Si el binario no existe, se compila con `go build` (una sola vez; hace falta
  Go instalado — el binario no viaja en el repo porque es por plataforma).
- El proceso queda DESACOPLADO (sobrevive al bot y a la UI): la sesión de
  WhatsApp vive en él, y reiniciar el bot no tiene por qué desvincular nada.
  El pid queda en `<instancia>/whatsapp/bridge.pid` para poder reiniciarlo.

Todo es loopback y archivos locales: acá no hay red externa.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("botata.wa_bridge")

BRIDGE_SRC = Path(__file__).resolve().parent.parent / "bridges" / "whatsapp"


def bridge_status(url: str, timeout: float = 5) -> dict | None:
    """GET /status, o None si el bridge no responde (no distingue por qué:
    para decidir si hay que levantarlo da igual caído que inexistente)."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def binary_path() -> Path:
    return BRIDGE_SRC / ("whatsapp-bridge.exe" if os.name == "nt" else "whatsapp-bridge")


def build_binary() -> str | None:
    """Compila el bridge si falta el binario. Devuelve un error legible o None.

    `go build` acá es determinista y sin red (las deps están en go.sum y la
    cache de módulos); tarda segundos. Si no hay Go, el mensaje dice exactamente
    qué instalar en vez de un FileNotFoundError críptico.
    """
    if binary_path().exists():
        return None
    if shutil.which("go") is None:
        return ("el binario del bridge no está y no encuentro Go para compilarlo. "
                "Instalá Go (go.dev/dl) y reintentá, o compilá a mano: "
                f"cd {BRIDGE_SRC} && go build -o {binary_path().name} .")
    log.info("compilando el bridge de WhatsApp (primera vez)…")
    try:
        r = subprocess.run(["go", "build", "-o", binary_path().name, "."],
                           cwd=str(BRIDGE_SRC), capture_output=True, text=True,
                           timeout=300)
    except subprocess.TimeoutExpired:
        return "go build tardó más de 5 minutos — algo está mal con el toolchain de Go"
    if r.returncode != 0:
        return f"go build falló: {(r.stderr or r.stdout or '').strip()[-500:]}"
    return None


def _pid_file(data_dir: Path) -> Path:
    return data_dir / "bridge.pid"


def _pid_vivo(pid: int) -> bool:
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True)
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_bridge(data_dir: Path) -> None:
    """Mata el bridge que ESTE módulo lanzó (por el pid file). Un bridge que
    levantó otra cosa no se toca: no es nuestro para matarlo."""
    pf = _pid_file(data_dir)
    if not pf.exists():
        return
    try:
        pid = int(pf.read_text().strip())
    except ValueError:
        pf.unlink(missing_ok=True)
        return
    if _pid_vivo(pid):
        log.info("deteniendo el bridge (pid %d)", pid)
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    pf.unlink(missing_ok=True)


def start_bridge(url: str, data_dir: Path, chats: list[str]) -> str | None:
    """Lanza el bridge desacoplado. Devuelve un error legible o None.

    Sin `chats` arranca igual pero avisando: es el estado legítimo de la
    primera vinculación (todavía no se conocen los JIDs — se eligen en la UI
    con el bridge ya arriba), no un default aceptable para operar.
    """
    err = build_binary()
    if err:
        return err
    addr = urllib.parse.urlparse(url).netloc or "127.0.0.1:8899"
    # Absoluto SIEMPRE: el proceso corre con cwd=BRIDGE_SRC, y un data_dir
    # relativo se resolvería contra esa carpeta — el bridge arrancaría con una
    # sesión nueva vacía y pediría QR teniendo la sesión buena en otro lado.
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    args = [str(binary_path()), "-addr", addr, "-data", str(data_dir)]
    if chats:
        args += ["-chats", ",".join(chats)]
    else:
        log.warning("bridge sin -chats: modo vinculación — elegí los chats en la "
                    "UI y reiniciá el bridge para activar el allowlist")
    # La salida va a un log propio: el bridge vive más que este proceso y su
    # terminal; sin archivo, sus errores no los ve nadie.
    log_f = open(data_dir / "bridge.log", "ab")
    kw: dict = {"stdout": log_f, "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL, "cwd": str(BRIDGE_SRC)}
    if os.name == "nt":
        kw["creationflags"] = (subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kw)
    except OSError as e:
        return f"no pude lanzar el bridge: {e}"
    finally:
        log_f.close()
    _pid_file(data_dir).write_text(str(proc.pid))
    log.info("bridge lanzado (pid %d) en %s", proc.pid, addr)
    return None


def ensure_bridge(url: str, data_dir: Path, chats: list[str],
                  wait: float = 20) -> tuple[dict | None, str | None]:
    """El punto de entrada: bridge respondiendo, o un error que diga por qué no.

    Devuelve (status, None) con el bridge arriba — vinculado o no, eso lo
    decide quien llama — o (None, error legible). Si ya respondía, no toca
    nada: puede ser uno lanzado a mano o compartido, y funciona igual.
    """
    st = bridge_status(url)
    if st is not None:
        return st, None
    err = start_bridge(url, data_dir, chats)
    if err:
        return None, err
    limite = time.monotonic() + wait
    while time.monotonic() < limite:
        time.sleep(0.5)
        st = bridge_status(url, timeout=2)
        if st is not None:
            return st, None
    return None, (f"lancé el bridge pero no respondió en {wait:.0f}s — mirá "
                  f"{data_dir / 'bridge.log'} para ver qué le pasó")
