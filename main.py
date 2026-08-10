"""
Отдельный сервис, который:
1. Держит одну залогиненную Playwright-сессию в YesPOS.
2. Принимает HTTP-запросы от Telegram-бота на создание клиента.
3. Обрабатывает их по очереди в фоне (одна задача за раз).

Запуск:
    pip install fastapi uvicorn playwright
    playwright install chromium
    uvicorn main:app --host 0.0.0.0 --port 8000

Бот стучится сюда:
    POST http://localhost:8000/create_client
    {
        "name": "Ivan Ivanov",
        "phone": "+998901234567",
        "birth_date": "15.03.1995",
        "gender": "Erkak",
        "email": "",
        ...
    }
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Подхватываем .env только для локального запуска.
# На Railway переменные окружения задаются в Variables и .env не нужен -
# если файла нет, load_dotenv просто ничего не делает.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from yespos_client import YesPosClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yespos_service")

# ==================== НАСТРОЙКИ ====================

# Все секретные данные берутся ТОЛЬКО из переменных окружения.
# Задайте их в Railway: Settings -> Variables
#   YESPOS_ORG_ID
#   YESPOS_LOGIN
#   YESPOS_PASSWORD
#   YESPOS_HEADLESS (необязательно, по умолчанию true)

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Переменная окружения {name} не задана. "
            f"Добавьте её в настройках сервиса (Railway -> Variables) "
            f"или в .env файле для локального запуска."
        )
    return value


ORG_ID = _require_env("YESPOS_ORG_ID")
LOGIN = _require_env("YESPOS_LOGIN")
PASSWORD = _require_env("YESPOS_PASSWORD")
HEADLESS = os.getenv("YESPOS_HEADLESS", "true").lower() != "false"

# =====================================================


yespos_client: YesPosClient | None = None
task_queue: asyncio.Queue | None = None
worker_task: asyncio.Task | None = None


class ClientData(BaseModel):
    name: str
    phone: str
    birth_date: str = ""       # формат DD.MM.YYYY
    gender: str = ""           # "Erkak" или "Ayol"
    email: str = ""
    stir: str = ""
    kpp: str = ""
    okpo: str = ""
    bank: str = ""
    mfo: str = ""
    account_number: str = ""
    address: str = ""
    description: str = ""
    barcode: str = ""          # баркод бонусной карты
    bonus_percent: str = ""    # процент бонуса


async def worker():
    """Фоновая задача: берёт клиентов из очереди и создаёт их по одному."""
    while True:
        job = await task_queue.get()
        client_data, future = job
        try:
            result = await yespos_client.create_client(client_data)
            if not future.done():
                future.set_result(result)
        except Exception as e:
            logger.exception("Ошибка воркера")
            if not future.done():
                future.set_result({"success": False, "error": str(e)})
        finally:
            task_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global yespos_client, task_queue, worker_task

    yespos_client = YesPosClient(ORG_ID, LOGIN, PASSWORD, headless=HEADLESS)
    await yespos_client.start()

    task_queue = asyncio.Queue()
    worker_task = asyncio.create_task(worker())

    logger.info("YesPOS сервис запущен и готов принимать запросы")
    yield

    worker_task.cancel()
    await yespos_client.stop()
    logger.info("YesPOS сервис остановлен")


app = FastAPI(lifespan=lifespan)


@app.post("/create_client")
async def create_client_endpoint(client: ClientData):
    """
    Ставит задачу на создание клиента в очередь и ждёт результата.
    Если хотите fire-and-forget (не ждать ответа) - смотрите
    /create_client_async ниже.
    """
    future = asyncio.get_event_loop().create_future()
    await task_queue.put((client.dict(), future))

    try:
        result = await asyncio.wait_for(future, timeout=90)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="YesPOS не ответил вовремя")

    if not result["success"]:
        raise HTTPException(status_code=502, detail=result.get("error", "Unknown error"))

    return {"status": "ok"}


@app.post("/create_client_async")
async def create_client_async_endpoint(client: ClientData):
    """
    Fire-and-forget вариант: сразу отвечает боту "принято",
    не дожидаясь пока браузер реально создаст клиента.
    Полезно, чтобы пользователь бота не ждал 5-10 секунд ответа.
    """
    future = asyncio.get_event_loop().create_future()
    await task_queue.put((client.dict(), future))
    return {"status": "queued"}


@app.get("/health")
async def health():
    return {"status": "ok", "queue_size": task_queue.qsize() if task_queue else 0}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)