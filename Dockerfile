FROM python:3.11-slim

WORKDIR /app

# Install system deps: ffmpeg, fontconfig, fonts
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      fonts-noto-cjk \
      fonts-noto-core \
      fonts-noto-extra \
      fontconfig \
      && rm -rf /var/lib/apt/lists/*

# Copy python requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Ensure font cache (so libass/ffmpeg finds the fonts)
RUN fc-cache -f -v || true

EXPOSE 8080
CMD ["python", "app.py"]
