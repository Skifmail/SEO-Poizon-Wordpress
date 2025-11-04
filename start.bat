@echo off
chcp 65001 > nul
cls
echo ════════════════════════════════════════════════════════════════
echo     POIZON → WORDPRESS - СЕРВИС ЗАГРУЗКИ ТОВАРОВ
echo ════════════════════════════════════════════════════════════════
echo.

REM Проверяем наличие Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не найден!
    echo.
    echo Установите Python 3.8 или выше с сайта:
    echo https://www.python.org/downloads/
    echo.
    echo При установке обязательно отметьте "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo [OK] Python найден: 
python --version
echo.

REM Проверяем наличие .env файла
if not exist ".env" (
    echo [ОШИБКА] Файл .env не найден!
    echo.
    echo ПЕРВЫЙ ЗАПУСК? Выполните следующие шаги:
    echo.
    echo 1. Откройте файл "env_example.txt"
    echo 2. Скопируйте его содержимое
    echo 3. Создайте новый файл ".env" (без расширения!)
    echo 4. Вставьте содержимое и заполните API ключи
    echo 5. Запустите этот файл снова
    echo.
    echo Подробная инструкция в файле "ИНСТРУКЦИЯ_УСТАНОВКИ.md"
    echo.
    pause
    exit /b 1
)

echo [OK] Файл .env найден
echo.

REM Проверяем наличие виртуального окружения
if not exist "venv\" (
    echo [INFO] Виртуальное окружение не найдено. Создаю...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ОШИБКА] Не удалось создать виртуальное окружение!
        pause
        exit /b 1
    )
    echo [OK] Виртуальное окружение создано
    echo.
)

REM Активируем виртуальное окружение
echo [INFO] Активация виртуального окружения...
call venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo [ОШИБКА] Не удалось активировать виртуальное окружение!
    pause
    exit /b 1
)
echo [OK] Виртуальное окружение активировано
echo.

REM Проверяем установлены ли зависимости
python -c "import flask" 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Устанавливаю зависимости (это может занять несколько минут)...
    echo.
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ОШИБКА] Не удалось установить зависимости!
        pause
        exit /b 1
    )
    echo.
    echo [OK] Все зависимости установлены
    echo.
)

echo ════════════════════════════════════════════════════════════════
echo     ЗАПУСК ВЕБ-ПРИЛОЖЕНИЯ
echo ════════════════════════════════════════════════════════════════
echo.
echo [INFO] Запускаю сервер...
echo.
echo Веб-интерфейс будет доступен по адресу:
echo.
echo     👉  http://127.0.0.1:5000
echo     👉  http://localhost:5000
echo.
echo Для остановки нажмите Ctrl+C
echo.
echo ════════════════════════════════════════════════════════════════
echo.

REM Запускаем приложение
python web_app.py

REM Если приложение завершилось с ошибкой
if %errorlevel% neq 0 (
    echo.
    echo ════════════════════════════════════════════════════════════════
    echo [ОШИБКА] Приложение завершилось с ошибкой!
    echo ════════════════════════════════════════════════════════════════
    echo.
    echo Возможные причины:
    echo   - Неверные API ключи в файле .env
    echo   - Порт 5000 уже занят другим приложением
    echo   - Нет доступа к интернету
    echo.
    echo Проверьте сообщения об ошибках выше
    echo.
    pause
)

