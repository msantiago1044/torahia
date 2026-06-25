FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg imagemagick ghostscript fonts-liberation libsm6 libxext6 findutils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

RUN pip install --no-cache-dir \
    moviepy==1.0.3 \
    decorator==4.4.2 \
    imageio==2.5.0 \
    openai \
    edge-tts \
    pillow \
    mysql-connector-python \
    requests \
    'numpy<2.0.0'

COPY . .

CMD ["python", "main.py"]