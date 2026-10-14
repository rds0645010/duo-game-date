@echo off
chcp 65001 > nul
title DUO Game Date Bot
echo ====================================================
echo   🕹️ Запуск сервера DUO Game Date Bot
echo ====================================================
echo.

if not exist .env (
    echo [!] Файл .env не найден. Создаю файл .env...
    (
        echo # Токен твоего бота от @BotFather в Telegram:
        echo BOT_TOKEN=ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER
        echo.
        echo # Ссылка на приложение:
        echo WEBAPP_URL=https://rds0645010.github.io/duo-game-date/
    ) > .env
    echo [!] Открываю файл .env. Вставь туда свой токен от @BotFather и сохрани!
    start notepad.exe .env
    echo.
    echo После сохранения токена в файле .env нажми любую клавишу...
    pause > nul
)

echo Запуск бота...
python bot.py
pause
