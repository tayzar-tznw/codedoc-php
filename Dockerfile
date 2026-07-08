# Use official Python runtime as base
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY webapp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY webapp/main.py ./webapp/
COPY webapp/frontend/dist ./webapp/frontend/dist
COPY output_docs_pipeline ./output_docs_pipeline
COPY graph_query_agent ./graph_query_agent

# Create .env placeholder (will be overridden by Cloud Run env vars)
RUN echo "GOOGLE_CLOUD_PROJECT=development-459201" > .env && \
    echo "GOOGLE_GENAI_USE_VERTEXAI=true" >> .env

# Expose port
EXPOSE 8080

# Set environment variables
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Run the application
CMD cd /app && uvicorn webapp.main:app --host 0.0.0.0 --port $PORT
