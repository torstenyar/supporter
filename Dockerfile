# Use the official Python image
FROM python:3.9-slim

# Set the working directory to the root of the project
WORKDIR /app

# Copy the requirements.txt first to leverage Docker cache
COPY requirements.txt /app/

# Install the required packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . /app/

# Expose the port
EXPOSE 80

# Set environment variables from .env file (if needed)
ENV PORT=80

# Ensure logs are sent to stdout/stderr
ENV PYTHONUNBUFFERED=1

# Run the Flask application
CMD ["python", "app.py"]
