#!/usr/bin/env python3
"""
OSCSee-LV1 - puente OSC See (OSC por UDP) -> Waves eMotion LV1 (OSC sobre TCP, protocolo MyFOH).

Doble clic y listo: busca el LV1 en la red, se conecta, y escucha OSC por UDP.
Desde OSC See (o cualquier app OSC) mandas:

    /lv1/scene <n>          recalla la escena n (1 = la primera de la lista)
    /lv1/scene/index <n>    recalla por índice 0-based (0 = la primera)
    /lv1/scene/name <s>     recalla por nombre exacto de escena
    /lv1/scene/next         escena siguiente
    /lv1/scene/prev         escena anterior
    /lv1/raw/<address> ...  reenvía /<address> con los mismos argumentos al LV1
                            (ej: /lv1/raw/Set/UserKey i:0 T)

Protocolo del LV1 tomado del módulo comunitario de Bitfocus Companion
(companion-module-waves-lv1, MIT, por Miguel Lourenço), que lo obtuvo por
ingeniería inversa. No es oficial de Waves: una actualización del LV1 podría
cambiarlo.

Solo usa la librería estándar de Python (tkinter incluido), así PyInstaller
lo empaqueta sin dependencias.
"""

import json
import os
import queue
import random
import socket
import struct
import sys
import threading
import time
try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # sin interfaz gráfica: el núcleo sigue siendo importable
    tk = ttk = None

APP_NAME = "OSCSee-LV1"
VERSION = "1.0.0"
DEVICE_NAME = "OSCSee-LV1"

ZDNS_GROUP = "225.1.1.1"
ZDNS_PORT = 13337
LV1_SERVICE = "_waveslv113._tcp"
TX_HEADER = bytes([0, 0, 0, 2, 0, 0, 0, 0])
HEADER_LEN = 8

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".oscsee-lv1.json")
LOG_PATH = os.path.join(os.path.expanduser("~"), "oscsee-lv1.log")
DEFAULT_CONFIG = {"listen_port": 9000, "lv1_ip": "", "lv1_port": 0, "uuid": ""}


# ─────────────────────────── OSC encode / decode ───────────────────────────

def _pad4(b: bytes) -> bytes:
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


def _osc_str(s: str) -> bytes:
    return _pad4(s.encode("utf-8") + b"\x00")


def osc_encode(address: str, args):
    """args: lista de (tipo, valor). Tipos: i f s h d T F N b."""
    tags = ","
    data = b""
    for t, v in args:
        tags += t
        if t == "i":
            data += struct.pack(">i", int(v))
        elif t == "f":
            data += struct.pack(">f", float(v))
        elif t == "s":
            data += _osc_str(str(v))
        elif t == "h":
            data += struct.pack(">q", int(v))
        elif t == "d":
            data += struct.pack(">d", float(v))
        elif t == "b":
            data += struct.pack(">i", len(v)) + _pad4(bytes(v))
        elif t in "TFN":
            pass
        else:
            raise ValueError(f"tipo OSC no soportado: {t}")
    return _osc_str(address) + _osc_str(tags) + data


def _read_str(buf: bytes, pos: int):
    end = buf.index(b"\x00", pos)
    s = buf[pos:end].decode("utf-8", errors="replace")
    pos = end + 1
    pos += (4 - pos % 4) % 4
    return s, pos


def osc_decode(buf: bytes):
    """Devuelve (address, [(tipo, valor), ...]). Ignora bundles (el LV1 no los usa)."""
    address, pos = _read_str(buf, 0)
    if pos >= len(buf):
        return address, []
    tags, pos = _read_str(buf, pos)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(("i", struct.unpack_from(">i", buf, pos)[0])); pos += 4
        elif t == "f":
            args.append(("f", struct.unpack_from(">f", buf, pos)[0])); pos += 4
        elif t == "s":
            s, pos = _read_str(buf, pos); args.append(("s", s))
        elif t == "h":
            args.append(("h", struct.unpack_from(">q", buf, pos)[0])); pos += 8
        elif t == "d":
            args.append(("d", struct.unpack_from(">d", buf, pos)[0])); pos += 8
        elif t == "b":
            n = struct.unpack_from(">i", buf, pos)[0]; pos += 4
            args.append(("b", buf[pos:pos + n])); pos += n + (4 - n % 4) % 4
        elif t in "TFN":
            args.append((t, {"T": True, "F": False, "N": None}[t]))
        else:
            raise ValueError(f"tipo OSC desconocido: {t}")
    return address, args


