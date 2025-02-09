#!/bin/bash
# Скрипт обновления кода на сервере

# Настройте переменные:
PROJECT_DIR="/root/sunrise_bot"   # Путь к директории проекта (замените на ваш путь)
BRANCH="main"                         # Имя ветки, которую необходимо обновлять (например, main или master)
SERVICE_NAME="sunrise_bot.service"    # Имя systemd-сервиса вашего бота

echo "=== Обновление проекта ==="

# Переходим в директорию проекта
echo "Переход в директорию проекта: $PROJECT_DIR"
cd "$PROJECT_DIR" || { echo "Не удалось перейти в $PROJECT_DIR"; exit 1; }

# Получаем последние изменения из репозитория
echo "Получение последних изменений из ветки $BRANCH..."
git pull origin "$BRANCH" || { echo "Ошибка при выполнении git pull"; exit 1; }

# Если есть файл requirements.txt, обновляем зависимости
if [ -f "requirements.txt" ]; then
    echo "Обновление зависимостей..."
    # Активируем виртуальное окружение
    source venv/bin/activate || { echo "Не удалось активировать виртуальное окружение"; exit 1; }
    pip install -r requirements.txt || { echo "Ошибка установки зависимостей"; deactivate; exit 1; }
    deactivate
else
    echo "Файл requirements.txt не найден. Пропуск обновления зависимостей."
fi

# Перезапускаем сервис
echo "Перезапуск сервиса $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME" || { echo "Не удалось перезапустить сервис $SERVICE_NAME"; exit 1; }

# Вывод статуса сервиса
echo "Статус сервиса $SERVICE_NAME:"
sudo systemctl status "$SERVICE_NAME" --no-pager

echo "=== Обновление завершено ==="
