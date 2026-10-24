# Docker: Windows/macOS → Android или iPhone

Кандидат использует Docker и компонент компьютера. Реальная связка с личным аккаунтом и физическим Samsung ещё требует [приёмки](MOBILE_ACCEPTANCE.md). Подробности — [DOCKER](DOCKER.md); прежний запуск без Docker — [QUICK_START_NATIVE](QUICK_START_NATIVE.md).

## Подготовка

1. Скачайте и распакуйте ZIP из GitHub Release. Там же скопируйте опубликованную строку **`имя/образ@sha256:…`** для настройщика. Архив и образ должны быть одной версии.
2. Установите официальный Codex Desktop, войдите и создайте локальный чат в отдельной рабочей папке. Установите Docker Desktop с Linux-контейнерами и Python **3.12 для Windows**, **3.11+ для Mac**.
3. В официальном BotFather выполните `/newbot`. Токен вводите только в скрытый запрос установщика. Команда `identify-owner.py` ниже попросит токен и покажет случайную `/identify …` команду: отправьте её своему боту, затем скопируйте выведенный числовой ID в настройщик.
4. Создайте свой named tunnel Cloudflare и HTTPS hostname с HTTP service **`http://relay:8787`** по [официальной инструкции](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/). Скопируйте URL панели и tunnel token. Управляющий порт `8788` в туннель не направляйте.

Откройте терминал в распакованной папке: Windows — напишите `powershell` в адресной строке Проводника; Mac — в Terminal напишите `cd ` и перетащите папку в окно.

## Mac → Samsung / Android или iPhone

1. Получите ID и запустите установку:

```sh
python3 scripts/identify-owner.py
sh scripts/docker-install.command
```

Введите свой ID, рабочую папку, HTTPS URL и BotFather token. На этапе Docker setup вставьте строку образа из Release, повторите BotFather token и вставьте tunnel token. Секреты вводятся скрыто.

2. Загрузите и запустите контейнеры:

```sh
.venv/bin/python scripts/docker.py pull
.venv/bin/python scripts/docker.py check
.venv/bin/python scripts/docker.py up
```

3. Дождитесь окончания задач Desktop, закройте его через Cmd+Q и вставьте:

```sh
.venv/bin/python scripts/docker.py desktop-launch
.venv/bin/python scripts/docker.py desktop-doctor
.venv/bin/python scripts/docker.py host
```

Не продолжайте при `FAIL`. Последнее окно оставьте работающим. На телефоне: свой бот → `/chats` → `/use номер` → `/panel`; выберите существующий чат.

## Windows → Android или iPhone

1. В PowerShell вставьте:

```powershell
py -3.12 scripts/identify-owner.py
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe scripts/docker.py desktop-setup
```

Native setup попросит собственный служебный локальный чат Desktop и проверит совместимый bundled plugin. Прежний tunnel provider оставьте выключенным. Для native gateway и одноразовой привязки выполните [точные шаги Windows](../adapters/windows_desktop/README.md#ответы-на-настоящие-запросы-разрешений-desktop); они обязательны для ответов на исходные запросы разрешений Desktop.

2. После успешной native-подготовки вставьте:

```powershell
.\.venv\Scripts\python.exe scripts/docker.py setup
.\.venv\Scripts\python.exe scripts/docker.py pull
.\.venv\Scripts\python.exe scripts/docker.py check
.\.venv\Scripts\python.exe scripts/docker.py up
```

В `setup` вставьте строку образа из Release и свои секреты по скрытым запросам. После штатного закрытия Desktop:

```powershell
.\.venv\Scripts\python.exe scripts/docker.py desktop-launch
.\.venv\Scripts\python.exe scripts/docker.py desktop-doctor
.\.venv\Scripts\python.exe scripts/docker.py host
```

3. На телефоне откройте собственную pair-ссылку из native-настройки, нажмите Start → `/menu` → панель. Компьютер, Docker Desktop, Desktop и окно `host` должны работать.

Во втором терминале проверьте `scripts/docker.py status` своим Python из `.venv`: требуется `host_connected: true`. Затем проверьте существующий чат и тестовое сообщение с телефона. Другому пользователю передавайте только ссылку на Release и эту страницу; токены, pair-ссылки, private-каталог, профиль Desktop и журналы не передаются.
