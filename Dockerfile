FROM python:3.11-slim

WORKDIR /app

# Системные пакеты и настройка часового пояса МСК
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Moscow
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Установка Python-библиотек
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "live_trade.py", "--all", "--real", "--yes", "--asian-range", "--risk", "1.0", "--max-pos", "5"]
