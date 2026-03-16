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

# Copy app code and generate chainlit config
COPY chat_app.py .
COPY notebooks/ notebooks/
RUN chainlit init 2>/dev/null || true

# Patch chainlit config and replace readme with blank
COPY chainlit.md /app/chainlit.md
RUN sed -i 's/^name = .*/name = "OMC Research Assistant"/' /app/.chainlit/config.toml && \
    sed -i 's/^enable_telemetry = .*/enable_telemetry = false/' /app/.chainlit/config.toml && \
    sed -i 's/^cache = .*/cache = false/' /app/.chainlit/config.toml

# Chainlit on 8080, marimo on 8081
EXPOSE 8080 8081

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
