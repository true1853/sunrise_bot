#!/bin/bash
# Скрипт обновления проекта: получение последних изменений, обновление зависимостей и перезапуск сервиса

# Настройте переменные:
PROJECT_DIR="/root/sunrise_bot"          # Путь к директории проекта на сервере
BRANCH="main"                            # Ветка, которую необходимо обновлять (например, main)
SERVICE_NAME="sunrise_bot.service"       # Имя systemd-сервиса вашего бота
VENV_DIR="/root/sunrise_bot/venv"         # Путь к виртуальному окружению (если используется)

echo "=== Обновление проекта ==="

# Переход в директорию проекта
echo "Переход в директорию проекта: $PROJECT_DIR"
cd "$PROJECT_DIR" || { echo "Не удалось перейти в $PROJECT_DIR"; exit 1; }

# Получение последних изменений из репозитория
echo "Получение последних изменений из ветки $BRANCH..."
git pull origin "$BRANCH" || { echo "Ошибка при выполнении git pull"; exit 1; }

# Обновление зависимостей, если найден requirements.txt
if [ -f "requirements.txt" ]; then
    echo "Обновление зависимостей..."
    source "$VENV_DIR/bin/activate" || { echo "Не удалось активировать виртуальное окружение"; exit 1; }
    pip install -r requirements.txt || { echo "Ошибка установки зависимостей"; deactivate; exit 1; }
    deactivate
else
    echo "Файл requirements.txt не найден. Пропуск обновления зависимостей."
fi

# Перезапуск systemd-сервиса
echo "Перезапуск сервиса $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME" || { echo "Не удалось перезапустить сервис $SERVICE_NAME"; exit 1; }

# Вывод статуса сервиса
echo "Статус сервиса $SERVICE_NAME:"
sudo systemctl status "$SERVICE_NAME" --no-pager

echo "=== Обновление завершено ==="
