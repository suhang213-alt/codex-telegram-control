# Переносимое ядро на Windows и Linux

Ядро создаёт собственные локальные чаты через Codex CLI. Оно не импортирует чаты Desktop или ChatGPT. Ниже — установка ядра; существующий Desktop на Windows подключается через [отдельный необязательный адаптер](../adapters/windows_desktop/README.md). Его native-проверки не входят в проверку ядра.

## Общая подготовка

Распакуйте пакет. Установите Python 3.11+ и [официальный Codex CLI](https://learn.chatgpt.com/docs/cli); справочник — [CLI reference](https://learn.chatgpt.com/docs/cli/reference). Убедитесь, что `codex --version` работает в новом терминале. На Windows стандартный npm launcher разрешается кодом в прямой запуск Node, без shell; нестандартный wrapper может потребовать явного пути к исполняемому CLI.

Создайте отдельного нового бота через официальный BotFather, узнайте свой ID через `scripts/identify-owner.py` и подготовьте публичный HTTPS-туннель. Подробный общий порядок — [шаги 2–3 инструкции macOS](INSTALL_MACOS.md#2-создайте-своего-бота-и-узнайте-свой-id). На Windows запускайте helper через `py -3 scripts/identify-owner.py`, на Linux — `python3 scripts/identify-owner.py`.

После отдельной установки [cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) команда первого тестового туннеля одинакова:

```console
cloudflared tunnel --url http://127.0.0.1:8787
```

Держите туннель в отдельном терминале. Скопируйте выданный HTTPS origin. Quick Tunnel предназначен для тестов; его адрес временный. Для постоянного использования настройте собственный именованный туннель. [Ограничения Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

## Windows

Откройте PowerShell в корне распакованного пакета. Создайте отдельную рабочую папку:

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\CodexTelegramWork"
py -3 --version
codex --version
.\scripts\install-windows.ps1
```

Если действующая политика PowerShell запрещает скрипт, не меняйте её глобально. Выполните те же команды вручную:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\codex-telegram.exe setup
```

В настройке укажите свой числовой ID, полный путь созданной папки, HTTPS origin, CLI и токен нового бота. Enter в вопросе о записи сохраняет режим `read-only`; `yes` выбирает `workspace-write`. Токен сохраняется через системное хранилище учётных данных Windows.

```powershell
.\.venv\Scripts\codex-telegram.exe login
.\.venv\Scripts\codex-telegram.exe doctor --protocol
.\.venv\Scripts\codex-telegram.exe run
```

Не запускайте следующую команду после ошибки предыдущей. `login` использует отдельный Codex home, `doctor --protocol` проверяет handshake без задания модели. `run` остаётся в терминале; Ctrl+C останавливает мост. Фоновая служба и автозапуск не устанавливаются.

Данные находятся в `%LOCALAPPDATA%\codex-telegram-control`. Для смены HTTPS-адреса остановите мост, замените placeholder своим адресом и выполните:

```powershell
.\.venv\Scripts\python.exe scripts/configure.py --public-url https://YOUR_ADDRESS
.\.venv\Scripts\codex-telegram.exe run
```

## Linux

Нужны Python 3.11+ с модулем `venv`, Codex CLI и доступное системное хранилище Secret Service или KWallet в пользовательской сессии. Если `venv` отсутствует, установите соответствующий пакет средствами своего дистрибутива. Требования Linux keyring зависят от выбранного backend; см. [документацию keyring](https://pypi.org/project/keyring/).

Из корня пакета:

```sh
mkdir -p "$HOME/CodexTelegramWork"
python3 --version
codex --version
sh scripts/install-linux.sh
.venv/bin/codex-telegram login
.venv/bin/codex-telegram doctor --protocol
.venv/bin/codex-telegram run
```

Заполняйте те же значения, что в таблице [настройки](INSTALL_MACOS.md#4-установите-и-настройте-мост). При ошибке остановитесь и устраните причину. На сервере без пользовательского keyring можно оставить ввод токена пустым и подавать `TELEGRAM_BOT_TOKEN` через выбранный вами менеджер секретов в окружение **процесса моста**. Не помещайте токен в исходники, `.env` репозитория или строку команды shell. Автоматического перехода к открытому хранению токена нет.

Данные находятся в `${XDG_DATA_HOME:-~/.local/share}/codex-telegram-control`. Остановка — Ctrl+C. Смена адреса после остановки:

```sh
.venv/bin/python scripts/configure.py --public-url https://YOUR_ADDRESS
.venv/bin/codex-telegram run
```

## Первая приёмка

Откройте своего бота → `/panel`, создайте `/new Проверка`, затем явно отправьте первое безопасное текстовое задание. Открытие панели и handshake не заменяют этот тест. Проверьте текст, файлы, разрешения, остановку и повторный запуск по [чек-листу](MOBILE_ACCEPTANCE.md).

Компьютер, мост и туннель должны работать одновременно. Реальный прогон на всех трёх ОС, живой Codex и телефон пока не подтверждены; актуальный объём проверки указан в [RELEASE.md](RELEASE.md). Не копируйте каталог данных и авторизацию с чужой установки.
