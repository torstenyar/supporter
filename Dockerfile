# Use the official Python image
FROM python:3.9-slim

# Set the working directory to the root of the project
WORKDIR /

# Copy the requirements.txt first to leverage Docker cache
COPY requirements.txt /

# Install the required packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Ensure logs are sent to stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV YARADO_ENVIRONMENT=production
ENV PORT=80

# Expose the port for the application and health checks
EXPOSE 80

# Run the Flask application
CMD ["python", "app.py"]