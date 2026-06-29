from moviepy.video.compositing import CompositeVideoClip
import os
import json
import datetime
import time
import argparse
import random
import re
import glob
import asyncio
import edge_tts
from io import BytesIO
from openai import OpenAI
import numpy as np
import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont
import requests
import base64

# --- PARCHE DE COMPATIBILIDAD ---
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

from moviepy.editor import *
import mysql.connector

# Cargar credenciales ocultas
with open('credentials.json', 'r', encoding='utf-8') as f:
    creds = json.load(f)

# ------------------------------------------------------------
# 1. CONFIGURACIÓN DE ZHIPU AI / BIGMODEL (texto e imágenes)
# ------------------------------------------------------------
ZHIPU_API_KEY = creds.get("zhipu_api_key")
if not ZHIPU_API_KEY:
    raise ValueError("❌ Falta la API key de Zhipu (zhipu_api_key) en credentials.json")

ZHIPU_BASE_URL = "https://api.z.ai/api/paas/v4/"

# Cliente compatible con OpenAI SDK para chat (guion + SEO)
client_zhipu = OpenAI(api_key=ZHIPU_API_KEY, base_url=ZHIPU_BASE_URL)

# Modelos de Zhipu/BigModel
ZHIPU_CHAT_MODEL = "glm-4.6"          # Modelo de texto para guion y SEO
ZHIPU_IMAGE_MODEL = "cogview-4-250304"  # Modelo de imágenes (CogView)

# Endpoint directo de imágenes (la SDK de OpenAI no siempre expone bien
# parámetros propios de Zhipu como "size", así que usamos requests directo)
ZHIPU_IMAGES_URL = "https://api.z.ai/api/paas/v4/images/generations"

# ------------------------------------------------------------
# 2. CONFIGURACIÓN GENERAL
# ------------------------------------------------------------
CARPETA_SALIDA_BASE = "PRODUCCION_TORAH"
RUTA_OUTRO = "ASSETS/VIDEO/outro.mp4"
ARCHIVO_MEMORIA = "memoria_progreso.json"
AMAZON_LINK = "https://amzn.to/4qRzgBC"
PATREON_LINK = "https://patreon.com/TorahDiaria"
ORDEN_TORAH = ["Bereshit", "Shemot", "Vayikra", "Bamidbar", "Devarim"]
VERSICULOS_POR_BLOQUE = 5  # Cantidad de versículos que se procesan en cada short

DB_CONFIG = {
    # Por defecto usa 'localhost' (correr el script directo en tu PC).
    # Si corres dentro de Docker y necesitas llegar al MySQL del host,
    # define la variable de entorno DB_HOST=host.docker.internal
    # (en Linux agrega --add-host=host.docker.internal:host-gateway al docker run).
    'host': os.environ.get('DB_HOST', 'localhost'),
    'user': 'root',
    'password': creds.get("db_password"),
    'database': 'torah_db'
}

# ------------------------------------------------------------
# 3. FUNCIONES AUXILIARES DE ZHIPU (texto)
# ------------------------------------------------------------
def call_zhipu(prompt, temperature=0.7):
    """Función genérica para consultar el modelo de chat de Zhipu (GLM)."""
    try:
        response = client_zhipu.chat.completions.create(
            model=ZHIPU_CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ Error en llamada a Zhipu (chat): {e}")
        return None


def limpiar_json(raw_text):
    """Extrae JSON de la respuesta de Zhipu (quita markdown)."""
    raw = (raw_text or "").replace("```json", "").replace("```", "").strip()
    return raw

# ------------------------------------------------------------
# 4. GESTIÓN DE MEMORIA Y BASE DE DATOS (sin cambios)
# ------------------------------------------------------------
def gestionar_progreso():
    if not os.path.exists(ARCHIVO_MEMORIA):
        return {"ultimo_libro": "Bereshit", "ultimo_capitulo": 1, "ultimo_versiculo": 0}
    with open(ARCHIVO_MEMORIA, 'r', encoding='utf-8') as f:
        return json.load(f)


def actualizar_memoria(libro, capitulo, versiculo_fin):
    print(f"💾 Guardando progreso: {libro} {capitulo}:{versiculo_fin}")
    datos = {
        "ultimo_libro": libro,
        "ultimo_capitulo": capitulo,
        "ultimo_versiculo": versiculo_fin,
        "ultima_fecha": str(datetime.datetime.now())
    }
    with open(ARCHIVO_MEMORIA, 'w', encoding='utf-8') as f:
        json.dump(datos, f, indent=4, ensure_ascii=False)


def obtener_texto_mysql(memoria):
    print(f"🔌 Consultando DB (Bloque de {VERSICULOS_POR_BLOQUE} versículos)...")
    conn = None
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)

        libro = memoria['ultimo_libro']
        cap = memoria['ultimo_capitulo']
        ver = memoria['ultimo_versiculo']

        sql = (
            "SELECT book_name_es, chapter, verse, spanish_text "
            "FROM torah_books "
            "WHERE book_name_es = %s AND chapter = %s AND verse > %s "
            f"ORDER BY verse ASC LIMIT {VERSICULOS_POR_BLOQUE}"
        )
        cursor.execute(sql, (libro, cap, ver))
        resultados = cursor.fetchall()

        if len(resultados) < VERSICULOS_POR_BLOQUE:
            faltantes = VERSICULOS_POR_BLOQUE - len(resultados)
            cap_sig = cap + 1
            sql_next = (
                "SELECT book_name_es, chapter, verse, spanish_text "
                "FROM torah_books "
                "WHERE book_name_es = %s AND chapter = %s "
                f"ORDER BY verse ASC LIMIT {faltantes}"
            )
            cursor.execute(sql_next, (libro, cap_sig))
            res_cap_sig = cursor.fetchall()
            resultados.extend(res_cap_sig)

        if not resultados:
            try:
                idx = ORDEN_TORAH.index(libro)
                if idx + 1 < len(ORDEN_TORAH):
                    nuevo_libro = ORDEN_TORAH[idx + 1]
                    print(f"🌟 ¡FIN DE LIBRO! Saltando automáticamente a {nuevo_libro}...")
                    sql_book_sig = (
                        "SELECT book_name_es, chapter, verse, spanish_text "
                        "FROM torah_books "
                        "WHERE book_name_es = %s AND chapter = 1 "
                        f"ORDER BY verse ASC LIMIT {VERSICULOS_POR_BLOQUE}"
                    )
                    cursor.execute(sql_book_sig, (nuevo_libro,))
                    resultados = cursor.fetchall()
                else:
                    print("🏁 ¡Felicidades! Toda la Torah ha sido procesada.")
                    exit()
            except ValueError:
                print(f"❌ Error: Libro {libro} no está en ORDEN_TORAH.")
                exit()

        if not resultados:
            print("❌ Error crítico: No hay datos en DB.")
            exit()

        primer, ultimo = resultados[0], resultados[-1]
        texto = " ".join([r['spanish_text'] for r in resultados])
        ref = f"{primer['book_name_es']} {primer['chapter']}:{primer['verse']}-{ultimo['verse']}"

        return {
            "referencia_texto": ref,
            "libro": ultimo['book_name_es'],
            "capitulo": ultimo['chapter'],
            "versiculo_inicio": primer['verse'],
            "versiculo_fin": ultimo['verse'],
            "contenido_completo": texto
        }
    except Exception as err:
        print(f"❌ Error MySQL: {err}")
        exit()
    finally:
        if conn:
            conn.close()

