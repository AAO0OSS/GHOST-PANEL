r"""
GhostPanel - Panel de notas para streaming, invisible en OBS.

La ventana se ve con normalidad en tu monitor, pero el compositor de Windows
la excluye de cualquier captura de pantalla (OBS, Zoom, Discord, Meet, Recorte
de pantalla...). Se apoya en la API oficial de Windows SetWindowDisplayAffinity
con el flag WDA_EXCLUDEFROMCAPTURE.

Admite notas de texto e imagenes (insertadas desde archivo o pegadas del
portapapeles con Ctrl+V). Todo se guarda en %APPDATA%\GhostPanel, salvo en
modo portable (ver state_dir), donde se guarda junto al propio script.

Incluye una pestaña de chat con IA que se dibuja dentro del panel (por tanto
invisible en la grabacion) y habla con una IA local via Ollama: gratis, sin
clave y privada, porque nada sale de tu equipo.

Si junto al script hay una carpeta ollama\ (como en la copia del USB), el
propio panel levanta ese servidor cuando hace falta y lo mata al cerrarse, de
modo que la IA funciona en ordenadores que no tienen Ollama instalado.

El archivo usa la extension .pyw a proposito: Windows lo abre con pythonw
(el lanzador pyw.exe) y por tanto NO aparece ninguna ventana de consola. Si
alguna vez hace falta ejecutarlo a mano sin consola: pythonw ghostpanel.pyw.
Los fallos no se pierden: quedan escritos en %APPDATA%\GhostPanel\error.log.

Requiere Windows 10 version 2004 (build 19041) o superior.
Pillow es opcional pero recomendado: sin el solo se pueden usar PNG y GIF,
y no se puede pegar del portapapeles. PyMuPDF es opcional y sirve para meter
PDFs: sus paginas se rasterizan y se insertan como imagenes, porque abrir un
visor externo crearia una ventana sin proteger que OBS si grabaria.
"""

import csv
import ctypes
import ctypes.wintypes as wintypes
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageGrab, ImageTk
    HAVE_PIL = True
except ImportError:
    # Sin Pillow seguimos funcionando, pero solo con PNG/GIF y sin pegar
    # imagenes del portapapeles: Tk no sabe leer JPEG ni el portapapeles.
    HAVE_PIL = False

try:
    import fitz  # PyMuPDF
    HAVE_PDF = True
except ImportError:
    try:
        import pymupdf as fitz  # el paquete se renombro en las versiones nuevas
        HAVE_PDF = True
    except ImportError:
        HAVE_PDF = False

APP_NAME = "GhostPanel"

# Si este archivo existe junto al script, GhostPanel es portable: guarda sus
# datos en el USB y no deja rastro en el ordenador donde se enchufa.
PORTABLE_MARKER = "PORTABLE.txt"

# Ollama portable: binarios y almacen de modelos, ambos junto al script.
OLLAMA_DIR = "ollama"
OLLAMA_MODELS_DIR = "modelos"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"

# Arranque en frio desde un USB 2.0: el modelo son casi 2 GB que hay que leer
# del pendrive antes de la primera respuesta. De ahi el margen tan generoso.
OLLAMA_BOOT_TIMEOUT = 20      # segundos que esperamos a que el servidor abra
OLLAMA_REPLY_TIMEOUT = 600    # y a que conteste, incluida la carga del modelo

# --- Constantes de Windows ---------------------------------------------------

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Desaparece del todo (Win10 2004+)

# WDA_MONITOR (0x1) existe desde Win7 y deja la ventana como un rectangulo
# NEGRO en la grabacion. No lo usamos nunca: un rectangulo negro flotando
# delata que hay algo oculto, que es justo lo que no queremos. Si no podemos
# conseguir invisibilidad real, el panel se oculta solo (ver auto_hide).

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080  # fuera de la barra de tareas y de Alt+Tab
WS_EX_APPWINDOW = 0x00040000   # lo contrario: fuerza su presencia en la barra

VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_H = 0x48

CREATE_NO_WINDOW = 0x08000000

# Nombres de proceso de OBS Studio segun version e instalador.
OBS_PROCESS_NAMES = {"obs64.exe", "obs32.exe", "obs.exe"}

if sys.platform != "win32":
    sys.exit("GhostPanel solo funciona en Windows: la exclusion de captura "
             "es una funcion del compositor de Windows (DWM).")

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.SetWindowDisplayAffinity.argtypes = (wintypes.HWND, wintypes.DWORD)
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.GetWindowDisplayAffinity.argtypes = (wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD))
user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
user32.GetParent.argtypes = (wintypes.HWND,)
user32.GetParent.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short

# En 64 bits hay que usar las variantes Ptr: las W truncan el valor a 32 bits.
if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_window_long = user32.GetWindowLongPtrW
    _set_window_long = user32.SetWindowLongPtrW
    _LONG_PTR = ctypes.c_longlong
else:
    _get_window_long = user32.GetWindowLongW
    _set_window_long = user32.SetWindowLongW
    _LONG_PTR = ctypes.c_long

_get_window_long.argtypes = (wintypes.HWND, ctypes.c_int)
_get_window_long.restype = _LONG_PTR
_set_window_long.argtypes = (wintypes.HWND, ctypes.c_int, _LONG_PTR)
_set_window_long.restype = _LONG_PTR


def taskbar_style_applied(hwnd):
    """True si la ventana ya esta marcada como herramienta."""
    style = _get_window_long(hwnd, GWL_EXSTYLE)
    return bool(style & WS_EX_TOOLWINDOW) and not (style & WS_EX_APPWINDOW)


def hide_from_taskbar(hwnd):
    """Saca la ventana de la barra de tareas y de Alt+Tab.

    WS_EX_TOOLWINDOW es la forma soportada de hacerlo. La barra de tareas solo
    relee este estilo cuando la ventana se oculta y se vuelve a mostrar, asi
    que quien llame a esto debe encargarse de ese ciclo.
    """
    style = _get_window_long(hwnd, GWL_EXSTYLE)
    wanted = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    if style == wanted:
        return False
    _set_window_long(hwnd, GWL_EXSTYLE, wanted)
    return True


