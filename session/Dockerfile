# Built on the microbial-ecology layer (session/Dockerfile.microbio), which carries R +
# vegan/phyloseq/DESeq2/ALDEx2 and the Python analysis stack. Base-image chaining rather
# than a second container: R's system libraries do not travel through `COPY --from`, and
# Docker shares layers across containers so the size is paid once per host.
#
#   docker build -f session/Dockerfile.microbio -t omc-microbio:latest session/
#   docker build -f session/Dockerfile          -t omc-session:latest   session/
FROM omc-microbio:latest

WORKDIR /app

# Session-only dependencies; the analysis stack comes from the base image.
RUN pip install --no-cache-dir \
    chainlit==2.10.0 \
    marimo==0.20.4 \
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