# ------------------------------------------------------------
# 5. UTILIDADES VISUALES (sin cambios)
# ------------------------------------------------------------
def zoom_in_effect(clip, zoom_ratio=0.04):
    def effect(get_frame, t):
        img = PIL.Image.fromarray(get_frame(t))
        base_size = img.size
        new_size = [
            int(base_size[0] * (1 + (zoom_ratio * t))),
            int(base_size[1] * (1 + (zoom_ratio * t)))
        ]
        img = img.resize(new_size, PIL.Image.LANCZOS)
        x = (new_size[0] - base_size[0]) // 2
        y = (new_size[1] - base_size[1]) // 2
        return np.array(img.crop([x, y, x + base_size[0], y + base_size[1]]))
    return clip.fl(effect)


def limpiar_titulo(texto):
    if not texto:
        return "Torah Diaria"
    limpio = texto.replace("\n", " ").replace("\r", " ")
    limpio = re.sub(r"[^\w\s\-\.,:;¡!¿?()áéíóúÁÉÍÓÚñÑ]", "", limpio, flags=re.UNICODE)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio[:100] if limpio else "Torah Diaria"


# Rutas conocidas de fuentes en negrita, en distintos sistemas. Se prueban
# en orden hasta encontrar una que exista. Cubre Windows, Linux (Docker) y Mac.
RUTAS_FUENTE_BOLD = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",  # Linux/Docker
    "C:/Windows/Fonts/arialbd.ttf",                                   # Windows (Arial Bold)
    "C:/Windows/Fonts/segoeuib.ttf",                                  # Windows (Segoe UI Bold)
    "C:/Windows/Fonts/calibrib.ttf",                                  # Windows (Calibri Bold)
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",              # macOS
]

_fuente_valida_cache = {"ruta": None, "buscada": False}


def obtener_fuente(fontsize):
    """
    Devuelve una fuente TrueType en negrita del tamaño pedido, probando
    varias rutas conocidas según el sistema operativo. Si ninguna ruta
    existe, usa la fuente por defecto de PIL pero pidiendo el tamaño
    explícitamente (soportado en Pillow >= 10.1), para no terminar con
    texto microscópico de tamaño fijo.
    """
    if not _fuente_valida_cache["buscada"]:
        for ruta in RUTAS_FUENTE_BOLD:
            if os.path.exists(ruta):
                _fuente_valida_cache["ruta"] = ruta
                break
        _fuente_valida_cache["buscada"] = True
        if _fuente_valida_cache["ruta"]:
            print(f"  🔎 Fuente encontrada para textos del video: {_fuente_valida_cache['ruta']}")
        else:
            print("  ⚠️ No se encontró ninguna fuente TrueType conocida. Usando fuente por defecto de PIL.")

    ruta = _fuente_valida_cache["ruta"]
    if ruta:
        try:
            return PIL.ImageFont.truetype(ruta, fontsize)
        except Exception:
            pass

    # Fallback final: fuente por defecto de PIL, pidiendo tamaño explícito
    # (Pillow >= 10.1). En versiones más viejas el parámetro size se ignora
    # y siempre se obtiene una fuente pequeña de tamaño fijo.
    try:
        return PIL.ImageFont.load_default(size=fontsize)
    except TypeError:
        return PIL.ImageFont.load_default()


def acortar_para_thumbnail(titulo, max_chars=55):
    """
    Los títulos de SEO suelen ser largos (buenos para YouTube, malos para
    una miniatura). Esta función corta el título a la primera frase o a
    max_chars, lo que sea más corto, intentando no partir una palabra.
    """
    titulo = titulo.strip()
    # Si el título tiene separador de subtítulo (-, :, |), usar solo la primera parte
    for sep in [" - ", ": ", " | "]:
        if sep in titulo:
            titulo = titulo.split(sep)[0].strip()
            break
    if len(titulo) <= max_chars:
        return titulo
    recorte = titulo[:max_chars]
    if " " in recorte:
        recorte = recorte.rsplit(" ", 1)[0]
    return recorte.rstrip(",.;:") + "..."


