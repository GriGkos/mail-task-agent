# Развертывание на VPS

Инструкция рассчитана на Ubuntu VPS с доменом, направленным на IP сервера.

## 1. DNS и доступ к VPS

Создайте A-запись `mail.example.com` на IP VPS. В настройках firewall провайдера
оставьте открытыми только TCP-порты `22`, `80` и `443`.

На сервере установите Docker Engine и Compose plugin по официальной инструкции:
<https://docs.docker.com/engine/install/ubuntu/>.

## 2. Код и секреты

```bash
git clone <URL_РЕПОЗИТОРИЯ> mail-task-agent
cd mail-task-agent
cp .env.example .env
mkdir -p secrets
```

Заполните `.env` на сервере:

```dotenv
APP_ENV=production
APP_BASE_URL=https://mail.example.com
APP_DOMAIN=mail.example.com
DEEPSEEK_API_KEY=...
TOKEN_ENCRYPTION_KEY=...
TELEGRAM_BOT_TOKEN=...
SAFE_MODE=true
DRY_RUN=true
```

Значения `TOKEN_ENCRYPTION_KEY`, `DEEPSEEK_API_KEY` и `TELEGRAM_BOT_TOKEN` не
нужно помещать в git.

## 3. Подключение почты

Для новых пользователей отдельные Google OAuth и Microsoft OAuth настройки не нужны.
Почта подключается единым способом через IMAP. Старые OAuth-коннекторы
оставлены в коде только для совместимости с ранее созданными аккаунтами.

## 5. Запуск

```bash
docker compose up -d api
docker compose --profile workers up -d
docker compose --profile edge up -d caddy
docker compose ps
```

Проверка:

```bash
curl https://mail.example.com/health
docker compose logs --tail=100 api telegram-bot gmail-worker
```

Caddy сам получает и обновляет Let's Encrypt certificate, когда DNS уже смотрит
на VPS и порты 80/443 доступны снаружи.

## 6. Подключение почты через Telegram

Откройте бота с телефона и отправьте `/start`, затем нажмите **Привязать почту**
или отправьте `/mail`. Бот выдаст одноразовую HTTPS-ссылку на форму подключения.
В форме укажите адрес почты и пароль приложения. Для известных почтовых доменов сервер,
порт и тип защиты определятся автоматически. Для неизвестного домена дополнительные
параметры можно открыть в форме и заполнить вручную.

Пароль не отправляется сообщением в Telegram. Для каждого Telegram-пользователя
создаётся отдельный почтовый аккаунт и набор задач.

## 7. Управление

```bash
docker compose ps
docker compose logs -f telegram-bot
docker compose restart telegram-bot gmail-worker digest-worker
docker compose down
```

## 8. Универсальное подключение IMAP

В Telegram пользователь может выбрать **Привязать почту** или отправить команду `/mail`.
Бот выдаёт одноразовую HTTPS-ссылку на форму подключения. Пароль не вводится сообщением в Telegram.

Основная форма содержит только адрес почты и пароль приложения. Дополнительные поля для IMAP-сервера,
порта, логина, папки и типа защиты скрыты в разделе **Дополнительные настройки** и нужны
только для неизвестных или нестандартных почтовых сервисов.

Поддержаны готовые серверы Gmail, Outlook.com, Яндекс и Mail.ru, а также произвольный IMAP-сервер.
Секреты сохраняются в `mail_accounts.encrypted_token` через `TOKEN_ENCRYPTION_KEY`.

После первого запуска IMAP-аккаунта агент создаёт baseline по `UIDVALIDITY` и `UID`. Старые письма
не обрабатываются; дальше читаются только новые сообщения. Дедупликация выполняется в базе данных.
У IMAP нет единого стандарта ярлыков, поэтому категории Gmail/Outlook для IMAP не записываются обратно
в почту, а состояние задачи хранится в MailTaskBot.

Для известных доменов сервер, порт и тип защиты определяются по адресу автоматически. Для неизвестного
домена пользователь может открыть дополнительные настройки и указать их вручную.
Используйте пароль приложения, а не обычный пароль от почты. Для Outlook.com
IMAP должен быть включён в настройках аккаунта; если политика провайдера запрещает парольный
IMAP-доступ, этот способ подключить такой аккаунт не сможет.

Не запускайте `docker compose down -v`, если нужно сохранить данные Postgres.
