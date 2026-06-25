# TorahIA 📖🎬

**TorahIA** es un generador automático de videos cortos (Shorts/Reels) sobre la Torá. Toma versículos almacenados en una base de datos MySQL, genera con IA un guion narrativo, ilustraciones, narración por voz (TTS) y metadatos SEO, y ensambla todo en un video vertical (9:16) listo para publicar en YouTube Shorts, TikTok o Instagram Reels. También puede compilar varios shorts recientes en un video horizontal (16:9) para YouTube.

## ✨ Características

- **Guion automático con IA**: convierte un bloque de versículos en un guion narrativo con gancho ("hook") inicial, listo para ser narrado.
- **Generación de imágenes con IA**: crea ilustraciones cinematográficas para cada segmento del guion.
- **Narración por voz (TTS)**: convierte el guion en audio en español usando Edge TTS (gratuito), sustituyendo nombres divinos por su pronunciación tradicional (ej. *YHWH → Adonay*).
- **Ensamblaje de video**: combina imágenes (con efecto de zoom cinematográfico), audio narrado, música de fondo y un outro, ajustando la duración total a un límite fijo (48s).
- **Miniaturas automáticas**: genera thumbnails con el título superpuesto sobre la primera imagen del video.
- **Metadatos SEO con IA**: genera título, descripción y hashtags optimizados para cada publicación, junto con un archivo de texto listo para pegar en herramientas de programación de contenido (ej. Metricool).
- **Memoria de progreso**: recuerda el último libro/capítulo/versículo procesado para continuar automáticamente en la siguiente ejecución, recorriendo los 5 libros de la Torá en orden.
- **Modo compilación**: une los videos generados en las últimas N horas en un solo video horizontal con transiciones.
- **Dockerizado**: listo para ejecutarse en un contenedor, ideal para automatizar con cron o un orquestador.

## 🧱 Arquitectura

```
MySQL (torah_db) ──► obtener_texto_mysql()
                          │
                          ▼
                 analizar_guion()  ──► IA de texto (guion + prompts visuales)
                          │
        ┌─────────────────┼─────────────────────┐
        ▼                                       ▼
generar_voz() (Edge TTS)              generar_imagen_*() (IA de imágenes)
        │                                       │
        └───────────────────┬───────────────────┘
                             ▼
                      crear_short()
                  (MoviePy: ensamblaje)
                             │
                             ▼
              VIDEO_FINAL.mp4 + thumbnail.jpg
                             │
                             ▼
         generar_metadata_viral() ──► IA de texto (SEO)
                             │
                             ▼
                  metadata.json + METRICOOL_READY.txt
```

## 📋 Requisitos previos

- Python 3.10+
- Docker (opcional, recomendado para producción)
- Una base de datos MySQL accesible con una tabla `torah_books` con, al menos, las columnas: `book_name_es`, `chapter`, `verse`, `spanish_text`.
- `ffmpeg` e `ImageMagick` instalados (ya incluidos en el `Dockerfile`).
- Una cuenta y API key de **Zhipu AI (BigModel)** — ver sección de configuración.

## ⚙️ Instalación

### Opción A: Local

```bash
git clone https://github.com/msantiago1044/torahia.git
cd torahia
python -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Opción B: Docker (recomendado)

```bash
docker build -t torahia .
docker run --rm \
  -v $(pwd)/credentials.json:/app/credentials.json \
  -v $(pwd)/ASSETS:/app/ASSETS \
  -v $(pwd)/PRODUCCION_TORAH:/app/PRODUCCION_TORAH \
  torahia
```

## 🔑 Configuración (`credentials.json`)

Crea un archivo `credentials.json` en la raíz del proyecto (este archivo **no debe subirse al repositorio**; agrégalo a `.gitignore`):

```json
{
  "zhipu_api_key": "TU_API_KEY_DE_ZHIPU_BIGMODEL",
  "db_password": "TU_PASSWORD_DE_MYSQL"
}
```

> Puedes obtener tu API key de Zhipu en [open.bigmodel.cn/usercenter/apikeys](https://open.bigmodel.cn/usercenter/apikeys).

### Variables de base de datos

Por defecto el proyecto asume:
- Host: `host.docker.internal` (para conectar desde el contenedor a un MySQL en tu máquina host)
- Usuario: `root`
- Base de datos: `torah_db`

Ajusta esto en el bloque `DB_CONFIG` de `main.py` según tu entorno.

## ▶️ Uso

### Generar un short diario

```bash
python main.py --mode standard
```

Esto:
1. Recupera el siguiente bloque de 10 versículos desde MySQL (continuando donde quedó la última ejecución).
2. Genera el guion y las imágenes con IA.
3. Crea el audio narrado.
4. Ensambla el video final (`VIDEO_FINAL.mp4`) y la miniatura.
5. Genera `metadata.json` y `METRICOOL_READY.txt` con título, descripción y hashtags.
6. Actualiza el progreso para la siguiente ejecución.

### Compilar videos recientes

```bash
python main.py --mode compile --hours 24
```

Une todos los shorts generados en las últimas 24 horas en un solo video horizontal (16:9), con transiciones y outro.

### Automatización con cron

```bash
# Generar un short todos los días a las 8:00 AM
0 8 * * * cd /ruta/a/torahia && python main.py --mode standard >> logs/torahia.log 2>&1

# Compilar los shorts de la semana cada domingo a las 9:00 PM
0 21 * * 0 cd /ruta/a/torahia && python main.py --mode compile --hours 168 >> logs/torahia.log 2>&1
```

## 📁 Estructura de salida

```
PRODUCCION_TORAH/
└── Bereshit_1_1-10/
    ├── audio_unico.mp3
    ├── img_0.jpg ... img_5.jpg
    ├── thumbnail.jpg
    ├── VIDEO_FINAL.mp4
    ├── metadata.json
    └── METRICOOL_READY.txt
```

## 🛠️ Stack técnico

| Componente | Tecnología |
|---|---|
| Lenguaje | Python 3.10 |
| Edición de video | MoviePy 1.0.3 |
| Texto a voz | Edge TTS |
| IA de texto (guion + SEO) | Zhipu AI — GLM-4 |
| IA de imágenes | Zhipu AI — CogView |
| Base de datos | MySQL |
| Imágenes/miniaturas | Pillow |
| Contenedor | Docker (python:3.10-slim) |

## 🗺️ Roadmap / Mejoras sugeridas

Ver la sección de **Mejoras propuestas** más abajo en este documento o en `CONTRIBUTING.md`.

## ⚠️ Avisos

- Este proyecto depende de servicios de IA de terceros (Zhipu AI) que tienen costo por uso; revisa los precios antes de automatizar ejecuciones frecuentes.
- Edge TTS es un servicio no documentado oficialmente por Microsoft; su disponibilidad puede cambiar sin aviso.
- Verifica los derechos de autor de la música y los assets usados en `ASSETS/MUSIC` y `ASSETS/VIDEO` antes de publicar contenido monetizado.

## 📄 Licencia

Sin licencia especificada todavía. Se recomienda añadir una (ej. MIT) en un archivo `LICENSE`.