def envolver_texto_por_ancho(draw, texto, fnt, ancho_max_px):
    """
    Envuelve texto en líneas midiendo el ancho REAL de cada palabra con la
    fuente dada (en vez de estimar por cantidad de caracteres, que falla
    con fuentes Bold/anchas). Garantiza que ninguna línea exceda ancho_max_px,
    salvo que una sola palabra ya sea más ancha que el límite.
    """
    palabras = texto.split()
    if not palabras:
        return [texto]

    lineas = []
    linea_actual = palabras[0]
    for palabra in palabras[1:]:
        candidato = f"{linea_actual} {palabra}"
        ancho_candidato = draw.textbbox((0, 0), candidato, font=fnt)[2]
        if ancho_candidato <= ancho_max_px:
            linea_actual = candidato
        else:
            lineas.append(linea_actual)
            linea_actual = palabra
    lineas.append(linea_actual)
    return lineas


def generar_miniatura(ruta_base, titulo, ruta_out, formato="16:9"):
    try:
        if not os.path.exists(ruta_base):
            return
        titulo = limpiar_titulo(titulo)
        titulo = acortar_para_thumbnail(titulo, max_chars=45)
        img = PIL.Image.open(ruta_base).convert("RGBA")
        tw, th = (1280, 720) if formato == "16:9" else (720, 1280)
        ratio_img = img.width / img.height
        ratio_tgt = tw / th
        if ratio_img > ratio_tgt:
            new_h = th
            new_w = int(new_h * ratio_img)
        else:
            new_w = tw
            new_h = int(new_w / ratio_img)
        img = img.resize((new_w, new_h), PIL.Image.LANCZOS)
        x = (new_w - tw) / 2
        y = (new_h - th) / 2
        img = img.crop((x, y, x + tw, y + th))
        overlay = PIL.Image.new('RGBA', img.size, (0, 0, 0, 110))
        img = PIL.Image.alpha_composite(img, overlay)
        draw = PIL.ImageDraw.Draw(img)

        # Tamaño de fuente GRANDE por defecto. Si el título es muy largo y no
        # cabe ni en 5 líneas, se reduce progresivamente hasta que entre.
        fontsize_inicial = 130 if formato == "9:16" else 130
        fontsize_minimo = 60
        max_lineas = 5
        ancho_objetivo_px = tw * 0.88  # margen lateral del 6% por lado

        fontsize = fontsize_inicial
        lineas = [titulo]
        fnt = None

        while fontsize >= fontsize_minimo:
            fnt = obtener_fuente(fontsize)

            lineas = envolver_texto_por_ancho(draw, titulo, fnt, ancho_objetivo_px)

            ancho_max_linea = max(
                (draw.textbbox((0, 0), linea, font=fnt)[2] for linea in lineas),
                default=0
            )

            if len(lineas) <= max_lineas and ancho_max_linea <= ancho_objetivo_px:
                break
            fontsize -= 6

        alturas = []
        for linea in lineas:
            bbox = draw.textbbox((0, 0), linea, font=fnt)
            alturas.append(bbox[3] - bbox[1])
        espacio_lineas = int(fontsize * 0.25)
        alto_total = sum(alturas) + espacio_lineas * (len(lineas) - 1)

        yt = (th - alto_total) / 2
        for i, linea in enumerate(lineas):
            bbox = draw.textbbox((0, 0), linea, font=fnt)
            w_t = bbox[2] - bbox[0]
            xt = (tw - w_t) / 2
            # Contorno más grueso para que se lea bien sobre cualquier fondo
            grosor = max(int(fontsize * 0.04), 2)
            for dx in range(-grosor, grosor + 1):
                for dy in range(-grosor, grosor + 1):
                    if dx != 0 or dy != 0:
                        draw.text((xt + dx, yt + dy), linea, font=fnt, fill="black")
            draw.text((xt, yt), linea, font=fnt, fill="yellow")
            yt += alturas[i] + espacio_lineas

        img.convert("RGB").save(ruta_out, "JPEG", quality=90)
    except Exception as e:
        print(f"⚠️ Error miniatura: {e}")


def crear_clip_texto_pil(texto, size, fontsize, color="white", color_borde="black",
                          ancho_max_px=None):
    """
    Crea una imagen RGBA con texto centrado y envuelto en varias líneas,
    usando PIL directamente (no depende de ImageMagick, a diferencia de
    TextClip de MoviePy). El envuelto de línea mide el ancho REAL de cada
    palabra (no estima por cantidad de caracteres), evitando que el texto
    salga desproporcionadamente pequeño o mal partido con fuentes Bold.
    Devuelve un array numpy (H, W, 4) listo para separar en RGB + máscara alpha.
    """
    if ancho_max_px is None:
        ancho_max_px = size[0] * 0.88  # margen lateral del 6% por lado

    img = PIL.Image.new("RGBA", size, (0, 0, 0, 0))
    draw = PIL.ImageDraw.Draw(img)
    fnt = obtener_fuente(fontsize)

    lineas = envolver_texto_por_ancho(draw, texto, fnt, ancho_max_px)

    # Calcular alto total para centrar verticalmente el bloque de texto
    alturas = []
    for linea in lineas:
        bbox = draw.textbbox((0, 0), linea, font=fnt)
        alturas.append(bbox[3] - bbox[1])
    espacio_lineas = 10
    alto_total = sum(alturas) + espacio_lineas * (len(lineas) - 1)

    y = (size[1] - alto_total) / 2
    for i, linea in enumerate(lineas):
        bbox = draw.textbbox((0, 0), linea, font=fnt)
        w_t = bbox[2] - bbox[0]
        x = (size[0] - w_t) / 2
        # Borde (contorno) para legibilidad sobre cualquier fondo
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                if dx != 0 or dy != 0:
                    draw.text((x + dx, y + dy), linea, font=fnt, fill=color_borde)
        draw.text((x, y), linea, font=fnt, fill=color)
        y += alturas[i] + espacio_lineas

    return np.array(img)


