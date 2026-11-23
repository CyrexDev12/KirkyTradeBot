FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --uid 10001 --create-home app
WORKDIR /app

FROM base AS backend
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY frontend ./frontend
RUN chmod -R a+rX /app/backend /app/frontend \
    && mkdir /data && chown app:app /data
ENV NFL_DATA_DIR=/data OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS bot
ENV MPLBACKEND=Agg MPLCONFIGDIR=/tmp/nfl-matplotlib
COPY bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
RUN chmod -R a+rX /app/bot \
    && mkdir /state && chown app:app /state
USER app
CMD ["python", "-m", "bot.discord_bot"]
