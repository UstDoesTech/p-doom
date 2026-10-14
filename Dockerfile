# Not built or run in the session that wrote it.
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/src ./backend/src
RUN pip install --no-cache-dir ./backend
COPY databricks/sql ./databricks/sql
COPY --from=web /web/dist /app/static
ENV PDOOM_STATIC_DIR=/app/static
RUN useradd --create-home app
USER app
EXPOSE 8000
CMD ["uvicorn", "pdoom.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
