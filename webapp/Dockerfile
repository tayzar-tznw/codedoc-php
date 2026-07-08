# Use official Python runtime as base
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .

# Copy frontend build
COPY frontend/dist ./frontend/dist

# Copy docs directory
COPY ../output_docs_pipeline ../output_docs_pipeline

# Copy graph_query_agent module from parent
COPY ../graph_query_agent ../graph_query_agent

# Expose port
EXPOSE 8080

# Set environment variable for port
ENV PORT=8080

# Run the application
CMD uvicorn main:app --host 0.0.0.0 --port $PORT
