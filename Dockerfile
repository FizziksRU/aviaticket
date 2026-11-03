# Образ бота. Телефоны DroidHub не требуют браузера в контейнере; Chromium нужен
# только для сайтов без телефона (desktop-Playwright): соберите с --build-arg WITH_BROWSER=1.
FROM python:3.12-slim

ARG WITH_BROWSER=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_BROWSER" = "1" ]; then playwright install --with-deps chromium; fi

COPY src ./src
COPY config ./config

# state/ (SQLite: пользователи, подписки, история цен) и debug/ — тома, не слой образа
RUN useradd --create-home --uid 10001 avia && mkdir -p state debug && chown avia:avia state debug
USER avia

# SIGTERM от docker stop ведёт к аккуратной остановке только с веткой scale-audit-dialog;
# без неё бот завершается сразу
STOPSIGNAL SIGTERM
CMD ["python", "-m", "avia.bot"]
