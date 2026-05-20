# Use a slim Python 3.12 base image.
# 3.12-slim keeps the image small while remaining stable — we pin the minor
# version so builds are reproducible and don't pick up unexpected breaking
# changes from a future Python release.
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /app

# Copy requirements first so Docker can cache the pip install layer.
# As long as requirements.txt hasn't changed, subsequent builds skip this
# step entirely — making iterative builds much faster.
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application source, including the data cache.
# The cache files (data/resolved.json, data/profiles.json) are included so
# the app runs entirely offline without needing API credentials.
COPY . .

# Streamlit listens on 8501 by default
EXPOSE 8501

# --server.address=0.0.0.0 is required inside Docker so the port is
# reachable from the host machine. Without it Streamlit binds to localhost
# only, which is not accessible outside the container.
ENTRYPOINT ["streamlit", "run", "app.py", \
            "--server.port=8501", \
            "--server.address=0.0.0.0", \
            "--server.headless=true"]