def lv1_frame(address, args) -> bytes:
    pkt = osc_encode(address, args)
    return struct.pack(">I", len(pkt)) + TX_HEADER + pkt


# ─────────────────────────── Descubrimiento zDNS ───────────────────────────

def _rank_ip(ip: str) -> int:
    if ip.startswith("127."):
        return -100
    if ip.startswith("169.254."):
        return -50
    p = ip.split(".")
    if p[0] == "172" and 16 <= int(p[1]) <= 31:
        return 30
    if ip.startswith("192.168.56."):
        return 20
    if ip.startswith("192.168."):
        return 100
    if ip.startswith("10."):
        return 90
    return 40


def _is_ipv4(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in parts)


def local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # truco: la IP de salida por defecto
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def discover_lv1(timeout=6.0, want_ip=None, log=lambda *a: None):
    """Escucha el anuncio /zDNS del LV1. Devuelve (ip, port) o None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        sock.bind(("", ZDNS_PORT))
    except OSError as e:
        log(f"No pude abrir el puerto de descubrimiento {ZDNS_PORT}: {e}")
        sock.close()
        return None

    joined = 0
    for ifip in local_ipv4s() + ["0.0.0.0"]:
        try:
            mreq = socket.inet_aton(ZDNS_GROUP) + socket.inet_aton(ifip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            joined += 1
        except OSError:
            pass
    if not joined:
        log("No pude unirme al grupo multicast en ninguna interfaz.")

    sock.settimeout(0.5)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                data, src = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                addr, args = osc_decode(data)
            except Exception:
                continue
            if addr != "/zDNS" or len(args) < 2 or args[0][0] != "s":
                continue
            if args[0][1] != LV1_SERVICE:
                continue
            port, v4 = None, []
            for t, v in args[2:]:
                if t == "s" and _is_ipv4(v):
                    v4.append(v)
                elif t == "i" and port is None and 1024 < v < 65536:
                    port = v
            if port is None:
                continue
            if want_ip:
                if want_ip in v4 or src[0] == want_ip:
                    return want_ip, port
                continue
            # la IP desde la que llegó el anuncio es la que de verdad rutea
            # La IP de origen del anuncio es la que de verdad llega a esta máquina,
            # aunque sea link-local (169.254.x.x, típica de la red SoundGrid).
            log(f"Anuncio del LV1 desde {src[0]} (IPs que anuncia: {', '.join(v4) or '—'})")
            return src[0], port
    finally:
        sock.close()
    return None


# ─────────────────────────── Cliente TCP del LV1 ───────────────────────────

class LV1Client:
    def __init__(self, cfg, events: queue.Queue):
        self.cfg = cfg
        self.events = events
        self.sock = None
        self.lock = threading.Lock()
        self.registered = False
        self.scenes = {}          # índice 0-based -> nombre
        self.current_scene = None
        self.stop_flag = False
        self.target = None
        self.wake = threading.Event()

    # ── utilidades ──
    def log(self, msg):
        self.events.put(("log", msg))

    def status(self, state, detail=""):
        self.events.put(("status", state, detail))

    def send(self, address, args=()):
        with self.lock:
            if not self.sock or not self.registered:
                raise ConnectionError("LV1 no conectado")
            self.sock.sendall(lv1_frame(address, list(args)))

    # ── acciones de escena ──
    def recall_index(self, idx: int):
        if self.scenes and not (0 <= idx < len(self.scenes)):
            self.log(f"Escena {idx + 1} fuera de rango (hay {len(self.scenes)})")
            return
        self.send("/Set/CurSceneIndex", [("i", idx)])
        # el LV1 no le devuelve /Notify al cliente que originó el cambio
        self._set_current(idx)
        self.log(f"→ Escena {idx + 1}: {self.scenes.get(idx, '?')}")

    @staticmethod
    def _norm(s: str) -> str:
        """Ignora mayúsculas, tildes, puntuación y espacios de más.
        'Canción A, Puente' == 'cancion a puente'."""
        import unicodedata
        s = unicodedata.normalize("NFKD", s)
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        s = "".join(ch if ch.isalnum() else " " for ch in s.lower())
        return " ".join(s.split())

    def recall_name(self, name: str):
        # 1) exacto
        for i in sorted(self.scenes):
            if self.scenes[i] == name:
                return self.recall_index(i)
        # 2) normalizado — nunca "parecido": en vivo es mejor no disparar que disparar la equivocada
        key = self._norm(name)
        hits = [i for i in sorted(self.scenes) if self._norm(self.scenes[i]) == key]
        if len(hits) > 1:
            self.log(f"⚠ '{name}' coincide con varias escenas "
                     f"({', '.join(str(i + 1) for i in hits)}); uso la {hits[0] + 1}")
        if hits:
            return self.recall_index(hits[0])
        self.log(f"No encontré la escena '{name}'")

    def step(self, delta: int):
        total = len(self.scenes)
        if not total:
            self.log("Todavía no tengo la lista de escenas")
            return
        cur = self.current_scene if self.current_scene is not None else (-1 if delta > 0 else 0)
        self.recall_index((cur + delta) % total)

    def _set_current(self, idx):
        self.current_scene = idx
        self.events.put(("scene", idx, self.scenes.get(idx, "")))

    def reconnect(self):
        """Corta la conexión actual y vuelve a conectar con la config nueva, ya."""
        self.wake.set()
        with self.lock:
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    # ── bucle principal ──
    def run(self):
        while not self.stop_flag:
            try:
                self._session()
            except Exception as e:
                self.log(f"Conexión caída: {e}")
            with self.lock:
                self.registered = False
                if self.sock:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
            if self.stop_flag:
                break
            if not self.wake.is_set():
                self.status("off", "Reintentando…")
            self.wake.wait(3)
            self.wake.clear()

    def _session(self):
        ip = self.cfg.get("lv1_ip", "").strip()
        port = int(self.cfg.get("lv1_port") or 0) if ip else 0
        if not port:
            self.status("search", "Buscando LV1 en la red…")
            found = discover_lv1(timeout=6.0, want_ip=ip or None, log=self.log)
            if not found:
                if ip:
                    self.log(f"No llega el anuncio del LV1 {ip}. Revisa red / permiso de red local, "
                             "o fija también el puerto en Ajustes.")
                else:
                    self.log("No apareció ningún LV1 (¿misma red? ¿permiso de red local? "
                             "¿Waves Remote activo?)")
                return
            ip, port = found
            self.log(f"LV1 encontrado en {ip}:{port}")
        self.target = (ip, port)

        self.status("search", f"Conectando a {ip}:{port}…")
        s = socket.create_connection((ip, port), timeout=5)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(1.0)
        with self.lock:
            self.sock = s

        # Handshake estilo MyFOH: /handshake + /device_name en UNA escritura TCP
        uuid = self.cfg["uuid"]
        s.sendall(
            lv1_frame("/handshake", [("i", 1), ("i", -1), ("i", 1)])
            + lv1_frame("/device_name", [("s", DEVICE_NAME), ("s", uuid)])
        )

        rx = b""
        hs_deadline = time.time() + 3
        last_rx = time.time()
        while not self.stop_flag:
            try:
                chunk = s.recv(65536)
                if not chunk:
                    raise ConnectionError("el LV1 cerró la conexión")
                rx += chunk
                last_rx = time.time()
            except socket.timeout:
                pass
            if not self.registered and time.time() > hs_deadline:
                raise ConnectionError("sin respuesta al handshake en 3 s")
            if time.time() - last_rx > 6:
                raise ConnectionError("el LV1 dejó de mandar ping")

            while len(rx) >= 4 + HEADER_LEN:
                size = struct.unpack_from(">I", rx, 0)[0]
                if size == 0 or size > 16 * 1024 * 1024:
                    rx = rx[1:]  # resincronizar
                    continue
                total = 4 + HEADER_LEN + size
                if len(rx) < total:
                    break
                pkt = rx[4 + HEADER_LEN:total]
                rx = rx[total:]
                try:
                    addr, args = osc_decode(pkt)
                except Exception:
                    continue
                self._handle(addr, args)

    def _handle(self, addr, args):
        if addr == "/ping":
            with self.lock:
                if self.sock:
                    self.sock.sendall(lv1_frame("/pong", args))
            return
        if addr == "/handshake" and args and args[0] == ("i", 1) and not self.registered:
            self.registered = True
            ip, port = self.target
            self.status("on", f"Conectado a LV1 {ip}:{port}")
            self.log("Handshake OK — registrado como MyFOH")
            return
        if addr == "/Notify/SceneList" and args and args[0][0] == "i":
            count = args[0][1]
            fresh = {}
            rest = args[1:]
            for k in range(0, min(count * 2, len(rest) - 1), 2):
                if rest[k][0] == "i" and rest[k + 1][0] == "s":
                    fresh[rest[k][1]] = rest[k + 1][1]
            self.scenes = fresh
            self.events.put(("scenes", dict(fresh)))
            self.log(f"Lista de escenas: {len(fresh)}")
            if self.current_scene is not None:
                self._set_current(self.current_scene)
            return
        if addr == "/Notify/CurSceneIndex" and args and args[0][0] == "i":
            self._set_current(args[0][1])
            return
        if addr == "/Notify/Scene/Name" and args and args[0][0] == "s":
            if self.current_scene is not None:
                self.scenes[self.current_scene] = args[0][1]
                self.events.put(("scenes", dict(self.scenes)))
                self._set_current(self.current_scene)


# ─────────────────────────── Escucha OSC por UDP ───────────────────────────

class OSCListener:
    def __init__(self, port, client: LV1Client, events: queue.Queue):
        self.port = port
        self.client = client
        self.events = events
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                data, src = self.sock.recvfrom(65535)
            except OSError:
                return
            try:
                for addr, args in self._unbundle(data):
                    self._dispatch(addr, args)
            except Exception as e:
                self.events.put(("log", f"Error con mensaje de {src[0]}: {e}"))

    def _unbundle(self, data):
        if data.startswith(b"#bundle\x00"):
            pos = 16
            while pos + 4 <= len(data):
                n = struct.unpack_from(">i", data, pos)[0]
                pos += 4
                yield from self._unbundle(data[pos:pos + n])
                pos += n
        else:
            yield osc_decode(data)

    @staticmethod
    def _num(args):
        for t, v in args:
            if t in ("i", "h"):
                return int(v)
            if t in ("f", "d"):
                return int(round(v))
            if t == "s" and v.strip().lstrip("-").isdigit():
                return int(v)
        return None

    def _dispatch(self, addr, args):
        c = self.client
        a = addr.rstrip("/").lower()
        if a == "/lv1":
            # Sintaxis principal: /lv1 "<texto libre>"
            #   "Canción A" -> escena por nombre · 3 -> escena 3 · "next"/"prev"
            text = next((str(v) for t, v in args if t == "s"), None)
            if text is None:
                n = self._num(args)
                return None if n is None else c.recall_index(n - 1)
            cmd = c._norm(text)
            if cmd in ("next", "siguiente", "sig"):
                return c.step(+1)
            if cmd in ("prev", "previous", "anterior", "ant"):
                return c.step(-1)
            if text.strip().isdigit() and not any(c._norm(n) == cmd for n in c.scenes.values()):
                return c.recall_index(int(text.strip()) - 1)
            return c.recall_name(text)
        if not addr.lower().startswith("/lv1/"):
            return
        if a == "/lv1/scene":
            n = self._num(args)
            if n is None:
                # /lv1/scene "Nombre" también vale
                name = next((v for t, v in args if t == "s"), None)
                return c.recall_name(name) if name else None
            return c.recall_index(n - 1)
        if a == "/lv1/scene/index":
            n = self._num(args)
            return None if n is None else c.recall_index(n)
        if a == "/lv1/scene/name":
            name = next((v for t, v in args if t == "s"), None)
            return None if name is None else c.recall_name(name)
        if a == "/lv1/scene/next":
            # ignora el "release" de botones que mandan 1 y luego 0
            if args and self._num(args) == 0:
                return
            return c.step(+1)
        if a in ("/lv1/scene/prev", "/lv1/scene/previous"):
            if args and self._num(args) == 0:
                return
            return c.step(-1)
        if a.startswith("/lv1/raw/"):
            target = addr[len("/lv1/raw"):]
            c.send(target, args)
            return self.events.put(("log", f"→ raw {target} {[v for _, v in args]}"))

        # Sintaxis corta: el nombre de la escena va en la dirección.
        #   /lv1/intro   /lv1/Cancion_A   /lv1/next   /lv1/prev   /lv1/3
        # Mayúsculas, tildes, "_" y "-" no importan: /lv1/cancion_a == "Canción A".
        if self._is_release(args):
            return  # botones que mandan 1 al apretar y 0 al soltar: solo cuenta el 1
        text = addr[len("/lv1/"):].strip("/").replace("/", " ")
        cmd = c._norm(text)
        if not cmd:
            return
        if cmd in ("next", "siguiente", "sig"):
            return c.step(+1)
        if cmd in ("prev", "previous", "anterior", "ant"):
            return c.step(-1)
        if cmd.isdigit() and not any(c._norm(n) == cmd for n in c.scenes.values()):
            return c.recall_index(int(cmd) - 1)
        return c.recall_name(text)

    def _is_release(self, args):
        if not args:
            return False
        t, v = args[0]
        if t == "F":
            return True
        return t in ("i", "h", "f", "d") and v == 0


# ─────────────────────────── Interfaz ───────────────────────────

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not cfg.get("uuid"):
        h = "0123456789ABCDEF"
        u = "".join(random.choice(h) for _ in range(32))
        cfg["uuid"] = f"{u[:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"
    save_config(cfg)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


class App:
    COLORS = {"on": "#2ecc71", "search": "#f1c40f", "off": "#e74c3c"}

    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.events = queue.Queue()
        root.title(f"{APP_NAME} {VERSION}")
        root.geometry("520x560")
        root.minsize(420, 420)

        top = ttk.Frame(root, padding=12)
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=22, height=22, highlightthickness=0)
        self.dot_id = self.dot.create_oval(3, 3, 19, 19, fill=self.COLORS["off"], outline="")
        self.dot.pack(side="left")
        self.status_lbl = ttk.Label(top, text="Iniciando…", font=("", 12, "bold"))
        self.status_lbl.pack(side="left", padx=8)

        info = ttk.Frame(root, padding=(12, 0))
        info.pack(fill="x")
        ips = ", ".join(local_ipv4s()) or "?"
        ttk.Label(info, text=f"Manda OSC a  {ips}  puerto  {self.cfg['listen_port']}").pack(anchor="w")
        ttk.Label(info, text='/lv1/intro   ·   /lv1/next   ·   /lv1/prev   ·   /lv1/3   (mayúsculas y tildes no importan)',
                  foreground="#888").pack(anchor="w", pady=(2, 0))

        conn = ttk.Frame(root, padding=(12, 10, 12, 0))
        conn.pack(fill="x")
        ttk.Label(conn, text="IP del LV1:").pack(side="left")
        self.ip_var = tk.StringVar(value=self.cfg.get("lv1_ip", ""))
        ip_entry = ttk.Entry(conn, textvariable=self.ip_var, width=18)
        ip_entry.pack(side="left", padx=6)
        ip_entry.bind("<Return>", lambda _e: self._apply_ip())
        ttk.Button(conn, text="Conectar", command=self._apply_ip).pack(side="left")
        ttk.Label(conn, text="vacío = automático", foreground="#888").pack(side="left", padx=8)

        cur = ttk.Frame(root, padding=12)
        cur.pack(fill="x")
        ttk.Label(cur, text="Escena actual:").pack(side="left")
        self.cur_lbl = ttk.Label(cur, text="—", font=("", 14, "bold"))
        self.cur_lbl.pack(side="left", padx=8)

        mid = ttk.Frame(root, padding=(12, 0))
        mid.pack(fill="both", expand=True)
        self.scene_list = tk.Listbox(mid, height=8, activestyle="none")
        self.scene_list.pack(fill="both", expand=True)
        self.scene_list.bind("<Double-Button-1>", self._click_scene)
        ttk.Label(mid, text="Doble clic en una escena para probarla", foreground="#888").pack(anchor="w")

        logf = ttk.Frame(root, padding=12)
        logf.pack(fill="both", expand=True)
        self.log_txt = tk.Text(logf, height=8, state="disabled", font=("Courier", 9))
        self.log_txt.pack(fill="both", expand=True)

        bottom = ttk.Frame(root, padding=(12, 0, 12, 12))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Ajustes…", command=self._settings).pack(side="right")

        self.client = LV1Client(self.cfg, self.events)
        threading.Thread(target=self.client.run, daemon=True).start()
        try:
            OSCListener(int(self.cfg["listen_port"]), self.client, self.events).start()
            self._log(f"Escuchando OSC en UDP {self.cfg['listen_port']}")
        except OSError as e:
            self._log(f"¡No pude abrir el puerto UDP {self.cfg['listen_port']}! {e}")

        root.after(100, self._pump)

    def _log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg + "\n"
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", line)
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_txt.delete("1.0", f"{lines - 500}.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._log(ev[1])
                elif kind == "status":
                    self.dot.itemconfig(self.dot_id, fill=self.COLORS.get(ev[1], "#999"))
                    self.status_lbl.configure(text=ev[2])
                elif kind == "scenes":
                    self.scene_list.delete(0, "end")
                    for i in sorted(ev[1]):
                        self.scene_list.insert("end", f"{i + 1:>3}  {ev[1][i]}")
                elif kind == "scene":
                    idx, name = ev[1], ev[2]
                    self.cur_lbl.configure(text=f"{idx + 1}  {name}")
                    self.scene_list.selection_clear(0, "end")
                    if idx < self.scene_list.size():
                        self.scene_list.selection_set(idx)
                        self.scene_list.see(idx)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _apply_ip(self):
        ip = self.ip_var.get().strip()
        if ip and not _is_ipv4(ip):
            self._log(f"'{ip}' no es una IP válida (ej: 192.168.1.10)")
            return
        self.cfg["lv1_ip"] = ip
        save_config(self.cfg)
        self._log(f"Conectando a {ip}…" if ip else "Modo automático: buscando LV1…")
        self.client.reconnect()

    def _click_scene(self, _e):
        sel = self.scene_list.curselection()
        if sel:
            try:
                self.client.recall_index(sel[0])
            except Exception as e:
                self._log(str(e))

    def _settings(self):
        w = tk.Toplevel(self.root)
        w.title("Ajustes")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=14)
        f.pack()
        fields = [
            ("Puerto OSC de entrada (UDP)", "listen_port"),
            ("Puerto del LV1 (0 = automático, recomendado)", "lv1_port"),
        ]
        vars_ = {}
        for r, (label, key) in enumerate(fields):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            ttk.Entry(f, textvariable=v, width=18).grid(row=r, column=1, padx=8)
            vars_[key] = v

        def save():
            try:
                new_listen = int(vars_["listen_port"].get())
                new_port = int(vars_["lv1_port"].get() or 0)
            except ValueError:
                return
            listen_changed = new_listen != int(self.cfg["listen_port"])
            port_changed = new_port != int(self.cfg.get("lv1_port") or 0)
            self.cfg["listen_port"], self.cfg["lv1_port"] = new_listen, new_port
            save_config(self.cfg)
            if port_changed:
                if new_port and not self.cfg.get("lv1_ip"):
                    self._log("Para fijar el puerto, pon también la IP del LV1.")
                self.client.reconnect()
            if listen_changed:
                self._log("Puerto OSC guardado. Cierra y vuelve a abrir la app para aplicarlo.")
            w.destroy()

        ttk.Button(f, text="Guardar", command=save).grid(row=len(fields), column=1, sticky="e", pady=(10, 0))


def main():
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:
        import traceback
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("CRASH:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