def toplevel_hwnd(window):
    """HWND de la ventana de nivel superior real que hay detras de un widget Tk.

    Tk envuelve cada toplevel en una ventana padre; winfo_id() devuelve la
    interior, y la afinidad de captura hay que aplicarla a la exterior.
    """
    window.update_idletasks()
    hwnd = user32.GetParent(window.winfo_id())
    return hwnd or window.winfo_id()


def set_capture_affinity(hwnd, affinity):
    """Aplica la afinidad de captura. Devuelve (ok, mensaje_de_error)."""
    if not user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd),
                                           wintypes.DWORD(affinity)):
        return False, ctypes.FormatError(ctypes.get_last_error()).strip()
    return True, ""


def get_capture_affinity(hwnd):
    """Afinidad actual de la ventana, o None si no se puede consultar."""
    value = wintypes.DWORD()
    if user32.GetWindowDisplayAffinity(wintypes.HWND(hwnd),
                                       ctypes.byref(value)):
        return value.value
    return None


# --- Deteccion de OBS --------------------------------------------------------

def _process_names_psutil():
    import psutil  # opcional; si no esta instalado usamos tasklist

    names = set()
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name")
        if name:
            names.add(name.lower())
    return names


def _process_names_tasklist():
    """Fallback sin dependencias. CREATE_NO_WINDOW evita el flash de consola."""
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
        creationflags=CREATE_NO_WINDOW, check=False,
    )
    names = set()
    for row in csv.reader(result.stdout.splitlines()):
        if row:
            names.add(row[0].lower())
    return names


def obs_is_running():
    try:
        try:
            names = _process_names_psutil()
        except ImportError:
            names = _process_names_tasklist()
    except Exception:
        return None  # no hemos podido comprobarlo
    return bool(names & OBS_PROCESS_NAMES)


# --- Persistencia ------------------------------------------------------------

def script_dir():
    """Carpeta donde vive este archivo, sea en el disco o en un USB."""
    if getattr(sys, "frozen", False):  # por si algun dia se empaqueta a .exe
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def portable_ollama_exe():
    """Ruta del ollama.exe que viaja junto al script, o None si no viaja."""
    path = os.path.join(script_dir(), OLLAMA_DIR, "ollama.exe")
    return path if os.path.exists(path) else None


def state_dir():
    """Carpeta de datos: junto al script si es portable, y si no en APPDATA.

    El modo portable se activa con un PORTABLE.txt al lado del script, que es
    como viene la copia del USB. Es explicito a proposito: la copia del disco
    duro sigue usando APPDATA y no se le mueven las notas de sitio.

    Si el USB estuviera protegido contra escritura caemos a APPDATA, que es
    peor para la privacidad pero al menos deja guardar.
    """
    if os.path.exists(os.path.join(script_dir(), PORTABLE_MARKER)):
        path = os.path.join(script_dir(), "datos")
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            pass
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


LOG_PATH = os.path.join(state_dir(), "error.log")


def log_error(context, error):
    """Deja rastro de un fallo. Un .pyw no tiene consola donde verlo."""
    import traceback
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("=== %s ===\n" % context)
            handle.write("".join(traceback.format_exception(
                type(error), error, error.__traceback__)))
            handle.write("\n")
    except OSError:
        pass


NOTES_PATH = os.path.join(state_dir(), "notes.txt")   # formato antiguo
DOC_PATH = os.path.join(state_dir(), "document.json")
CONFIG_PATH = os.path.join(state_dir(), "config.json")
DEFAULT_IMAGE_WIDTH = 380
MIN_IMAGE_WIDTH = 60
MAX_IMAGE_WIDTH = 2000
PDF_PAGE_WARNING = 10

ZOOM_STEP = 1.15         # lo que crece o mengua por muesca de rueda
VIEWER_MIN_WIDTH = 40
VIEWER_MAX_WIDTH = 5000  # tope del visor: mas alla el zoom se come la RAM

HINT = ("Ctrl+Alt+H oculta/muestra  ·  Ctrl+rueda = zoom  ·  "
        "doble clic = pantalla completa")


def images_dir():
    path = os.path.join(state_dir(), "images")
    os.makedirs(path, exist_ok=True)
    return path

DEFAULT_CONFIG = {"protected": True, "topmost": True, "hide_cursor": True,
                  "ollama_model": "llama3.2", "geometry": "480x560"}


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return config
    if isinstance(saved, dict):
        # Solo las claves que conocemos: asi una opcion retirada no sobrevive
        # para siempre en el config.json, porque save_config lo reescribe tal
        # cual al cerrar.
        config.update({key: value for key, value in saved.items()
                       if key in DEFAULT_CONFIG})
    return config


def save_config(config):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
    except OSError:
        pass


