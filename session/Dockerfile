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
COPY notebooks/ notebooks/

# Chainlit config — generated from chainlit 2.10.0, customized, committed to repo
COPY .chainlit/config.toml /app/.chainlit/config.toml
COPY chainlit.md /app/chainlit.md

# Chainlit on 8080, marimo on 8081
EXPOSE 8080 8081

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
