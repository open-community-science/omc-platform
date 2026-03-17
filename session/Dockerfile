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

# Copy app code
COPY chat_app.py .
COPY tools.py .
COPY data_layer.py .
COPY notebooks/ notebooks/
COPY viz_server.py .
COPY viz/ /app/viz/

# Chainlit config — generated from chainlit 2.10.0, customized, committed to repo
COPY .chainlit/config.toml /app/.chainlit/config.toml
COPY chainlit.md /app/chainlit.md

# Chainlit on 8080, marimo on 8081, viz on 8082
EXPOSE 8080 8081 8082

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
