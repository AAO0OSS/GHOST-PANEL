# GhostPanel

Panel de notas que se ve en tu monitor pero **no aparece en las capturas de
pantalla ni en las grabaciones**: OBS, Zoom, Discord, Meet o la herramienta
Recorte de pantalla componen lo que hay detrás del panel, como si no estuviera.

Lleva notas de texto, imágenes con zoom y una pestaña de chat con una IA local.
Puede funcionar entero desde un USB, sin instalar nada en el ordenador donde se
enchufa.

---

## Cómo funciona

No es un truco de transparencias ni de ocultar la ventana cuando detecta que
estás grabando. Usa una función oficial de Windows,
[`SetWindowDisplayAffinity`](https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity),
con el valor `WDA_EXCLUDEFROMCAPTURE`: es el propio compositor de Windows (DWM)
el que excluye la ventana de cualquier captura, a nivel de sistema. Por eso
funciona con cualquier programa de grabación, incluidos los que aún no existen.

Hay un detalle que el programa no da por supuesto: **pide la invisibilidad y
después comprueba que Windows la haya concedido de verdad**, en vez de fiarse
del número de compilación. Si no la concede, no se conforma con
`WDA_MONITOR` —el modo antiguo, que deja un rectángulo negro flotando en la
grabación y delata que hay algo oculto—, sino que esconde la ventana mientras
OBS esté abierto y te avisa de que estás en ese modo.

Todo lo que el panel dibuja tiene que vivir **dentro de esa misma ventana**. La
exclusión de captura se aplica por ventana, así que el visor de imágenes y el
panel de ajustes son capas colocadas encima del propio panel, no ventanas
aparte. Una ventana nueva sí aparecería en la grabación.

---

## Requisitos