def imageclip_con_alpha(arr_rgba, duracion):
    """
    Construye un ImageClip con transparencia real a partir de un array RGBA,
    separando explícitamente el canal RGB y usándolo junto a una máscara
    (ImageClip con ismask=True). Este método es el más confiable en
    MoviePy 1.0.3 -- pasar transparent=True directamente puede no aplicar
    bien la máscara según la versión de Pillow/numpy instalada.
    """
    rgb = arr_rgba[:, :, :3]
    alpha = arr_rgba[:, :, 3] / 255.0  # Máscara en rango 0.0-1.0

    clip_rgb = ImageClip(rgb).set_duration(duracion)
    mask_clip = ImageClip(alpha, ismask=True).set_duration(duracion)
    return clip_rgb.set_mask(mask_clip)


def crear_clip_referencia(referencia_texto, duracion=3.0, size=(1080, 1920)):
    """
    Crea el clip de texto con la referencia del pasaje (ej. 'Vayikra 14:26-35')
    que se muestra en los primeros segundos del video.
    """
    arr = crear_clip_texto_pil(
        referencia_texto, size=size, fontsize=95,
        color="white", color_borde="black", ancho_max_px=size[0] * 0.85
    )
    return imageclip_con_alpha(arr, duracion)


def crear_clips_subtitulos(frases, size=(1080, 1920)):
    """
    A partir de una lista de frases con tiempos {"texto","inicio","fin"},
    crea una lista de ImageClips posicionados en la parte inferior del video,
    cada uno mostrado durante su rango de tiempo correspondiente.
    """
    clips = []
    y_pos = int(size[1] * 0.74)  # Subtítulo en el tercio inferior
    for frase in frases:
        duracion = max(frase["fin"] - frase["inicio"], 0.2)
        arr = crear_clip_texto_pil(
            frase["texto"].upper(), size=(size[0], 320), fontsize=58,
            color="white", color_borde="black", ancho_max_px=size[0] * 0.88
        )
        clip = (
            imageclip_con_alpha(arr, duracion)
            .set_start(frase["inicio"])
            .set_position(("center", y_pos))
        )
        clips.append(clip)
    return clips



def preparar_outro(formato="9:16"):
    if not os.path.exists(RUTA_OUTRO):
        print(f"⚠️ Aviso: No se encontró outro en {RUTA_OUTRO}")
        return None
    try:
        clip = VideoFileClip(RUTA_OUTRO)
        if formato == "16:9":
            return clip.resize(height=1080).resize(width=1920)
        bg = (
            clip
            .resize(height=1920)
            .crop(x1=clip.w / 2 - 540, width=1080)
            .resize(0.1)
            .resize(10)
            .fl_image(lambda img: img * 0.4)
        )
        fg = clip.resize(width=1080)
        return CompositeVideoClip([bg, fg.set_pos("center")], size=(1080, 1920)).set_duration(clip.duration)
    except Exception as e:
        print(f"❌ Error procesando Outro: {e}")
        return None

# ------------------------------------------------------------
# 6. TEXTO A VOZ CON EDGE TTS (totalmente gratis)
# ------------------------------------------------------------
VOZ_NARRADOR = "es-MX-JorgeNeural"  # Voz masculina, español latino (México)

async def generar_voz_edge(texto, ruta_salida):
    """
    Genera MP3 usando Edge TTS (voz masculina en español latino) y devuelve
    además la lista de "word boundaries" (palabra, inicio, fin en segundos)
    que se usa luego para generar los subtítulos sincronizados.
    """
    try:
        # Reemplazar nombres divinos para evitar pronunciación extraña
        subs = {"YHWH": "Adonay", "Yhwh": "Adonay", "Yahveh": "Adonay", "Jehova": "Adonay"}
        for k, v in subs.items():
            texto = re.sub(rf'(?i)\b{k}\b', v, texto)

        communicate = edge_tts.Communicate(texto, VOZ_NARRADOR, boundary="WordBoundary")
        word_boundaries = []

        with open(ruta_salida, "wb") as audio_file:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_file.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # offset/duration vienen en unidades de 100 nanosegundos
                    inicio = chunk["offset"] / 10_000_000
                    duracion = chunk["duration"] / 10_000_000
                    word_boundaries.append({
                        "texto": chunk["text"],
                        "inicio": inicio,
                        "fin": inicio + duracion
                    })

        print(f"  ✅ Audio generado: {ruta_salida}")
        print(f"  🔎 Palabras con tiempo capturadas (word_boundaries): {len(word_boundaries)}")
        if word_boundaries:
            primera = word_boundaries[0]
            ultima = word_boundaries[-1]
            print(f"  🔎 Primera palabra: '{primera['texto']}' [{primera['inicio']:.2f}s-{primera['fin']:.2f}s]")
            print(f"  🔎 Última palabra: '{ultima['texto']}' [{ultima['inicio']:.2f}s-{ultima['fin']:.2f}s]")
        else:
            print("  ⚠️ No se recibió ningún WordBoundary del servicio. No habrá subtítulos en este short.")
        return word_boundaries
    except Exception as e:
        print(f"  ❌ Error generando voz con Edge TTS: {e}")
        # Crear un audio mudo de respaldo
        os.system(f"ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 1 -q:a 9 -acodec libmp3lame {ruta_salida} -y")
        return []


# Función síncrona para llamar desde el código principal.
# Devuelve la lista de word_boundaries para construir subtítulos.
def generar_voz(texto, ruta_salida):
    return asyncio.run(generar_voz_edge(texto, ruta_salida))