def load_document():
    """Documento como lista de segmentos de texto e imagen."""
    try:
        with open(DOC_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass

    # Migracion desde el formato antiguo de solo texto.
    try:
        with open(NOTES_PATH, "r", encoding="utf-8") as handle:
            text = handle.read()
        if text:
            return [{"t": "text", "v": text}]
    except OSError:
        pass
    return []


def save_document(segments):
    try:
        with open(DOC_PATH, "w", encoding="utf-8") as handle:
            json.dump(segments, handle, ensure_ascii=False, indent=1)
    except OSError:
        return False
    prune_unused_images(segments)
    return True


def prune_unused_images(segments):
    """Borra de disco las imagenes que ya no aparecen en el documento."""
    used = {seg.get("f") for seg in segments if seg.get("t") == "image"}
    try:
        for name in os.listdir(images_dir()):
            if name not in used:
                os.remove(os.path.join(images_dir(), name))
    except OSError:
        pass


def store_image_bytes(data, extension=".png"):
    """Guarda los bytes con un nombre derivado de su hash y lo devuelve."""
    name = hashlib.sha1(data).hexdigest()[:16] + extension
    path = os.path.join(images_dir(), name)
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(data)
    return name


# --- Aplicacion --------------------------------------------------------------

class GhostPanel:

    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.hwnd = None
        self.hotkey_was_down = False
        self.hidden = False
        self.auto_hidden = False
        self.real_invisibility = False
        self.auto_hide = False
        self.last_obs_state = None
        # Tk no mantiene viva la PhotoImage de un Text: si la suelta el
        # recolector, la imagen desaparece del panel. Guardamos referencias.
        self.photo_refs = []
        self.image_files = {}  # nombre Tk de la PhotoImage -> fichero en disco
        self.viewer = None         # overlay del visor, o None si esta cerrado
        self.viewer_canvas = None
        self.viewer_photo = None   # la PhotoImage a tamaño de zoom actual
        self.viewer_file = None
        self.viewer_zoom = 1.0
        self.ollama_process = None  # solo si lo hemos arrancado nosotros

        # Arrancamos oculta y solo mostramos la ventana cuando la proteccion
        # ya esta puesta: asi no se cuela ni un fotograma en la grabacion.
        root.withdraw()
        root.title(APP_NAME)
        root.geometry(self.config["geometry"])
        root.minsize(340, 260)
        root.configure(bg="#14161a")
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self._build_ui()

        self.hwnd = toplevel_hwnd(root)
        self.apply_protection(self.config["protected"], announce=False)
        self.apply_topmost(self.config["topmost"])

        # La ventana sigue en withdraw(), asi que el deiconify() de mas abajo
        # hace de ciclo ocultar/mostrar y la barra de tareas relee el estilo.
        hide_from_taskbar(self.hwnd)

        self.remember_cursors()
        self.apply_cursor_hiding(self.config["hide_cursor"])

        self.load_into_widget(load_document())

        root.deiconify()
        self.poll_obs()
        self.poll_hotkey()

    # -- interfaz --

    def _build_ui(self):
        root = self.root
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Ghost.TCheckbutton", background="#14161a",
                        foreground="#d8dee9", focuscolor="#14161a")
        style.map("Ghost.TCheckbutton", background=[("active", "#14161a")])

        header = tk.Frame(root, bg="#14161a")
        header.pack(fill="x", padx=12, pady=(12, 6))

        tk.Label(header, text=APP_NAME, bg="#14161a", fg="#eceff4",
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")

        self.shield_label = tk.Label(header, text="", bg="#14161a",
                                     fg="#a3be8c", font=("Segoe UI", 9),
                                     justify="left", wraplength=400)
        self.shield_label.pack(anchor="w", pady=(4, 0))

        self.obs_label = tk.Label(header, text="OBS: comprobando...",
                                  bg="#14161a", fg="#7b8494",
                                  font=("Segoe UI", 9))
        self.obs_label.pack(anchor="w")

        style.configure("Ghost.TNotebook", background="#14161a",
                        borderwidth=0)
        style.configure("Ghost.TNotebook.Tab", background="#1c1f26",
                        foreground="#d8dee9", padding=(12, 4), borderwidth=0)
        style.map("Ghost.TNotebook.Tab",
                  background=[("selected", "#3b4252")],
                  foreground=[("selected", "#eceff4")])

        notebook = ttk.Notebook(root, style="Ghost.TNotebook")
        notebook.pack(fill="both", expand=True, padx=12, pady=6)

        notes_tab = tk.Frame(notebook, bg="#14161a")
        notebook.add(notes_tab, text="Notas")

        self.notes = tk.Text(notes_tab, bg="#1c1f26", fg="#eceff4",
                             insertbackground="#eceff4", relief="flat",
                             font=("Consolas", 11), undo=True, wrap="word",
                             padx=10, pady=10)
        self.notes.pack(fill="both", expand=True)

        self._build_chat_tab(notebook)

        toggles = tk.Frame(root, bg="#14161a")
        toggles.pack(fill="x", padx=12, pady=(0, 2))

        self.protected_var = tk.BooleanVar(value=self.config["protected"])
        self.topmost_var = tk.BooleanVar(value=self.config["topmost"])
        self.cursor_var = tk.BooleanVar(value=self.config["hide_cursor"])

        ttk.Checkbutton(toggles, text="Invisible en capturas",
                        variable=self.protected_var, style="Ghost.TCheckbutton",
                        command=lambda: self.apply_protection(
                            self.protected_var.get())).pack(side="left")
        ttk.Checkbutton(toggles, text="Siempre encima",
                        variable=self.topmost_var, style="Ghost.TCheckbutton",
                        command=lambda: self.apply_topmost(
                            self.topmost_var.get())).pack(side="left", padx=10)
        ttk.Checkbutton(toggles, text="Sin cursor",
                        variable=self.cursor_var, style="Ghost.TCheckbutton",
                        command=lambda: self.apply_cursor_hiding(
                            self.cursor_var.get())).pack(side="left")

        controls = tk.Frame(root, bg="#14161a")
        controls.pack(fill="x", padx=12, pady=(0, 8))

        tk.Button(controls, text="Guardar", command=self.save,
                  bg="#3b4252", fg="#eceff4", relief="flat",
                  activebackground="#4c566a", activeforeground="#eceff4",
                  padx=14).pack(side="right")
        tk.Button(controls, text="Insertar", command=self.insert_file_dialog,
                  bg="#3b4252", fg="#eceff4", relief="flat",
                  activebackground="#4c566a", activeforeground="#eceff4",
                  padx=14).pack(side="right", padx=6)

        self.status_label = tk.Label(
            root, text=HINT, bg="#14161a", fg="#616a7a",
            font=("Segoe UI", 8), anchor="w")
        self.status_label.pack(fill="x", padx=12, pady=(0, 8))

        root.bind("<Control-s>", lambda _event: self.save())
        # Interceptamos el pegado: si el portapapeles trae una imagen la
        # insertamos, y si no dejamos que Tk pegue el texto como siempre.
        self.notes.bind("<Control-v>", self.on_paste)
        self.notes.bind("<Control-V>", self.on_paste)
        # Clic derecho sobre una imagen: menu de tamaño.
        self.notes.bind("<Button-3>", self.on_right_click)
        # Ctrl+rueda hace zoom sobre la imagen que haya bajo el puntero, y
        # doble clic la abre a pantalla completa dentro del panel.
        self.notes.bind("<Control-MouseWheel>", self.on_ctrl_wheel)
        self.notes.bind("<Double-Button-1>", self.on_double_click)

    # -- chat de IA --

    OLLAMA_URL = "http://localhost:11434/api/chat"

    def _build_chat_tab(self, notebook):
        tab = tk.Frame(notebook, bg="#14161a")
        notebook.add(tab, text="Chat IA")

        top = tk.Frame(tab, bg="#14161a")
        top.pack(fill="x", pady=(6, 4))

        tk.Label(top, text="Modelo de Ollama:", bg="#14161a", fg="#d8dee9",
                 font=("Segoe UI", 9)).pack(side="left")
        self.model_var = tk.StringVar(
            value=self.config.get("ollama_model", "llama3.2"))
        tk.Entry(top, textvariable=self.model_var, bg="#1c1f26", fg="#eceff4",
                 insertbackground="#eceff4", relief="flat", width=18,
                 font=("Segoe UI", 9)).pack(side="left", padx=6, ipady=2)

        self.chat_log = tk.Text(tab, bg="#1c1f26", fg="#eceff4", relief="flat",
                                font=("Segoe UI", 10), wrap="word", padx=10,
                                pady=10, state="disabled")
        self.chat_log.pack(fill="both", expand=True, pady=(4, 4))
        self.chat_log.tag_configure("user", foreground="#88c0d0",
                                    font=("Segoe UI", 10, "bold"))
        self.chat_log.tag_configure("ai", foreground="#a3be8c",
                                    font=("Segoe UI", 10, "bold"))
        self.chat_log.tag_configure("err", foreground="#bf616a")

        entry_row = tk.Frame(tab, bg="#14161a")
        entry_row.pack(fill="x", pady=(0, 6))
        self.chat_input = tk.Text(entry_row, height=2, bg="#1c1f26",
                                  fg="#eceff4", insertbackground="#eceff4",
                                  relief="flat", font=("Segoe UI", 10),
                                  wrap="word", padx=8, pady=6)
        self.chat_input.pack(side="left", fill="x", expand=True)
        # Intro envia; Shift+Intro hace salto de linea.
        self.chat_input.bind("<Return>", self._on_chat_return)
        self.chat_input.bind("<Shift-Return>", lambda _e: None)
        self.send_button = tk.Button(entry_row, text="Enviar",
                                     command=self.chat_send, bg="#3b4252",
                                     fg="#eceff4", relief="flat",
                                     activebackground="#4c566a",
                                     activeforeground="#eceff4", padx=12)
        self.send_button.pack(side="left", padx=(6, 0), fill="y")

        self.chat_history = []  # [(rol, texto)] para el contexto de la IA
        self.chat_busy = False

    def _on_chat_return(self, _event):
        self.chat_send()
        return "break"  # evita el salto de linea al enviar

    def append_chat(self, who, text, tag):
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", who + ": ", tag)
        self.chat_log.insert("end", text + "\n\n")
        self.chat_log.configure(state="disabled")
        self.chat_log.see("end")

    def chat_send(self):
        if self.chat_busy:
            return
        prompt = self.chat_input.get("1.0", "end-1c").strip()
        if not prompt:
            return
        self.chat_input.delete("1.0", "end")
        self.append_chat("Tu", prompt, "user")
        self.chat_history.append(("user", prompt))

        model = self.model_var.get().strip()
        self.config["ollama_model"] = model

        self.chat_busy = True
        self.send_button.configure(state="disabled", text="...")
        history = list(self.chat_history)
        threading.Thread(target=self._chat_worker, args=(model, history),
                         daemon=True).start()

    def _chat_worker(self, model, history):
        """Corre en un hilo: la red no debe congelar la ventana."""
        try:
            if not self.ollama_alive() and portable_ollama_exe():
                self.root.after(0, self.flash,
                                "Arrancando la IA local del USB, la primera "
                                "respuesta tarda...")
            self.ensure_ollama()
            reply = self._ask_ollama(model, history)
            self.root.after(0, self._chat_done, reply, None)
        except Exception as error:
            log_error("chat_ollama", error)
            self.root.after(0, self._chat_done, None, error)

    def _chat_done(self, reply, error):
        self.chat_busy = False
        self.send_button.configure(state="normal", text="Enviar")
        if error is not None:
            self.append_chat("Error", self._friendly_error(error), "err")
            return
        self.chat_history.append(("assistant", reply))
        self.append_chat("IA", reply, "ai")

    def _friendly_error(self, error):
        text = str(error)
        if isinstance(error, urllib.error.URLError) or "Connection" in text:
            if portable_ollama_exe():
                return ("No arranco la IA del USB. Comprueba que existen las "
                        "carpetas ollama\\ y modelos\\ junto al programa; "
                        "los detalles estan en datos\\error.log.")
            return ("No se pudo conectar con Ollama en localhost:11434. "
                    "Abrelo con 'ollama serve' y descarga un modelo con "
                    "'ollama pull llama3.2'.")
        return text

    # -- servidor Ollama portable --

    def ollama_alive(self, timeout=1.5):
        """True si ya hay un Ollama escuchando en el puerto de siempre."""
        try:
            with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=timeout):
                return True
        except Exception:
            return False

    def ensure_ollama(self):
        """Levanta el Ollama del USB si no hay ninguno en marcha.

        Se llama desde el hilo del chat, nunca desde el de la interfaz: abrir
        el servidor lleva unos segundos y congelaria la ventana.

        Si el ordenador ya tiene su propio Ollama corriendo no tocamos nada, y
        por eso tampoco lo mataremos al salir.
        """
        if self.ollama_alive():
            return True
        exe = portable_ollama_exe()
        if exe is None:
            return False

        if self.ollama_process is None or self.ollama_process.poll() is not None:
            environment = dict(os.environ)
            # Los modelos se leen del USB, no del perfil del anfitrion.
            environment["OLLAMA_MODELS"] = os.path.join(script_dir(),
                                                        OLLAMA_MODELS_DIR)
            # Sin esto Ollama descarga el modelo de la RAM a los 5 minutos de
            # inactividad y habria que releer 2 GB del pendrive. Mientras el
            # panel este abierto preferimos gastar RAM.
            environment["OLLAMA_KEEP_ALIVE"] = "-1"
            self.ollama_process = subprocess.Popen(
                [exe, "serve"], env=environment,
                creationflags=CREATE_NO_WINDOW,  # sin consola, como todo aqui
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)

        for _ in range(OLLAMA_BOOT_TIMEOUT * 2):
            if self.ollama_alive(timeout=1):
                return True
            if self.ollama_process.poll() is not None:
                return False  # se ha muerto al arrancar, no seguimos esperando
            time.sleep(0.5)
        return False

    def stop_ollama(self):
        """Mata el servidor que hayamos arrancado nosotros, y solo ese.

        Va por el arbol de procesos porque Ollama lanza un proceso aparte para
        ejecutar el modelo, y terminando solo el padre se quedaria huerfano.
        """
        if self.ollama_process is None:
            return
        try:
            subprocess.run(["taskkill", "/PID", str(self.ollama_process.pid),
                            "/T", "/F"], creationflags=CREATE_NO_WINDOW,
                           check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            self.ollama_process.wait(timeout=10)
        except Exception as error:
            log_error("stop_ollama", error)
        self.ollama_process = None

    def _ask_ollama(self, model, history):
        payload = json.dumps({
            "model": model or "llama3.2",
            "messages": [{"role": r, "content": c} for r, c in history],
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request,
                                    timeout=OLLAMA_REPLY_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("message", {}).get("content", "").strip() or \
            "(respuesta vacia)"

    # -- cursor --

    def remember_cursors(self):
        """Guarda el cursor propio de cada widget para poder restaurarlo."""
        self.default_cursors = {}
        self._record_cursors(self.root)

    def _record_cursors(self, widget):
        """Apunta el cursor de los widgets que aun no conociamos.

        Solo los nuevos: si el puntero ya esta oculto, releer todo guardaria
        "none" como cursor original y al desmarcar la casilla no habria forma
        de recuperar el puntero.
        """
        if widget not in self.default_cursors:
            try:
                self.default_cursors[widget] = widget.cget("cursor")
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._record_cursors(child)

    def adopt_cursors(self, widget):
        """Mete un widget recien creado en el ajuste de cursor.

        Sin esto el puntero reaparece encima del visor, y OBS graba el cursor
        del sistema aunque la ventana sea invisible.
        """
        self._record_cursors(widget)
        self.apply_cursor_hiding(self.cursor_var.get())

    def forget_cursors(self):
        """Quita del registro los widgets ya destruidos."""
        for widget in list(self.default_cursors):
            try:
                alive = widget.winfo_exists()
            except tk.TclError:
                alive = False
            if not alive:
                del self.default_cursors[widget]

    def apply_cursor_hiding(self, enabled):
        """Oculta el puntero mientras esta sobre el panel.

        OBS graba el cursor del sistema aunque la ventana sea invisible, asi
        que sin esto se ve el puntero moviendose y pulsando sobre la nada. Tk
        aplica el cursor por widget, y solo mientras el raton esta encima: al
        salir del panel vuelve el puntero normal por si solo.
        """
        for widget, original in self.default_cursors.items():
            try:
                widget.configure(cursor="none" if enabled else original)
            except tk.TclError:
                pass
        self.config["hide_cursor"] = bool(enabled)

    # -- imagenes y documento --

    def photo_for(self, filename, width):
        """PhotoImage escalada al ancho pedido, o None si no se puede leer."""
        path = os.path.join(images_dir(), filename)
        try:
            if HAVE_PIL:
                image = Image.open(path)
                image.load()
                if image.width != width:
                    height = max(1, round(image.height * width / image.width))
                    image = image.resize((width, height), Image.LANCZOS)
                return ImageTk.PhotoImage(image)

            # Sin Pillow solo hay zoom/subsample por factores enteros, asi que
            # el ancho final es aproximado.
            photo = tk.PhotoImage(file=path)
            if width < photo.width():
                factor = max(1, round(photo.width() / width))
                photo = photo.subsample(factor, factor)
            elif width > photo.width():
                factor = max(1, round(width / photo.width()))
                photo = photo.zoom(factor, factor)
            return photo
        except Exception:
            return None

    def natural_size(self, filename):
        """(ancho, alto) originales del archivo en disco."""
        path = os.path.join(images_dir(), filename)
        try:
            if HAVE_PIL:
                with Image.open(path) as image:
                    return image.width, image.height
            photo = tk.PhotoImage(file=path)
            return photo.width(), photo.height()
        except Exception:
            return DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_WIDTH

    def natural_width(self, filename):
        """Ancho original del archivo, para el 'tamaño original' del menu."""
        return self.natural_size(filename)[0]

    def panel_width(self):
        width = self.notes.winfo_width()
        return max(MIN_IMAGE_WIDTH, (width if width > 1 else 420) - 40)

    def place_image(self, filename, index=tk.INSERT, width=None):
        if width is None:
            width = min(DEFAULT_IMAGE_WIDTH, self.natural_width(filename))
        width = max(MIN_IMAGE_WIDTH, min(MAX_IMAGE_WIDTH, int(width)))

        photo = self.photo_for(filename, width)
        if photo is None:
            self.flash("No se pudo mostrar la imagen %s" % filename)
            return False
        self.notes.image_create(index, image=photo, padx=2, pady=4)
        self.photo_refs.append(photo)
        self.image_files[str(photo)] = {"file": filename, "width": width}
        return True

    # -- redimensionado --

    def image_at(self, index):
        """(nombre Tk, posicion) de la imagen en ese indice, o None."""
        try:
            items = self.notes.dump(index, "%s+1c" % index, image=True)
        except tk.TclError:
            return None
        for key, value, position in items:
            if key == "image" and value in self.image_files:
                return value, position
        return None

    def resize_image(self, tk_name, position, factor=None, width=None):
        info = self.image_files.get(tk_name)
        if not info:
            return
        if width is None:
            width = info["width"] * factor
        width = max(MIN_IMAGE_WIDTH, min(MAX_IMAGE_WIDTH, int(width)))
        if width == info["width"]:
            return

        # Tk no reescala una imagen ya insertada: hay que quitarla y volver a
        # ponerla en el mismo sitio con la nueva escala.
        self.notes.delete(position)
        self.forget_photo(tk_name)
        if self.place_image(info["file"], index=position, width=width):
            self.flash("Imagen a %d px de ancho" % width)

    def delete_image(self, tk_name, position):
        self.notes.delete(position)
        self.forget_photo(tk_name)
        self.flash("Imagen eliminada")

    def forget_photo(self, tk_name):
        """Suelta la PhotoImage que ya no esta en el panel.

        El zoom de rueda reemplaza la imagen en cada muesca, asi que sin esto
        photo_refs acumularia una copia por paso.
        """
        self.image_files.pop(tk_name, None)
        self.photo_refs = [photo for photo in self.photo_refs
                           if str(photo) != tk_name]

    def on_right_click(self, event):
        index = self.notes.index("@%d,%d" % (event.x, event.y))
        found = self.image_at(index)
        if not found:
            return
        tk_name, position = found
        info = self.image_files[tk_name]

        menu = tk.Menu(self.root, tearoff=0, bg="#1c1f26", fg="#eceff4",
                       activebackground="#3b4252", activeforeground="#eceff4",
                       borderwidth=0)
        menu.add_command(label="Ver con zoom (doble clic)",
                         command=lambda: self.open_viewer(info["file"]))
        menu.add_separator()
        menu.add_command(
            label="Mas grande",
            command=lambda: self.resize_image(tk_name, position, factor=1.25))
        menu.add_command(
            label="Mas pequeña",
            command=lambda: self.resize_image(tk_name, position, factor=0.8))
        menu.add_separator()
        menu.add_command(
            label="Ajustar al panel",
            command=lambda: self.resize_image(tk_name, position,
                                              width=self.panel_width()))
        menu.add_command(
            label="Tamaño original (%d px)" % self.natural_width(info["file"]),
            command=lambda: self.resize_image(
                tk_name, position, width=self.natural_width(info["file"])))
        menu.add_separator()
        menu.add_command(label="Eliminar",
                         command=lambda: self.delete_image(tk_name, position))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    # -- zoom --

    def on_ctrl_wheel(self, event):
        """Ctrl+rueda: agranda o reduce la imagen que hay bajo el puntero."""
        found = self.image_at(self.notes.index("@%d,%d" % (event.x, event.y)))
        if not found:
            return None  # sin imagen debajo dejamos el scroll normal del Text
        tk_name, position = found
        self.resize_image(tk_name, position,
                          factor=ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP)
        return "break"

    def on_double_click(self, event):
        """Doble clic sobre una imagen: la abre en el visor con zoom."""
        found = self.image_at(self.notes.index("@%d,%d" % (event.x, event.y)))
        if not found:
            return None  # que Tk seleccione la palabra, como siempre
        self.open_viewer(self.image_files[found[0]]["file"])
        return "break"

    def open_viewer(self, filename):
        """Visor con zoom, dibujado DENTRO del panel.

        A proposito no es un Toplevel: la exclusion de captura se aplica por
        ventana, asi que una ventana nueva apareceria en la grabacion. Este
        visor es un Frame colocado encima del panel, y por tanto hereda su
        invisibilidad.
        """
        self.close_viewer()

        overlay = tk.Frame(self.root, bg="#0b0c0f")
        overlay.place(x=0, y=0, relwidth=1, relheight=1)

        bar = tk.Frame(overlay, bg="#0b0c0f")
        bar.pack(fill="x", padx=8, pady=(8, 4))

        self.viewer_label = tk.Label(bar, text="", bg="#0b0c0f", fg="#d8dee9",
                                     font=("Segoe UI", 9))
        self.viewer_label.pack(side="left")

        # De derecha a izquierda, que es como los coloca pack(side="right").
        for text, command in (("Cerrar", self.close_viewer),
                              ("+", lambda: self.viewer_scale(ZOOM_STEP)),
                              ("-", lambda: self.viewer_scale(1 / ZOOM_STEP)),
                              ("100%", lambda: self.viewer_set_zoom(1.0)),
                              ("Ajustar", self.viewer_fit)):
            tk.Button(bar, text=text, command=command, bg="#3b4252",
                      fg="#eceff4", relief="flat", activebackground="#4c566a",
                      activeforeground="#eceff4", padx=8).pack(side="right",
                                                               padx=3)

        canvas = tk.Canvas(overlay, bg="#0b0c0f", highlightthickness=0,
                           takefocus=1)
        canvas.pack(fill="both", expand=True, padx=8)

        tk.Label(overlay, text="rueda = zoom  ·  arrastrar = mover  ·  "
                               "Esc = cerrar", bg="#0b0c0f", fg="#616a7a",
                 font=("Segoe UI", 8)).pack(fill="x", padx=8, pady=(4, 8))

        self.viewer = overlay
        self.viewer_canvas = canvas
        self.viewer_file = filename
        self.viewer_zoom = 1.0

        canvas.bind("<MouseWheel>", self.viewer_on_wheel)
        canvas.bind("<Control-MouseWheel>", self.viewer_on_wheel)
        # scan_mark/scan_dragto es el arrastre nativo del Canvas.
        canvas.bind("<ButtonPress-1>",
                    lambda event: canvas.scan_mark(event.x, event.y))
        canvas.bind("<B1-Motion>",
                    lambda event: canvas.scan_dragto(event.x, event.y, gain=1))
        canvas.bind("<Escape>", lambda _event: self.close_viewer())
        canvas.bind("<Double-Button-1>", lambda _event: self.viewer_fit())
        for sequence in ("<plus>", "<equal>", "<KP_Add>"):
            canvas.bind(sequence, lambda _event: self.viewer_scale(ZOOM_STEP))
        for sequence in ("<minus>", "<KP_Subtract>"):
            canvas.bind(sequence,
                        lambda _event: self.viewer_scale(1 / ZOOM_STEP))
        canvas.bind("<Key-0>", lambda _event: self.viewer_fit())
        canvas.focus_set()

        self.adopt_cursors(overlay)
        # El canvas aun no tiene tamaño real: esperamos para poder ajustar.
        self.root.after_idle(self.viewer_fit)

    def close_viewer(self):
        if self.viewer is None:
            return
        self.viewer.destroy()
        self.viewer = None
        self.viewer_canvas = None
        self.viewer_photo = None
        self.viewer_file = None
        self.forget_cursors()
        self.notes.focus_set()

    def viewer_on_wheel(self, event):
        self.viewer_scale(ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP)
        return "break"

    def viewer_scale(self, factor):
        self.viewer_set_zoom(self.viewer_zoom * factor)

    def viewer_set_zoom(self, zoom):
        if self.viewer is None:
            return
        natural = max(1, self.natural_size(self.viewer_file)[0])
        self.viewer_zoom = max(VIEWER_MIN_WIDTH / natural,
                               min(VIEWER_MAX_WIDTH / natural, zoom))
        self.viewer_render()

    def viewer_fit(self):
        """Zoom que hace entrar la imagen completa en el hueco disponible."""
        if self.viewer is None:
            return
        canvas = self.viewer_canvas
        canvas.update_idletasks()
        width, height = self.natural_size(self.viewer_file)
        self.viewer_set_zoom(min(canvas.winfo_width() / max(1, width),
                                 canvas.winfo_height() / max(1, height)))

    def viewer_render(self):
        canvas = self.viewer_canvas
        width = round(self.natural_size(self.viewer_file)[0] * self.viewer_zoom)
        width = max(VIEWER_MIN_WIDTH, min(VIEWER_MAX_WIDTH, width))
        photo = self.photo_for(self.viewer_file, width)
        if photo is None:
            self.close_viewer()
            self.flash("No se pudo abrir la imagen en el visor")
            return

        # Guardamos el centro para que el zoom no salte al principio.
        before_x, before_y = self.viewer_center()

        canvas.delete("all")
        # Si cabe de sobra la centramos; si no, arriba a la izquierda y se
        # recorre arrastrando.
        left = max(0, (canvas.winfo_width() - photo.width()) // 2)
        top = max(0, (canvas.winfo_height() - photo.height()) // 2)
        canvas.create_image(left, top, anchor="nw", image=photo)
        self.viewer_photo = photo  # sin esta referencia se la lleva el GC
        canvas.configure(scrollregion=(0, 0, left + photo.width(),
                                       top + photo.height()))
        canvas.update_idletasks()
        self.viewer_recenter(before_x, before_y)

        self.viewer_label.config(text="%d%%  ·  %d × %d px" % (
            round(self.viewer_zoom * 100), photo.width(), photo.height()))

    def viewer_center(self):
        """Centro visible actual, en fracciones del area total."""
        canvas = self.viewer_canvas
        first_x, last_x = canvas.xview()
        first_y, last_y = canvas.yview()
        return (first_x + last_x) / 2, (first_y + last_y) / 2

    def viewer_recenter(self, center_x, center_y):
        canvas = self.viewer_canvas
        first_x, last_x = canvas.xview()
        first_y, last_y = canvas.yview()
        canvas.xview_moveto(max(0.0, center_x - (last_x - first_x) / 2))
        canvas.yview_moveto(max(0.0, center_y - (last_y - first_y) / 2))

    def load_into_widget(self, segments):
        self.notes.delete("1.0", "end")
        self.photo_refs.clear()
        self.image_files.clear()
        for segment in segments:
            if segment.get("t") == "text":
                self.notes.insert("end", segment.get("v", ""))
            elif segment.get("t") == "image" and segment.get("f"):
                self.place_image(segment["f"], index="end",
                                 width=segment.get("w"))
        self.notes.edit_modified(False)

    def serialize(self):
        """Recorre el Text conservando el orden de texto e imagenes."""
        segments = []
        for key, value, _index in self.notes.dump("1.0", "end-1c",
                                                  text=True, image=True):
            if key == "text":
                if segments and segments[-1]["t"] == "text":
                    segments[-1]["v"] += value
                else:
                    segments.append({"t": "text", "v": value})
            elif key == "image":
                info = self.image_files.get(value)
                if info:
                    segments.append({"t": "image", "f": info["file"],
                                     "w": info["width"]})
        return segments

    # -- insertar archivos --

    def insert_file_dialog(self):
        patterns = "*.png *.gif" if not HAVE_PIL else \
            "*.png *.gif *.jpg *.jpeg *.bmp *.webp"
        if HAVE_PDF:
            patterns += " *.pdf"
        types = [("Imagenes y PDF" if HAVE_PDF else "Imagenes", patterns),
                 ("Todos los archivos", "*.*")]
        path = filedialog.askopenfilename(parent=self.root,
                                          title="Insertar archivo",
                                          filetypes=types)
        if not path:
            return
        if os.path.splitext(path)[1].lower() == ".pdf":
            self.insert_pdf_path(path)
        else:
            self.insert_image_path(path)

    def insert_image_path(self, path):
        try:
            if HAVE_PIL:
                # Normalizamos a PNG: asi una sola rama sabe leer lo guardado.
                with Image.open(path) as image:
                    buffer = io.BytesIO()
                    image.convert("RGBA").save(buffer, format="PNG")
                filename = store_image_bytes(buffer.getvalue())
            else:
                extension = os.path.splitext(path)[1].lower()
                if extension not in (".png", ".gif"):
                    self.flash("Sin Pillow solo se admiten PNG y GIF. "
                               "Instalalo con: pip install pillow")
                    return
                with open(path, "rb") as handle:
                    filename = store_image_bytes(handle.read(), extension)
        except Exception as error:
            self.flash("No se pudo leer la imagen: %s" % error)
            return

        if self.place_image(filename):
            self.flash("Imagen insertada")

    def insert_pdf_path(self, path):
        """Rasteriza el PDF e inserta cada pagina como imagen.

        Abrirlo en un visor externo crearia una ventana sin la exclusion de
        captura, asi que OBS la grabaria: por eso lo traemos al panel.
        """
        if not HAVE_PDF:
            self.flash("Para insertar PDFs instala PyMuPDF: "
                       "pip install pymupdf")
            return
        try:
            document = fitz.open(path)
        except Exception as error:
            self.flash("No se pudo abrir el PDF: %s" % error)
            return

        inserted = 0
        try:
            total = document.page_count
            if total == 0:
                self.flash("Ese PDF no tiene paginas.")
                return
            if total > PDF_PAGE_WARNING and not messagebox.askyesno(
                    "Insertar PDF",
                    "El PDF tiene %d paginas y se insertaran todas como "
                    "imagenes.\n\n¿Continuar?" % total, parent=self.root):
                return

            # Zoom 2x sobre los 72 dpi del PDF: legible sin inflar el disco.
            matrix = fitz.Matrix(2, 2)
            for page in document:
                try:
                    pixmap = page.get_pixmap(matrix=matrix)
                    filename = store_image_bytes(pixmap.tobytes("png"))
                except Exception:
                    continue
                if self.place_image(filename):
                    self.notes.insert(tk.INSERT, "\n")
                    inserted += 1
        finally:
            document.close()

        if inserted:
            self.flash("PDF insertado: %d pagina(s)" % inserted)
        else:
            self.flash("No se pudo renderizar ninguna pagina del PDF.")

    def on_paste(self, _event=None):
        """Ctrl+V: imagen o PDF si los hay, y si no el pegado normal de texto."""
        if not HAVE_PIL:
            return None
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            return None

        if isinstance(clip, list) and clip:
            inserted = False
            for path in clip:
                if not os.path.isfile(path):
                    continue
                if os.path.splitext(path)[1].lower() == ".pdf":
                    self.insert_pdf_path(path)
                else:
                    self.insert_image_path(path)
                inserted = True
            return "break" if inserted else None

        if clip is not None and hasattr(clip, "save"):
            try:
                buffer = io.BytesIO()
                clip.convert("RGBA").save(buffer, format="PNG")
                filename = store_image_bytes(buffer.getvalue())
            except Exception as error:
                self.flash("No se pudo pegar la imagen: %s" % error)
                return "break"
            if self.place_image(filename):
                self.flash("Imagen pegada")
            return "break"
        return None

    # -- proteccion --

    def apply_protection(self, enabled, announce=True):
        if not enabled:
            set_capture_affinity(self.hwnd, WDA_NONE)
            self.real_invisibility = False
            self.auto_hide = False
            self.protected_var.set(False)
            self.config["protected"] = False
            self.shield_label.config(
                text="Visible: el panel SI aparece en la grabacion.",
                fg="#ebcb8b")
            if announce:
                self.flash("Proteccion desactivada")
            return

        # Pedimos siempre la invisibilidad real y comprobamos que Windows la
        # haya aceptado de verdad, en vez de fiarnos del numero de build.
        ok, error = set_capture_affinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)
        confirmed = ok and get_capture_affinity(
            self.hwnd) == WDA_EXCLUDEFROMCAPTURE

        self.real_invisibility = confirmed
        self.protected_var.set(True)
        self.config["protected"] = True

        if confirmed:
            self.auto_hide = False
            self.shield_label.config(
                text="Invisible total: OBS compone lo que hay detras del panel, "
                     "no queda ningun rectangulo.", fg="#a3be8c")
        else:
            # Sin invisibilidad real no dejamos WDA_MONITOR puesto: preferimos
            # que la ventana no este en pantalla a que salga un cuadro negro.
            set_capture_affinity(self.hwnd, WDA_NONE)
            self.auto_hide = True
            detail = error or "tu version de Windows no lo admite"
            self.shield_label.config(
                text="Sin invisibilidad real (%s). Para que NO salga un "
                     "rectangulo negro, el panel se ocultara solo mientras OBS "
                     "este abierto." % detail, fg="#bf616a")

        if announce:
            self.flash("Proteccion actualizada")

    def verify_protection(self):
        """Revalida la afinidad por si Windows recreo la ventana."""
        if not self.protected_var.get():
            return
        if get_capture_affinity(self.hwnd) != WDA_EXCLUDEFROMCAPTURE:
            self.apply_protection(True, announce=False)

    def verify_taskbar(self):
        """Tk puede reponer el estilo al tocar la ventana; lo revalidamos.

        Si hace falta corregirlo con la ventana ya visible, el ciclo
        withdraw/deiconify es obligatorio para que la barra de tareas se entere.
        """
        if self.hidden or taskbar_style_applied(self.hwnd):
            return
        hide_from_taskbar(self.hwnd)
        self.root.withdraw()
        self.root.deiconify()
        self.verify_protection()

    def apply_topmost(self, enabled):
        self.root.attributes("-topmost", bool(enabled))
        self.config["topmost"] = bool(enabled)

    # -- bucles --

    def poll_obs(self):
        running = obs_is_running()
        if running is None:
            self.obs_label.config(text="OBS: no se ha podido comprobar",
                                  fg="#7b8494")
        elif running:
            self.obs_label.config(text="OBS: en ejecucion", fg="#88c0d0")
        else:
            self.obs_label.config(text="OBS: cerrado", fg="#7b8494")

        # Al arrancar OBS reafirmamos la proteccion antes de que capture nada.
        if running and self.last_obs_state is not True:
            self.verify_protection()

        self.verify_taskbar()

        # Plan B para equipos sin WDA_EXCLUDEFROMCAPTURE: quitar la ventana de
        # la pantalla mientras OBS corre. Preferible a un rectangulo negro.
        if self.auto_hide and self.protected_var.get():
            if running and not self.hidden:
                self.root.withdraw()
                self.hidden = True
                self.auto_hidden = True
            elif not running and self.auto_hidden:
                self.root.deiconify()
                self.hidden = False
                self.auto_hidden = False

        self.last_obs_state = running

        self.root.after(2000, self.poll_obs)

    def poll_hotkey(self):
        """Ctrl+Alt+H global. Solo consulta esas tres teclas concretas."""
        down = (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000
                and user32.GetAsyncKeyState(VK_MENU) & 0x8000
                and user32.GetAsyncKeyState(VK_H) & 0x8000)
        if down and not self.hotkey_was_down:
            self.toggle_visibility()
        self.hotkey_was_down = bool(down)
        self.root.after(120, self.poll_hotkey)

    def toggle_visibility(self):
        if self.hidden:
            hide_from_taskbar(self.hwnd)
            self.root.deiconify()
            self.hidden = False
            self.auto_hidden = False
            self.verify_protection()
        else:
            self.root.withdraw()
            self.hidden = True
            self.auto_hidden = False

    # -- acciones --

    def flash(self, message):
        self.status_label.config(text=message)
        self.root.after(2500,
                        lambda: self.status_label.config(text=HINT))

    def save(self):
        if save_document(self.serialize()):
            self.notes.edit_modified(False)
            self.flash("Guardado en %s" % state_dir())  # el USB si es portable
        else:
            self.flash("No se ha podido guardar")
        return "break"

    def quit(self):
        self.stop_ollama()
        save_document(self.serialize())
        self.config["geometry"] = self.root.geometry()
        save_config(self.config)
        self.root.destroy()


def main():
    try:
        root = tk.Tk()
        GhostPanel(root)
        root.mainloop()
    except Exception as error:
        log_error("main", error)
        try:
            messagebox.showerror(
                APP_NAME,
                "GhostPanel no pudo arrancar:\n\n%s\n\n"
                "Los detalles estan en:\n%s" % (error, LOG_PATH))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
