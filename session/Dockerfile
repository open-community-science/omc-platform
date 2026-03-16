FROM python:3.12-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    chainlit==2.10.0 \
    marimo==0.20.4 \
    plotly \
    pandas \
    numpy \
    openai \
    httpx

# Generate fresh chainlit config, then override with ours
COPY chat_app.py .
RUN chainlit init 2>/dev/null || true
COPY chainlit_config.toml .chainlit/config.toml
COPY notebooks/ notebooks/

# Chainlit on 8080, marimo on 8081
EXPOSE 8080 8081

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
