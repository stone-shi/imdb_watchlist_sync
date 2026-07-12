# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Install system dependencies for Chrome and SeleniumBase
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    xvfb \
    libxi6 \
    libxss1 \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    fonts-liberation \
    libasound2 \
    libgbm1 \
    libnspr4 \
    xdg-utils \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y google-chrome-stable --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the seleniumbase driver
RUN sbase install chromedriver

# Copy the application code
COPY imdb_server.py mcp_server.py embedding.py .
COPY version.txt* .

# Expose port
EXPOSE 8080

# Environment variables
ENV PYTHONUNBUFFERED=1

# Run the server
CMD ["python", "imdb_server.py", "--host", "0.0.0.0", "--port", "8080"]