def agrupar_en_frases(word_boundaries, palabras_por_frase=7):
    """
    Agrupa los word_boundaries (palabra por palabra) en bloques de N palabras
    para mostrar como subtítulo estilo clásico (frase completa abajo).
    Devuelve una lista de dicts: {"texto": ..., "inicio": ..., "fin": ...}
    """
    frases = []
    bloque = []
    for wb in word_boundaries:
        bloque.append(wb)
        if len(bloque) >= palabras_por_frase:
            frases.append({
                "texto": " ".join(w["texto"] for w in bloque),
                "inicio": bloque[0]["inicio"],
                "fin": bloque[-1]["fin"]
            })
            bloque = []
    if bloque:
        frases.append({
            "texto": " ".join(w["texto"] for w in bloque),
            "inicio": bloque[0]["inicio"],
            "fin": bloque[-1]["fin"]
        })
    return frases

# ------------------------------------------------------------
# 7. IA: GUION Y METADATOS CON ZHIPU (GLM-4)
# ------------------------------------------------------------
def analizar_guion(texto_biblico):
    try:
        ESTILO_VISUAL = (
            "Cinematic Biblical Realism, 8k resolution, ultra-detailed, dramatic chiaroscuro lighting, "
            "epic atmosphere, anamorphic lens flares, authentic textures of ancient wool and stone. NO cartoon."
        )

        prompt = f"""
Actúa como experto editor de YouTube Shorts y Cineasta Bíblico.
Analiza este bloque de versículos: "{texto_biblico}"

Responde ÚNICAMENTE con JSON válido en esta estructura EXACTA:
{{
  "texto_completo_audio": "Un solo párrafo fluido que empieza con un GANCHO intrigante...",
  "segmentos_visuales": [
    {{"prompt_visual": "descripción épica en inglés para imagen 1"}},
    {{"prompt_visual": "descripción épica en inglés para imagen 2"}},
    {{"prompt_visual": "descripción épica en inglés para imagen 3"}},
    {{"prompt_visual": "descripción épica en inglés para imagen 4"}},
    {{"prompt_visual": "descripción épica en inglés para imagen 5"}}
  ]
}}

REGLAS OBLIGATORIAS:
1) "texto_completo_audio" debe ser UN SOLO párrafo narrativo continuo, SIN saltos de línea.
2) Debe empezar obligatoriamente con un Hook/Gancho intrigante en la primera frase.
3) No incluyas títulos, listas ni numeración dentro del texto.
4) Cada "prompt_visual" debe estar en inglés y debe incluir este estilo: {ESTILO_VISUAL}
5) Entrega exactamente 5 elementos en "segmentos_visuales".
6) No agregues campos extra.
"""
        respuesta = call_zhipu(prompt, temperature=0.7)
        if not respuesta:
            raise ValueError("Zhipu no devolvió respuesta")

        raw = limpiar_json(respuesta)
        data = json.loads(raw)

        texto_completo_audio = str(data.get("texto_completo_audio", "")).replace("\n", " ").strip()
        segmentos_visuales = data.get("segmentos_visuales", [])

        if not texto_completo_audio:
            raise ValueError("Zhipu devolvió texto_completo_audio vacío")
        if not isinstance(segmentos_visuales, list) or len(segmentos_visuales) == 0:
            raise ValueError("Zhipu devolvió segmentos_visuales inválido")

        segmentos_limpios = []
        for s in segmentos_visuales:
            pv = ""
            if isinstance(s, dict):
                pv = str(s.get("prompt_visual", "")).strip()
            if pv:
                segmentos_limpios.append({"prompt_visual": pv})

        if not segmentos_limpios:
            raise ValueError("No hay prompts visuales válidos")

        return {
            "texto_completo_audio": texto_completo_audio,
            "segmentos_visuales": segmentos_limpios
        }
    except Exception as e:
        print(f"⚠️ Error en analizar_guion, usando fallback: {e}")
        return {
            "texto_completo_audio": (
                "Gancho: lo que estás a punto de escuchar cambia cómo entendemos este pasaje. "
                f"{texto_biblico[:900]}"
            ).replace("\n", " "),
            "segmentos_visuales": [
                {"prompt_visual": "Epic biblical mystery, ancient scrolls glowing in candlelight, cinematic realism, 8k"},
                {"prompt_visual": "Ancient desert camp at dawn, dramatic sky, biblical realism, 8k"},
                {"prompt_visual": "Hebrew scribes writing sacred text, intense chiaroscuro, 8k"},
                {"prompt_visual": "Prophetic figure on mountain ridge, wind and dust, epic cinematic shot, 8k"},
                {"prompt_visual": "Wide shot of promised land horizon, golden light, cinematic biblical realism, 8k"}
            ]
        }


def generar_metadata_viral(texto_biblico, referencia, es_compilacion=False):
    print("📢 Generando SEO con Zhipu (GLM-4)...")
    base_desc = (
        f"\n👇 APÓYANOS & COMUNIDAD 👇"
        f"\n🙏 Patreon: {PATREON_LINK}"
        f"\n🛒 Documentos: {AMAZON_LINK}"
        f"\n📖 Lectura de hoy: {referencia}"
        f"\n#Torah #Biblia"
    )

    if es_compilacion:
        t = f"TORAH SEMANAL: {referencia}"
        titulo = (t[:97] + "...") if len(t) > 100 else t
        return {
            "titulo": limpiar_titulo(titulo),
            "descripcion": f"Compilación de estudios semanales.\n{base_desc}",
            "tags": ["#Torah", "#Compilacion"]
        }

    try:
        prompt = (
            "Actúa como experto YouTube SEO. Genera solo un JSON válido con claves exactas: "
            "titulo (string), descripcion (string), tags (array de hashtags tipo #Torah). "
            f"Tema: '{texto_biblico}'. Referencia: '{referencia}'. "
            "Sin markdown, sin texto extra."
        )
        respuesta = call_zhipu(prompt, temperature=0.7)
        if not respuesta:
            raise ValueError("Zhipu no devolvió respuesta")

        raw = limpiar_json(respuesta)
        data = json.loads(raw)

        titulo = limpiar_titulo(str(data.get('titulo', f"Lectura: {referencia}")))
        if len(titulo) > 100:
            titulo = titulo[:97] + "..."

        descripcion = str(data.get('descripcion', '')).strip()
        descripcion = f"{descripcion}\n{base_desc}".strip()

        tags = data.get('tags', ["#Torah", "#Biblia"])
        if not isinstance(tags, list):
            tags = ["#Torah", "#Biblia"]
        tags = [str(t).strip() for t in tags if str(t).strip()]
        tags = [t if t.startswith("#") else f"#{t}" for t in tags]

        return {
            "titulo": titulo,
            "descripcion": descripcion,
            "tags": tags or ["#Torah", "#Biblia"]
        }
    except Exception as e:
        print(f"⚠️ Fallo SEO IA: {e}")
        return {
            "titulo": limpiar_titulo(f"Lectura: {referencia}"[:100]),
            "descripcion": f"{texto_biblico}\n{base_desc}",
            "tags": ["#Torah", "#Biblia"]
        }

