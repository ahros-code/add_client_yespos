"""
Асинхронная логика работы с админкой YesPOS через Playwright.
Держит одну открытую залогиненную сессию браузера и переиспользует её
для создания клиентов одного за другим (без повторного логина каждый раз).
"""

import asyncio
import logging
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger("yespos_client")

UZ_MONTHS = [
    "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
    "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr",
]


class YesPosClient:
    """
    Управляет одной браузерной сессией, залогиненной в YesPOS.
    Использование:
        client = YesPosClient(org_id, login, password)
        await client.start()
        await client.create_client({...})
        await client.stop()
    """

    def __init__(self, org_id: str, login: str, password: str, headless: bool = True):
        self.org_id = org_id
        self.login = login
        self.password = password
        self.headless = headless

        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        # Гарантирует, что запросы на создание клиента обрабатываются
        # строго по одному (браузер не умеет параллелить действия на одной странице)
        self._lock = asyncio.Lock()

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        self._page.set_default_timeout(15000)
        await self._login()

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _login(self):
        page = self._page
        await page.goto("https://app.yespos.uz/")
        await page.locator("input[name='org_id']").wait_for(state="visible", timeout=30000)

        await page.locator("input[name='org_id']").fill(self.org_id)
        await page.locator("input[name='login']").fill(self.login)
        await page.locator("input[name='password']").fill(self.password)
        await page.get_by_role("button", name="Kirish").click()

        await page.get_by_text("Mijozlar", exact=True).first.wait_for(
            state="visible", timeout=30000
        )
        await page.wait_for_timeout(1000)
        logger.info("YesPOS: логин выполнен успешно")

    async def _ensure_logged_in(self):
        """Проверяет, что сессия жива, и перелогинивается при необходимости."""
        try:
            visible = await self._page.get_by_text("Mijozlar", exact=True).first.is_visible()
            if not visible:
                raise RuntimeError("not on dashboard")
        except Exception:
            logger.warning("YesPOS: сессия похоже истекла, перелогиниваемся")
            await self._login()

    async def create_client(self, client: dict) -> dict:
        """
        Создаёт клиента в YesPOS.
        client: словарь с ключами
            name, birth_date (DD.MM.YYYY), gender (Erkak/Ayol), phone, email,
            stir, kpp, okpo, bank, mfo, account_number, address, description
        Возвращает {"success": True} или {"success": False, "error": "..."}
        """
        async with self._lock:
            try:
                await self._ensure_logged_in()
                await self._open_create_form()
                await self._fill_form(client)
                await self._save()
                logger.info("YesPOS: клиент создан: %s", client.get("name"))

                await self._set_bonus_card(
                    name=client.get("name", ""),
                    barcode=client.get("barcode", ""),
                    bonus_percent=client.get("bonus_percent", ""),
                )
                logger.info("YesPOS: карта установлена в 'Бонусная карта'")

                return {"success": True}
            except Exception as e:
                logger.exception("YesPOS: ошибка при создании клиента")
                # На всякий случай делаем скриншот для диагностики
                try:
                    await self._page.screenshot(path="last_error.png")
                except Exception:
                    pass
                return {"success": False, "error": str(e)}

    async def _set_bonus_card(
        self, name: str = "", barcode: str = "", bonus_percent: str = ""
    ):
        """
        Открываем меню карт лояльности для строки только что созданного
        клиента (находим её по имени, а не по позиции в таблице - список
        не всегда показывает нового клиента первой строкой), меняем тип
        карты на "Бонусная карта", задаём баркод и процент бонуса.
        """
        page = self._page

        # Ждём, что список клиентов отобразился (диалог создания закрылся)
        await page.wait_for_timeout(1000)

        # Баг YesPOS: кнопка "discounts" иногда не реагирует на клик после
        # создания клиента, пока страница не перезагружена. Вместо того
        # чтобы ждать/ретраить клик, просто обновляем страницу и кликаем
        # на свежем DOM. Переход по "Mijozlar" делается кликами (не сменой
        # URL), поэтому после reload() нужно заново дойти до списка клиентов -
        # иначе reload() может выкинуть на дефолтный экран (например, дашборд),
        # и строка/кнопка ниже просто не найдутся.
        await page.reload()
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)
        await page.get_by_text("Mijozlar", exact=True).first.click()
        await page.wait_for_timeout(1000)
        await page.get_by_text("Mijozlar", exact=True).last.click()
        await page.wait_for_timeout(1000)

        # Находим строку таблицы именно созданного клиента по имени,
        # а не просто первую строку - иначе можно попасть на карту
        # соседнего клиента.
        row = page.locator("tr", has_text=name).first
        await row.wait_for(state="visible", timeout=15000)

        discounts_btn = row.locator("button[aria-label='discounts']")
        await discounts_btn.wait_for(state="visible", timeout=15000)
        await discounts_btn.click()
        await page.wait_for_timeout(500)

        # В открывшемся меню нажимаем "Tahrirlash" (редактировать)
        edit_btn = page.locator("button[aria-label='Tahrirlash']").first
        await edit_btn.wait_for(state="visible", timeout=10000)
        await edit_btn.click()
        await page.wait_for_timeout(500)

        # Открывается окно с select "Накопительная карта" (MUI select,
        # id=mui-component-select-type). Меняем на "Бонусная карта" (value=3).
        card_select = page.locator("#mui-component-select-type")
        await card_select.wait_for(state="visible", timeout=10000)
        await card_select.click()
        await page.wait_for_timeout(300)

        bonus_option = page.locator("li[data-value='3']")
        await bonus_option.wait_for(state="visible", timeout=10000)
        await bonus_option.click()
        await page.wait_for_timeout(300)

        # Баркод карты
        if barcode:
            await page.locator("input[name='scan']").fill(barcode)

        # Процент бонуса
        if bonus_percent:
            await page.locator("input[name='bonus_percent']").fill(str(bonus_percent))

        await page.wait_for_timeout(300)

        # Сохраняем изменения карты
        await page.get_by_role("button", name="Saqlash").click()
        await page.wait_for_timeout(1500)

    async def _open_create_form(self):
        page = self._page
        await page.get_by_text("Mijozlar", exact=True).first.click()
        await page.wait_for_timeout(1000)
        await page.get_by_text("Mijozlar", exact=True).last.click()
        await page.wait_for_timeout(1000)
        await page.get_by_role("button", name="Yaratish").click()
        await page.wait_for_timeout(500)

    async def _fill_form(self, client: dict):
        page = self._page

        if client.get("name"):
            await page.locator("[name='name']").fill(client["name"])

        if client.get("birth_date"):
            await self._set_birth_date(client["birth_date"])

        if client.get("gender"):
            gender_value = "M" if client["gender"] == "Erkak" else "F"
            await page.locator("#mui-component-select-sex").click()
            await page.wait_for_timeout(300)
            await page.locator(f"li[data-value='{gender_value}']").click()
            await page.wait_for_timeout(300)

        if client.get("phone"):
            phone_field = page.locator("[name='phone']")
            # Поле телефона использует маску ввода (+998 (XX) XXX-XX-XX).
            # fill() вставляет значение мгновенно и маска обрабатывает его
            # неправильно (всегда получается один и тот же "мусорный" номер).
            # Поэтому очищаем поле и печатаем по символам, как реальный ввод.
            await phone_field.click()
            await phone_field.press("Control+A")
            await phone_field.press("Delete")
            # Маска сама покажет "+998 (" - печатаем только цифры после кода страны
            digits_only = "".join(ch for ch in client["phone"] if ch.isdigit())
            if digits_only.startswith("998"):
                digits_only = digits_only[3:]  # маска уже содержит +998
            await phone_field.type(digits_only, delay=50)

        if client.get("email"):
            await page.locator("[name='email']").fill(client["email"])

        if client.get("stir"):
            await page.locator("[name='inn']").fill(client["stir"])

        if client.get("kpp"):
            await page.locator("[name='kpp']").fill(client["kpp"])

        if client.get("okpo"):
            await page.locator("[name='okpo']").fill(client["okpo"])

        if client.get("bank"):
            await page.locator("[name='bank']").fill(client["bank"])

        if client.get("mfo"):
            await page.locator("[name='mfo']").fill(client["mfo"])

        if client.get("account_number"):
            await page.locator("[name='rs']").fill(client["account_number"])

        if client.get("address"):
            await page.locator("[name='adress']").fill(client["address"])

        if client.get("description"):
            await page.locator("textarea[name='description']").fill(client["description"])

        await page.wait_for_timeout(300)

    async def _save(self):
        page = self._page
        await page.get_by_role("button", name="Saqlash").click()
        await page.wait_for_timeout(1500)

    async def _set_birth_date(self, date_str: str):
        page = self._page
        day, month, year = date_str.split(".")
        day, month, year = int(day), int(month), int(year)

        await page.locator("[name='birth_date']").click()
        await page.wait_for_timeout(500)

        dialog = page.get_by_role("dialog", name="Select date")
        await dialog.wait_for(state="visible", timeout=10000)

        header = dialog.locator("text=/^[A-Za-z]+ \\d{4}$/").first
        await header.click()
        await page.wait_for_timeout(500)

        await dialog.get_by_text(str(year), exact=True).click()
        await page.wait_for_timeout(500)

        if await dialog.count() == 0 or not await dialog.is_visible():
            await page.locator("[name='birth_date']").click()
            await page.wait_for_timeout(500)
            dialog = page.get_by_role("dialog", name="Select date")
            await dialog.wait_for(state="visible", timeout=10000)

        prev_btn = dialog.locator("button[aria-label*='revious']")
        next_btn = dialog.locator("button[aria-label*='ext']")

        for _ in range(24):
            header_text = await dialog.locator(
                "text=/^[A-Za-z]+ \\d{4}$/"
            ).first.inner_text()
            current_month_name, current_year_str = header_text.split(" ")
            current_month = (
                UZ_MONTHS.index(current_month_name) + 1
                if current_month_name in UZ_MONTHS
                else None
            )
            current_year = int(current_year_str)

            if current_year == year and current_month == month:
                break
            elif current_year < year or (current_year == year and current_month < month):
                await next_btn.click()
            else:
                await prev_btn.click()
            await page.wait_for_timeout(300)

        day_locator = dialog.locator("button[role='gridcell']").get_by_text(
            str(day), exact=True
        )
        await day_locator.first.wait_for(state="visible", timeout=15000)
        await day_locator.first.click()
        await page.wait_for_timeout(300)

        confirm = dialog.get_by_text("Tanlash", exact=True)
        if await confirm.count() > 0:
            await confirm.click()
            await page.wait_for_timeout(300)