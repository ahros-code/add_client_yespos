# Официальный образ Microsoft уже содержит Chromium и все системные
# библиотеки, нужные Playwright - не нужно ставить их вручную.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway сам подставит переменную PORT - слушаем именно её
ENV PORT=8000
EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