# ------------------------------------------------------------
# 8. IA: GENERACIÓN DE IMÁGENES CON ZHIPU (CogView)
# ------------------------------------------------------------
def crear_imagen_backup(ruta_salida):
    try:
        print("  ⚠️ Creando imagen de respaldo (Backup)...")
        img = PIL.Image.new('RGB', (1080, 1920), color='#0f172a')
        try:
            draw = PIL.ImageDraw.Draw(img)
            draw.text((100, 900), "TorahDiaria", fill="white")
        except Exception:
            pass
        img.save(ruta_salida)
    except Exception as e:
        print(f"  ❌ Error fatal creando backup: {e}")


def generar_imagen_zhipu(prompt, ruta_salida):
    """Genera imagen usando CogView (Zhipu AI / BigModel)."""
    print(f"  🎨 Generando imagen con Zhipu CogView: {prompt[:60]}...")

    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": ZHIPU_IMAGE_MODEL,
        "prompt": prompt,
        # CogView exige que ancho y alto sean múltiplos de 16 (1080 no lo es).
        # Usamos 1072x1904 (el 9:16 más grande válido) y luego reescalamos
        # a 1080x1920 al guardar la imagen.
        "size": "1072x1904"
    }

    max_intentos = 5
    espera_base = 20  # segundos

    for intento in range(max_intentos):
        try:
            response = requests.post(
                ZHIPU_IMAGES_URL, headers=headers, json=payload, timeout=120
            )
            if response.status_code == 200:
                data = response.json()
                items = data.get("data", [])
                if not items:
                    raise ValueError("Respuesta sin datos de imagen")

                item = items[0]
                img_bytes = None

                # CogView puede devolver una URL o el contenido en base64,
                # según el modelo/endpoint. Soportamos ambos casos.
                if item.get("url"):
                    img_resp = requests.get(item["url"], timeout=60)
                    img_resp.raise_for_status()
                    img_bytes = img_resp.content
                elif item.get("b64_json"):
                    img_bytes = base64.b64decode(item["b64_json"])
                else:
                    raise ValueError("Formato de respuesta de imagen no reconocido")

                img = PIL.Image.open(BytesIO(img_bytes))
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img = img.resize((1080, 1920), PIL.Image.LANCZOS)
                img.save(ruta_salida, "JPEG", quality=90)
                print(f"  ✅ Imagen guardada: {ruta_salida}")
                return True

            elif response.status_code == 429:
                espera = espera_base * (intento + 1)  # 20s, 40s, 60s, 80s, 100s
                print(f"  ⏳ Límite de tasa alcanzado... esperando {espera}s (Intento {intento + 1}/{max_intentos})")
                time.sleep(espera)
            else:
                print(f"  ❌ Error Zhipu (Intento {intento + 1}): {response.status_code} - {response.text[:200]}")
                time.sleep(5)
        except Exception as e:
            print(f"  ❌ Error conexión Zhipu (Intento {intento + 1}): {e}")
            time.sleep(5)

    print("  ❌ FRACASO: No se pudo generar la imagen.")
    return False

