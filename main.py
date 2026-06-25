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

ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"

# Cliente compatible con OpenAI SDK para chat (guion + SEO)
client_zhipu = OpenAI(api_key=ZHIPU_API_KEY, base_url=ZHIPU_BASE_URL)

# Modelos de Zhipu/BigModel
ZHIPU_CHAT_MODEL = "glm-4.6"          # Modelo de texto para guion y SEO
ZHIPU_IMAGE_MODEL = "cogview-4-250304"  # Modelo de imágenes (CogView)

# Endpoint directo de imágenes (la SDK de OpenAI no siempre expone bien
# parámetros propios de Zhipu como "size", así que usamos requests directo)
ZHIPU_IMAGES_URL = "https://open.bigmodel.cn/api/paas/v4/images/generations"

# ------------------------------------------------------------
# 2. CONFIGURACIÓN GENERAL
# ------------------------------------------------------------
CARPETA_SALIDA_BASE = "PRODUCCION_TORAH"
RUTA_OUTRO = "ASSETS/VIDEO/outro.mp4"
ARCHIVO_MEMORIA = "memoria_progreso.json"
AMAZON_LINK = "https://amzn.to/4qRzgBC"
PATREON_LINK = "https://patreon.com/TorahDiaria"
ORDEN_TORAH = ["Bereshit", "Shemot", "Vayikra", "Bamidbar", "Devarim"]

DB_CONFIG = {
    'host': 'host.docker.internal',
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
    print("🔌 Consultando DB (Bloque de 10 versículos)...")
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
            "ORDER BY verse ASC LIMIT 10"
        )
        cursor.execute(sql, (libro, cap, ver))
        resultados = cursor.fetchall()

        if len(resultados) < 10:
            faltantes = 10 - len(resultados)
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
                        "ORDER BY verse ASC LIMIT 10"
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


def generar_miniatura(ruta_base, titulo, ruta_out, formato="16:9"):
    try:
        if not os.path.exists(ruta_base):
            return
        titulo = limpiar_titulo(titulo)
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
        overlay = PIL.Image.new('RGBA', img.size, (0, 0, 0, 100))
        img = PIL.Image.alpha_composite(img, overlay)
        draw = PIL.ImageDraw.Draw(img)
        try:
            fnt = PIL.ImageFont.truetype(
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                60 if formato == "9:16" else 80
            )
        except Exception:
            fnt = PIL.ImageFont.load_default()
        w_t, h_t = draw.textsize(titulo, font=fnt) if hasattr(draw, 'textsize') else (300, 100)
        xt, yt = (tw - w_t) / 2, (th - h_t) / 2
        draw.text((xt - 3, yt - 3), titulo, font=fnt, fill="black")
        draw.text((xt, yt), titulo, font=fnt, fill="yellow")
        img.convert("RGB").save(ruta_out, "JPEG", quality=90)
    except Exception as e:
        print(f"⚠️ Error miniatura: {e}")


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
async def generar_voz_edge(texto, ruta_salida):
    """Genera MP3 usando Edge TTS (voz en español)."""
    try:
        # Reemplazar nombres divinos para evitar pronunciación extraña
        subs = {"YHWH": "Adonay", "Yhwh": "Adonay", "Yahveh": "Adonay", "Jehova": "Adonay"}
        for k, v in subs.items():
            texto = re.sub(rf'(?i)\b{k}\b', v, texto)

        communicate = edge_tts.Communicate(texto, "es-ES-ElviraNeural")  # Voz femenina clara
        await communicate.save(ruta_salida)
        print(f"  ✅ Audio generado: {ruta_salida}")
        return True
    except Exception as e:
        print(f"  ❌ Error generando voz con Edge TTS: {e}")
        # Crear un audio mudo de respaldo
        os.system(f"ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 1 -q:a 9 -acodec libmp3lame {ruta_salida} -y")
        return False


# Función síncrona para llamar desde el código principal
def generar_voz(texto, ruta_salida):
    asyncio.run(generar_voz_edge(texto, ruta_salida))

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
    {{"prompt_visual": "descripción épica en inglés para imagen 5"}},
    {{"prompt_visual": "descripción épica en inglés para imagen 6"}}
  ]
}}

REGLAS OBLIGATORIAS:
1) "texto_completo_audio" debe ser UN SOLO párrafo narrativo continuo, SIN saltos de línea.
2) Debe empezar obligatoriamente con un Hook/Gancho intrigante en la primera frase.
3) No incluyas títulos, listas ni numeración dentro del texto.
4) Cada "prompt_visual" debe estar en inglés y debe incluir este estilo: {ESTILO_VISUAL}
5) Entrega exactamente 6 elementos en "segmentos_visuales".
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
                {"prompt_visual": "Close-up of weathered hands holding parchment, ultra detailed, 8k"},
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
        "size": "1080x1920"  # Formato vertical 9:16 nativo para Shorts/Reels
    }

    for intento in range(3):
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
                print(f"  ⏳ Límite de tasa alcanzado... esperando 15s (Intento {intento + 1})")
                time.sleep(15)
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
        segmentos_visuales = [{"prompt_visual": "Epic biblical landscape, cinematic realism, 8k"} for _ in range(6)]

    audio_unico_path = os.path.join(carpeta, "audio_unico.mp3")
    print("  🎙️ Generando audio único completo...")
    generar_voz(texto_completo_audio, audio_unico_path)

    if not os.path.exists(audio_unico_path):
        raise RuntimeError("No se pudo generar el audio único")

    audio_unico = AudioFileClip(audio_unico_path)
    duracion_total_audio = audio_unico.duration
    if duracion_total_audio <= 0:
        raise RuntimeError("Duración de audio inválida")

    num_segmentos = len(segmentos_visuales)
    duracion_por_imagen = duracion_total_audio / max(num_segmentos, 1)

    clips_core = []
    for i, seg in enumerate(segmentos_visuales):
        i_path = os.path.join(carpeta, f"img_{i}.jpg")
        prompt_visual = seg.get('prompt_visual', 'Epic biblical scene, cinematic realism, 8k')

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

    outro = preparar_outro("9:16")
    duracion_outro = outro.duration if outro else 0.0

    clips_finales = [video_principal]
    if outro:
        clips_finales.append(outro)

    final_video = concatenate_videoclips(clips_finales, method="compose")

    duracion_total = video_principal.duration + duracion_outro
    if duracion_total > 48.0:
        factor = duracion_total / 48.0
        print(f"  ⚠️ Duración total {duracion_total:.2f}s > 48.0s. Aplicando speedx con factor={factor:.6f}")
        final_video = final_video.fx(vfx.speedx, factor)
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
