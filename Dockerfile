# Use the official Python image
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Copy the requirements.txt first to leverage Docker cache
COPY requirements.txt .

# Install the required packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port
EXPOSE 80

# Set environment variables from .env file
ENV PORT=80

# Run the Flask application
CMD ["python", "app.py"]