| | |
|---|---|
| Sistema | Windows 10 versión 2004 (compilación 19041) o superior |
| Python | Incluido en `python\` en la copia portable; sin ella, 3.11 o superior con tkinter |
| Pillow | Opcional pero recomendado: sin él solo admite PNG y GIF, y no se pueden pegar imágenes |
| PyMuPDF | Opcional. Solo si quieres insertar PDFs |
| Ollama | Opcional. Solo para la pestaña de chat |

En un Windows anterior a la 2004 el panel arranca igual, pero sin invisibilidad
real: pasa al modo de ocultarse solo mientras haya un OBS en marcha.

---

## Cómo se arranca

**Desde la copia portable (USB):** doble clic en `GhostPanel.vbs`. Ese, y solo
ese: si haces doble clic en `ghostpanel.pyw`, Windows lo abriría con el Python
**del ordenador anfitrión** —si es que tiene uno— en vez de con el del USB, y el
panel arrancaría cojo o no arrancaría. El `.vbs` fuerza el intérprete correcto y
lo lanza sin ninguna ventana de consola.

Si tu empresa bloquea los `.vbs` que vienen de un USB:

```
python\pythonw.exe ghostpanel.pyw
```

**Desde este repositorio:** aquí solo están las fuentes. El runtime que
acompaña al USB —el Python recortado, los binarios de Ollama y los modelos, casi
2 GB— no se versiona. Con un Python 3.11+ ya instalado:

```
pip install pillow
pythonw ghostpanel.pyw
```

### Reconstruir la copia portable

El `python\` del USB no es el paquete *embeddable* de python.org tal cual: ese
viene sin tkinter. Se armó a partir de él añadiendo, de una instalación normal
de Python 3.11:

- `Lib\` completa y `DLLs\`, con `_tkinter.pyd`, `tcl86t.dll` y `tk86t.dll`
- la carpeta `tcl\`
- Pillow en `Lib\site-packages\`
- y un `python311._pth` que incluya `Lib`, `Lib\site-packages`, `DLLs` y la
  línea `import site`, sin la cual no se cargarían los paquetes

Para el chat: `ollama\` con `ollama.exe` y su `lib\`, y los modelos descargados
en `modelos\` (`OLLAMA_MODELS=...\modelos ollama pull llama3.2`).

---

## Modo portable

Lo decide un archivo: **mientras exista `PORTABLE.txt` junto al script**, las
notas, las imágenes y la configuración se guardan en la carpeta `datos\` de al
lado. Si lo borras, el panel guarda en `%APPDATA%\GhostPanel` del ordenador
anfitrión.

Es explícito a propósito, para que una copia en el disco duro y otra en el USB
no se muevan las notas de sitio la una a la otra. Si el USB estuviera protegido
contra escritura, el panel no se queda colgado: usa `%APPDATA%` como último
recurso para poder seguir guardando.

---

## La interfaz

**Cabecera.** Un punto de color con el estado de la protección y el botón de
ajustes. Verde: invisible en capturas. Ámbar: protección desactivada, el panel
sale en la grabación. Rojo: tu Windows no admite la invisibilidad real y el
panel se esconderá solo mientras OBS esté abierto.

**Ajustes** (el engranaje). Invisible en capturas, siempre encima, ocultar el
cursor mientras esté sobre el panel, opacidad y modelo de Ollama. Se cierra con
`Esc`, con la ✕ o pinchando fuera.

**Opacidad.** Del 1 al 100 %, y se aplica en vivo. Al 1 % el panel es
prácticamente invisible pero sigue ahí y sigue recibiendo los clics, así que
`Ctrl+0` lo devuelve al 100 % si te pasas.

**Notas.** Texto e imágenes mezclados, con barra vertical que aparece sola
cuando hay algo fuera de la vista.

**Visor.** Doble clic sobre una imagen la abre a pantalla completa dentro del
panel, con zoom sin límite, barras horizontal y vertical, y arrastre con el
ratón.

---

## Atajos

| Atajo | Qué hace |
|---|---|
| `Ctrl+Alt+H` | Oculta y muestra el panel. Funciona aunque no tenga el foco |
| `Ctrl+S` | Guarda |
| `Ctrl+V` | Pega texto, o la imagen que haya en el portapapeles |
| `Ctrl+0` | Devuelve la opacidad al 100 % |
| `Ctrl+rueda` | Zoom sobre la imagen que esté bajo el puntero |
| Doble clic | Abre la imagen en el visor |
| Clic derecho | Menú de la imagen: tamaño, ajustar al panel, original, eliminar |
| `Esc` | Cierra el visor o los ajustes |

En el visor: rueda para el zoom, `+` y `-`, `0` para ajustar, arrastrar para
moverse.

---

## Imágenes

Se insertan desde el botón `Insertar` o pegando con `Ctrl+V`. Formatos: PNG y
GIF siempre; JPG, BMP y WEBP con Pillow; PDF con PyMuPDF, rasterizando sus
páginas (abrir un visor externo crearía una ventana sin proteger que OBS sí
grabaría).

Cada imagen se guarda una sola vez, con el nombre derivado de su hash, así que
insertar dos veces la misma no ocupa el doble. **Ninguna imagen puede superar el
ancho del panel**: el área de notas no se desplaza en horizontal, así que lo que
sobresaliera por la derecha sería inalcanzable. Para verla en detalle está el
visor.

---

## Chat con IA

La pestaña `Chat IA` habla con un [Ollama](https://ollama.com) local: gratis,
sin clave y privado, porque nada sale de tu equipo.

Si junto al script hay una carpeta `ollama\`, el propio panel levanta ese
servidor cuando hace falta y lo mata al cerrarse —a él y al proceso que ejecuta
el modelo—, leyendo los modelos de `modelos\`. Así el chat funciona también en
ordenadores que no tienen Ollama instalado. Si el ordenador ya tiene el suyo en
marcha, lo respeta y no lo toca al salir.

Medido en el USB 2.0 de la copia portable: **la primera respuesta tarda unos
150 s** porque hay que leer 1,9 GB del pendrive, y **las siguientes unos 4 s**.
Esa espera es una sola vez por sesión: el servidor arranca con
`OLLAMA_KEEP_ALIVE = -1`, así que el modelo no se descarga de la RAM a los cinco
minutos de no usarlo. Si vas a usar el chat en directo, manda un «hola» al
empezar.

El Ollama de la copia portable lleva el motor de CPU y el runner de Vulkan, que
da GPU en NVIDIA, AMD e Intel. No lleva CUDA ni ROCm porque ocupan 2,7 GB.

---

## Dónde se guardan tus cosas

En `datos\` (portable) o en `%APPDATA%\GhostPanel`:

| | |
|---|---|
| `document.json` | Las notas, como lista ordenada de texto e imágenes |
| `images\` | Las imágenes insertadas |
| `config.json` | Opciones, tamaño y posición de la ventana |
| `error.log` | Fallos, si los hay |

**Se guarda solo.** En el acto al insertar, redimensionar o borrar una imagen, y
1,5 segundos después de que dejes de teclear. Borrar una imagen la quita también
del disco en ese momento.

---

## Limitaciones conocidas

- **La posición de la ventana no se valida.** El panel se reabre donde estaba la
  última vez. Si esa posición era un monitor secundario y lo abres en un
  ordenador con una sola pantalla, quedará fuera del área visible, y como a
  propósito no sale ni en la barra de tareas ni en `Alt+Tab`, parecerá que no
  arranca. Se soluciona borrando `geometry` de `config.json`.
- **OBS graba el cursor del sistema** aunque la ventana sea invisible. Para eso
  está la opción «Ocultar el cursor encima».
- **Las notas se guardan en claro.** Cualquiera que abra el `document.json` las
  lee. No hay cifrado.
- **El guardado no es atómico**: si el USB se desconecta justo durante una
  escritura, el documento puede perderse. Y como el programa entero vive en el
  pendrive, desconectarlo con el panel abierto lo mata en el acto.
- **El área de notas no se desplaza en horizontal**, por el ajuste de línea del
  texto.

---

## Estructura

```
ghostpanel.pyw    la aplicación entera
GhostPanel.vbs    lanzador sin consola, sin rutas fijas
PORTABLE.txt      marcador: mientras exista, los datos van junto al script
LEEME.txt         guía corta para quien recibe el USB
```

Fuera del repositorio, solo en la copia del USB: `python\`, `ollama\`,
`modelos\` y `datos\`.
