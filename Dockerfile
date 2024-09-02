# Use the official Python image
FROM python:3.9-slim

# Set the working directory to /app
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements.txt first to leverage Docker cache
COPY requirements.txt .

# Install the required Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Ensure logs are sent to stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV YARADO_ENVIRONMENT=production
ENV PORT=80

# Create a non-root user and switch to it
RUN useradd -m appuser
USER appuser

# Expose the port for the application
EXPOSE 80

# Run the Flask application
CMD ["python", "app.py"]
