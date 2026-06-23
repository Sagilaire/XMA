FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TOOLS_DIR=/tools \
    TEMP_DIR=/temp/workflows \
    LOG_LEVEL=INFO

# System dependencies:
#   * openjdk-17-jdk-headless -> Java (apktool) + keytool (debug keystore gen)
#   * imagemagick             -> icon resize
#   * ca-certificates         -> HTTPS to Google's Android repo and GitHub
#   * wget, unzip, curl       -> bootstrapping build-tools and apktool
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jdk-headless \
        imagemagick \
        ca-certificates \
        wget \
        unzip \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Some Debian/Ubuntu ImageMagick builds restrict PDF/PS inputs; PNGs are always
# allowed by default. We add a defensive policy override just in case the base
# image flips the defaults. (See https://imagemagick.org/script/security-policy.php)
RUN if [ -f /etc/ImageMagick-6/policy.xml ]; then \
        sed -i 's|<policy domain="coder" rights="none" pattern="PDF" />|<policy domain="coder" rights="read|write" pattern="PNG" />|' \
            /etc/ImageMagick-6/policy.xml || true; \
    fi

WORKDIR /app

# Install Python deps in a deterministic order so Docker can cache the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && (pip check || true)

# Copy the app source.
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Empty marker so the persistent /tools volume doesn't shadow the image's
# defaults when first mounted.
RUN mkdir -p /tools /temp/workflows \
    && touch /tools/.keep /temp/workflows/.keep

EXPOSE 4015

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4015", "--workers", "1"]