# ------------------------------------------------------------
# 9. ENSAMBLAJE DEL SHORT (9:16)
# ------------------------------------------------------------
def crear_short(guion, referencia, carpeta):
    texto_completo_audio = guion.get("texto_completo_audio", "").strip()
    segmentos_visuales = guion.get("segmentos_visuales", [])

    if not texto_completo_audio:
        texto_completo_audio = f"Gancho: descubre el mensaje oculto de {referencia}."
    if not segmentos_visuales:
        segmentos_visuales = [{"prompt_visual": "Epic biblical landscape, cinematic realism, 8k"} for _ in range(5)]

    audio_unico_path = os.path.join(carpeta, "audio_unico.mp3")
    print("  🎙️ Generando audio único completo...")
    word_boundaries = generar_voz(texto_completo_audio, audio_unico_path)

    if not os.path.exists(audio_unico_path):
        raise RuntimeError("No se pudo generar el audio único")

    audio_unico = AudioFileClip(audio_unico_path)
    duracion_total_audio = audio_unico.duration
    if duracion_total_audio <= 0:
        raise RuntimeError("Duración de audio inválida")

    # Agrupar las palabras (con sus tiempos reales del TTS) en frases cortas
    # para mostrarlas como subtítulos sincronizados.
    frases_subtitulo = agrupar_en_frases(word_boundaries, palabras_por_frase=7) if word_boundaries else []
    print(f"  🔎 Frases de subtítulo generadas: {len(frases_subtitulo)}")

    num_segmentos = len(segmentos_visuales)
    duracion_por_imagen = duracion_total_audio / max(num_segmentos, 1)

    clips_core = []
    for i, seg in enumerate(segmentos_visuales):
        i_path = os.path.join(carpeta, f"img_{i}.jpg")
        prompt_visual = seg.get('prompt_visual', 'Epic biblical scene, cinematic realism, 8k')

        if i > 0:
            # Pequeña pausa entre cada solicitud de imagen para no saturar
            # el límite de tasa (rate limit) de la cuenta de Zhipu.
            time.sleep(5)

        exito = generar_imagen_zhipu(prompt_visual, i_path)  # Usando CogView (Zhipu)
        if not exito or not os.path.exists(i_path):
            crear_imagen_backup(i_path)

        img_clip = ImageClip(i_path).resize(height=1920)
        if img_clip.w > 1080:
            img_clip = img_clip.crop(x1=img_clip.w / 2 - 540, width=1080)
        elif img_clip.w < 1080:
            img_clip = img_clip.resize(width=1080)

        img_clip = zoom_in_effect(img_clip.set_duration(duracion_por_imagen), zoom_ratio=0.04)
        clips_core.append(img_clip)

    if not clips_core:
        raise RuntimeError("No se pudieron crear clips visuales")

    video_principal = concatenate_videoclips(clips_core, method="compose")
    video_principal = video_principal.set_audio(audio_unico)

    if video_principal.duration > duracion_total_audio:
        video_principal = video_principal.subclip(0, duracion_total_audio)
    elif video_principal.duration < duracion_total_audio:
        faltante = duracion_total_audio - video_principal.duration
        ultimo_frame = video_principal.to_ImageClip(t=video_principal.duration - 0.01).set_duration(faltante)
        video_principal = concatenate_videoclips([video_principal, ultimo_frame], method="compose").set_audio(audio_unico)

    # --- Ajustar velocidad ANTES de superponer texto, para no descalibrar
    # los tiempos de los subtítulos. Si hace falta acelerar, se recalculan
    # aquí mismo los tiempos de cada frase dividiendo por el factor. ---
    outro = preparar_outro("9:16")
    duracion_outro = outro.duration if outro else 0.0
    duracion_estim_total = video_principal.duration + duracion_outro

    factor_velocidad = 1.0
    if duracion_estim_total > 48.0:
        factor_velocidad = duracion_estim_total / 48.0
        print(f"  ⚠️ Duración total estimada {duracion_estim_total:.2f}s > 48.0s. Acelerando video y audio con factor={factor_velocidad:.6f}")
        # video_principal ya tiene el audio adjunto (set_audio se hizo antes),
        # así que speedx acelera ambos a la vez de forma consistente.
        video_principal = video_principal.fx(vfx.speedx, factor_velocidad)
        # Recalcular tiempos de las frases de subtítulo para que sigan
        # sincronizadas tras la aceleración.
        for frase in frases_subtitulo:
            frase["inicio"] = frase["inicio"] / factor_velocidad
            frase["fin"] = frase["fin"] / factor_velocidad

    # Guardar el audio definitivo (ya acelerado si aplicó speedx) para
    # reasignarlo tras el CompositeVideoClip, que no copia el audio del
    # primer clip automáticamente.
    audio_definitivo = video_principal.audio

    # --- Capa de texto: referencia del versículo (primeros segundos) + subtítulos ---
    capas_overlay = [video_principal]

    duracion_referencia = min(3.0, video_principal.duration)
    clip_referencia = crear_clip_referencia(referencia, duracion=duracion_referencia)
    clip_referencia = clip_referencia.set_start(0).set_position(("center", "center"))
    capas_overlay.append(clip_referencia)

    if frases_subtitulo:
        capas_overlay.extend(crear_clips_subtitulos(frases_subtitulo))

    print(f"  🔎 Total de capas en el CompositeVideoClip (1 video + texto): {len(capas_overlay)}")
    video_principal = CompositeVideoClip(capas_overlay, size=(1080, 1920)).set_duration(video_principal.duration)
    video_principal = video_principal.set_audio(audio_definitivo)

    clips_finales = [video_principal]
    if outro:
        clips_finales.append(outro)

    final_video = concatenate_videoclips(clips_finales, method="compose")

    if final_video.duration > 48.0:
        final_video = final_video.subclip(0, 48.0)
    elif final_video.duration < 48.0:
        faltante = 48.0 - final_video.duration
        freeze = final_video.to_ImageClip(t=max(final_video.duration - 0.01, 0)).set_duration(faltante)
        final_video = concatenate_videoclips([final_video, freeze], method="compose")

    musica_folder = "ASSETS/MUSIC"
    if os.path.exists(musica_folder) and os.listdir(musica_folder):
        tracks = glob.glob(os.path.join(musica_folder, "*.mp3"))
        if tracks:
            track = AudioFileClip(random.choice(tracks))
            track = afx.audio_loop(track, duration=final_video.duration) if track.duration < final_video.duration else track.subclip(0, final_video.duration)
            track = track.volumex(0.15)
            if final_video.audio is not None:
                final_video = final_video.set_audio(CompositeAudioClip([final_video.audio, track]))
            else:
                final_video = final_video.set_audio(track)

    ruta_out = os.path.join(carpeta, "VIDEO_FINAL.mp4")
    final_video.write_videofile(ruta_out, fps=24, codec="libx264", audio_codec="aac")

    return ruta_out

