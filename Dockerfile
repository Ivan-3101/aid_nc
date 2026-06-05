FROM python:3.14
ENV TZ=Asia/Calcutta

RUN mkdir -p /app
WORKDIR /app

COPY config.json /app
COPY requirements.txt /app

RUN pip install  -r requirements.txt
#RUN pip install --no-cache-dir -r requirements.txt
ENV TZ=Asia/Calcutta


COPY *.py /app/
COPY entrypoint.sh /app/entrypoint.sh

RUN mkdir -p /app/logs /app/secrets && chmod +x /app/entrypoint.sh

# Download embedding model at build time so the image runs fully offline.
# Pass --build-arg HF_TOKEN=hf_... to avoid rate limits during build.
ARG HF_TOKEN
RUN HF_TOKEN=${HF_TOKEN} python -c \
    "from sentence_transformers import SentenceTransformer; \
     SentenceTransformer('sentence-transformers/all-mpnet-base-v2')"

# Prevent any outbound Hub calls at runtime — use only the baked-in cache.
ENV HF_HUB_OFFLINE=1

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

