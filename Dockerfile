# Stage 1: Build the React frontend
# Use ECR Public Gallery to avoid Docker Hub rate limits
FROM public.ecr.aws/docker/library/node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install --legacy-peer-deps
COPY frontend/ ./
RUN npm run build

# Stage 2: Run the Python backend + serve the built frontend
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./

# Copy built frontend from Stage 1
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# App Runner requires port 8080
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