# ------------------------------------------------------------
# 10. COMPILACIÓN (usa metadata generada con Zhipu)
# ------------------------------------------------------------
def compilar_videos(horas_atras):
    print("📚 Iniciando Compilación...")
    ahora, limite = time.time(), horas_atras * 3600
    carpetas, search_path = [], os.path.join(os.getcwd(), CARPETA_SALIDA_BASE)

    if not os.path.exists(search_path):
        return None

    for folder in os.listdir(search_path):
        full = os.path.join(search_path, folder)
        if os.path.isdir(full) and "COMPILACION" not in folder:
            mtime = os.path.getmtime(full)
            if (ahora - mtime) <= limite:
                vid = os.path.join(full, "VIDEO_FINAL.mp4")
                if os.path.exists(vid):
                    carpetas.append((mtime, full, vid))

    if not carpetas:
        return None

    carpetas.sort(key=lambda x: x[0])

    clips, refs = [], []
    for _, path, vid_path in carpetas:
        try:
            with open(os.path.join(path, "metadata.json"), 'r', encoding='utf-8') as f:
                refs.append(json.load(f).get('titulo', 'Parte'))
        except Exception:
            pass

        c = VideoFileClip(vid_path)
        bg = (
            c
            .resize(width=1920)
            .resize(0.05)
            .resize(width=1920)
            .crop(x1=0, y1=c.h / 2 - 540, width=1920, height=1080)
        )
        fg = c.resize(height=1000).set_pos("center")
        clips.append(CompositeVideoClip([bg, fg], size=(1920, 1080)))

    if len(clips) > 1:
        clips[-1] = clips[-1].crossfadein(1)

    outro = preparar_outro("16:9")
    if outro:
        clips.append(outro)

    video_final = concatenate_videoclips(clips, method="compose")

    ruta_c = os.path.join(CARPETA_SALIDA_BASE, f"COMPILACION_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}")
    os.makedirs(ruta_c, exist_ok=True)

    ruta_final = os.path.join(ruta_c, "VIDEO_FINAL.mp4")
    video_final.write_videofile(ruta_final, fps=24, codec="libx264", audio_codec="aac")

    with open(os.path.join(ruta_c, "metadata.json"), "w", encoding='utf-8') as f:
        json.dump(generar_metadata_viral("", " - ".join(refs[:3]), True), f, indent=4, ensure_ascii=False)

    try:
        PIL.Image.fromarray(video_final.get_frame(t=2)).convert('RGB').save(
            os.path.join(ruta_c, "thumbnail.jpg"),
            "JPEG",
            quality=90
        )
    except Exception:
        pass

    return ruta_final

# ------------------------------------------------------------
# 11. MAIN
# ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generador automático de Shorts y Compilaciones de la Torah")
    parser.add_argument("--mode", choices=["standard", "compile"], default="standard",
                         help="Modo de ejecución: 'standard' para generar un short diario, 'compile' para unir los recientes.")
    parser.add_argument("--hours", type=int, default=24,
                         help="Horas hacia atrás para compilar videos (usado solo en modo compile).")
    args = parser.parse_args()

    # --- MODO COMPILACIÓN ---
    if args.mode == "compile":
        print(f"🔄 Iniciando modo compilación (últimas {args.hours} horas)...")
        ruta = compilar_videos(args.hours)
        if ruta:
            print(f"✅ Compilación lista en: {ruta}")
        else:
            print("⚠️ No se encontraron videos recientes para compilar en el lapso indicado.")
        return

    # --- MODO STANDARD (Generación de Short) ---
    print("▶️ Iniciando generación de Short (Modo Standard)...")

    # 1. Recuperar el estado y extraer datos
    memoria = gestionar_progreso()
    info = obtener_texto_mysql(memoria)
    print(f"📜 Procesando bloque: {info['referencia_texto']}")

    # 2. Crear carpetas necesarias
    carpeta_nombre = info['referencia_texto'].replace(" ", "_").replace(":", "_")
    carpeta = os.path.join(CARPETA_SALIDA_BASE, carpeta_nombre)
    os.makedirs(carpeta, exist_ok=True)

    # 3. Analizar guion y obtener metadata con la IA
    guion = analizar_guion(info['contenido_completo'])
    meta = generar_metadata_viral(info['contenido_completo'], info['referencia_texto'])

    # 4. Limpiar y estructurar los metadatos (SEO)
    titulo = limpiar_titulo(meta.get('titulo', f"Lectura: {info['referencia_texto']}"))
    descripcion = str(meta.get('descripcion', '')).strip()
    tags = meta.get('tags', ["#Torah", "#Biblia"])
    if not isinstance(tags, list):
        tags = ["#Torah", "#Biblia"]
    tags = [str(t).strip() for t in tags if str(t).strip()]
    tags = [t if t.startswith("#") else f"#{t}" for t in tags]

    meta_limpio = {
        "titulo": titulo,
        "descripcion": descripcion,
        "tags": tags
    }

    # Guardar metadata en la carpeta
    with open(os.path.join(carpeta, "metadata.json"), "w", encoding='utf-8') as f:
        json.dump(meta_limpio, f, indent=4, ensure_ascii=False)

    # 5. Generar el video
    ruta_video = crear_short(guion, info['referencia_texto'], carpeta)

    # 6. Preparar archivo de texto listo para copiar a Metricool
    hashtags_formateados = " ".join(tags)
    metricool_path = os.path.join(carpeta, "METRICOOL_READY.txt")
    metricool_content = (
        f"TÍTULO: {titulo}\n\n"
        f"DESCRIPCIÓN:\n{descripcion}\n\n"
        f"HASHTAGS: {hashtags_formateados}\n"
    )
    with open(metricool_path, "w", encoding="utf-8") as f:
        f.write(metricool_content)

    # 7. Generar miniatura base si la imagen principal se guardó
    if os.path.exists(os.path.join(carpeta, "img_0.jpg")):
        generar_miniatura(
            os.path.join(carpeta, "img_0.jpg"),
            titulo,
            os.path.join(carpeta, "thumbnail.jpg"),
            formato="9:16"
        )

    # 8. Actualizar progreso para la próxima ejecución
    actualizar_memoria(info['libro'], info['capitulo'], info['versiculo_fin'])

    print(f"✅ ¡Short finalizado con éxito! Memoria actualizada.\n📁 Ubicación: {ruta_video}")


if __name__ == "__main__":
    main()
