Инструкция первого запуска: **Windows + Samsung Galaxy, через Docker**. Для выпуска **v0.3.0-candidate**, проверено по коду 24.10.2026.

Результат: на Samsung вы открываете своего Telegram-бота и его панель, выбираете существующий локальный чат Codex Desktop на компьютере и отправляете сообщение.

**До начала:** нужны Windows x64 на Intel/AMD, доступ к локальному Codex в приложении OpenAI и собственный домен, уже подключённый к Cloudflare. Если домена нет, сначала потребуется его регистрация и [подключение к Cloudflare](https://developers.cloudflare.com/fundamentals/manage-domains/add-site/): текущий способ открытия панели использует именно такой домен. Windows ARM для этого адаптера не поддержан. На Samsung нужны только Telegram и Интернет. Это кандидат: полный запуск на физической связке Windows + Samsung пока не подтверждён.

Все команды ниже выполняются **на компьютере**, в **PowerShell**, обычным пользователем. Исключение — установка WSL, если её запросит Docker. Вставляйте строки по одной, нажимайте Enter и ждите завершения. При `FAIL`, `ok: false` или ошибке остановитесь на этом шаге. Пароли и токены вводите только в обозначенные скрытые поля: при вставке символов не видно — это нормально.

1. **Установите приложение OpenAI с локальным Codex.**

    Откройте [официальную страницу Windows-приложения](https://learn.chatgpt.com/docs/windows/windows-app), нажмите ссылку скачивания, затем «Установить». В документации приложение сейчас называется ChatGPT desktop; нужен доступный в нём **Codex с локальным проектом**. Войдите в собственный аккаунт. Обычный облачный чат ChatGPT для этой установки не подходит.

2. **Установите Docker Desktop.**

    Откройте [скачивание Docker Desktop для Windows](https://docs.docker.com/desktop/setup/install/windows-install/). Выберите **Windows x86_64**, запустите скачанный установщик. При выборе backend используйте **WSL 2**. Если потребуется перезагрузка, выполните её. Откройте Docker Desktop через «Пуск» и дождитесь готовности движка.

    В Docker Desktop нажмите шестерёнку **Settings → General**. Нужен включённый **Use the WSL 2 based engine**; если изменили настройку, нажмите **Apply & restart**. Для этого комплекта нужны **Linux containers**: это проверит команда в шаге 4. Параметр **Expose daemon on tcp://localhost:2375 without TLS**, если он показан, оставьте выключенным. [Описание настроек Docker Desktop](https://docs.docker.com/desktop/settings-and-maintenance/settings/).

    Если Docker сообщает, что WSL отсутствует или устарел: «Пуск» → найдите PowerShell → правой кнопкой → «Запуск от имени администратора». Для отсутствующего WSL выполните `wsl --install`, для установленного устаревшего — `wsl --update`. Перезагрузитесь, если предложено. Остальную настройку выполняйте в обычном PowerShell, без прав администратора. Актуальные требования Windows и компьютера указаны на странице Docker выше.

3. **Установите Python 3.12.**

    Откройте [Python для Windows](https://www.python.org/downloads/windows/), скачайте **Python install manager**, запустите его и нажмите «Установить». Откройте новое окно PowerShell через «Пуск». Выполните:

    ```powershell
    pymanager install 3.12
    py -3.12 --version
    ```

    Во второй строке должно появиться `Python 3.12...`. Если Python 3.12 уже установлен и эта проверка проходит, устанавливать его повторно не нужно. [Официальная инструкция Python install manager](https://docs.python.org/3/using/windows.html).

4. **Скачайте комплект инструмента и откройте PowerShell в его папке.**

    Нажмите [«Скачать ZIP v0.3.0-candidate»](https://github.com/suhang213-alt/codex-telegram-control/archive/refs/tags/v0.3.0-candidate.zip). В «Загрузках» нажмите ZIP правой кнопкой → «Извлечь всё». Выберите постоянное место, например «Документы» → `CodexTelegramTool`. Откройте распакованную папку, внутри которой видны **`scripts`**, **`adapters`** и **`compose.yml`**. Не работайте внутри самого ZIP. После настройки папку не перемещайте: подключение Desktop запоминает её путь.

    В верхней адресной строке Проводника вместо пути напишите `powershell` и нажмите Enter. Все дальнейшие команды вставляйте в это окно. Сначала проверьте:

    ```powershell
    py -3.12 scripts/verify_release.py
    docker info --format '{{.OSType}}'
    docker compose version
    ```

    Ожидается `passed: true` в JSON первой команды, затем `linux`, затем версия Docker Compose. Создайте локальное окружение:

    ```powershell
    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install .
    ```

5. **Создайте собственного Telegram-бота.**

    Чтобы удобно копировать данные, на компьютере откройте [Telegram Web](https://web.telegram.org/) и войдите в тот же Telegram-аккаунт, что на Samsung. Откройте [официального BotFather](https://t.me/BotFather), нажмите Start и отправьте `/newbot`. Введите название, затем свободный username с окончанием `bot`. BotFather выдаст токен — секретную строку для управления вашим ботом. Копируйте её только в скрытые запросы следующих шагов; не добавляйте в команды или файлы проекта. [Порядок создания бота Telegram](https://core.telegram.org/bots/tutorial#obtain-your-bot-token).

    В PowerShell выполните:

    ```powershell
    py -3.12 scripts/identify-owner.py
    ```

    Вставьте токен → Enter. PowerShell покажет временную команду `/identify ...`. Скопируйте **всю выведенную строку**, отправьте её в личный чат **своего нового бота** через Telegram Web. В PowerShell появится `Your numeric owner ID:` и число. Сохраните это число для шага 7. Молчание самого бота на этом шаге нормально.

6. **Подготовьте два локальных чата в Desktop.**

    В «Документах» Windows создайте папку `CodexTelegramTest`. В Codex добавьте её как локальный проект. Создайте в этом проекте два чата: **«Служебный Telegram»** и **«Тест управления»**. В тестовом чате отправьте «Ответь: тестовый чат готов» и дождитесь ответа.

    В служебном чате отправьте этот текст:

    ```text
    Выведи UUID этого чата из доверенного контекста или переменной CODEX_THREAD_ID. Если он недоступен, сообщи об этом; не придумывай UUID. Ничего не устанавливай и не меняй.
    ```

    Скопируйте полученный UUID. Если Codex сообщил, что UUID недоступен, здесь нужно остановиться: случайный UUID не подойдёт. Служебный чат задаёт контекст подключения; для переписки с телефона далее выбирается другой, тестовый чат.

    Откройте папку `CodexTelegramTest` в Проводнике, нажмите Ctrl+L и скопируйте полный путь. Оставьте Desktop открытым.

7. **Настройте связь с Desktop.**

    В PowerShell **папки инструмента** выполните:

    ```powershell
    .\.venv\Scripts\python.exe scripts/docker.py desktop-setup
    ```

    На вопросы отвечайте так:

    | Что написано в окне | Что ввести |
    |---|---|
    | `Type ACCEPT...` | `ACCEPT` → Enter |
    | `Your Telegram numeric user ID...` | Число из шага 5 |
    | `Dedicated existing local Desktop service chat UUID` | UUID служебного чата из шага 6 |
    | `Allowed existing workspace directory` | Полный путь к `CodexTelegramTest` |
    | `Codex home directory [default ~/.codex]` | Enter, если используется обычный профиль |
    | `Installed bundled codex-app-tools directory...` | Enter, если установлена единственная версия plugin |
    | `Enable the optional localhost.run tunnel?` | Просто Enter: в этой инструкции туннель запускает Docker |
    | `BotFather token...` | Токен своего бота → Enter, ввод скрыт |

    Затем:

    ```powershell
    .\.venv\Scripts\python.exe adapters/windows_desktop/scripts/doctor.py --native
    ```

    Продолжайте только при **`"ok": true`** и **`"native_verified": true`**. Если `native_plugin` показывает `FAIL`, смотрите [выбор установленного plugin](https://github.com/suhang213-alt/codex-telegram-control/blob/v0.3.0-candidate/adapters/windows_desktop/README.md#выбор-bundled-plugin). Чужие профили и plugin не копируйте.

8. **Подготовьте подключение к запросам разрешений Desktop.**

    Пока Desktop открыт, скопируйте **весь следующий блок** в PowerShell и выполните. Он ищет единственный процесс движка и передаёт его номер проверяющему сборщику:

    ```powershell
    & {
        $codexEngines = @(Get-CimInstance Win32_Process -Filter "Name = 'codex.exe'" | Where-Object { $_.CommandLine -match '\bapp-server\b' })
        if ($codexEngines.Count -ne 1) { throw 'Нужен один работающий Desktop engine. Остановитесь: процесс не выбран.' }
        .\.venv\Scripts\python.exe adapters/windows_desktop/gateway/build.py --engine-pid ($codexEngines[0].ProcessId)
    }
    ```

    Дождитесь успешного завершения. При ошибке о недоступном .NET compiler или несовместимом Desktop не продолжайте. Пока не закрывайте Desktop; запуск подготовленного подключения будет в шаге 12.

9. **Создайте HTTPS-туннель Cloudflare.**

    Войдите в [Cloudflare](https://dash.cloudflare.com/). Нужен ваш уже подключённый активный домен. Откройте **Networking → Tunnels → Create a tunnel**, задайте имя `codex-telegram`, создайте туннель. Если предлагают тип подключения, выберите **Cloudflared**; на экране установки выберите Docker, если этот вариант показан.

    В показанной команде найдите `--token`. Скопируйте только длинное значение после него, без самой команды, пробелов и кавычек. Это **токен туннеля**, не токен Telegram. Команду Cloudflare отдельно не запускайте: контейнер запустится ниже.

    Выберите будущий адрес панели: например, поддомен `codex` вашего собственного домена. Получится `https://codex.ВАШ-ДОМЕН`. В настройщик вставляйте **свой настоящий адрес** без пути `/panel`, а не этот пример. Окно Cloudflare оставьте открытым; если оно ждёт подключения туннеля, это нормально. [Официальные шаги Cloudflare](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/).

10. **Настройте и запустите Docker-компонент.**

    Выполните:

    ```powershell
    .\.venv\Scripts\python.exe scripts/docker.py setup
    ```

    | Вопрос | Что вставить |
    |---|---|
    | `Panel public HTTPS origin` | Свой полный HTTPS-адрес из шага 9 |
    | `Published runtime image version or digest` | Всю строку образа из блока ниже |
    | `BotFather token` | Тот же токен Telegram, что в шаге 7 |
    | `Your Cloudflare named-tunnel token` | Только токен туннеля из шага 9 |

    Строка образа для этого ZIP:

    ```text
    suhang213/codex-telegram-control@sha256:ac0200227a6ab66d8f5b0321958433cefa064c0e1fafa8eda63ecfcbce3cc2c4
    ```

    Выполните по одной строке, каждый раз дожидаясь успешного окончания:

    ```powershell
    .\.venv\Scripts\python.exe scripts/docker.py pull
    .\.venv\Scripts\python.exe scripts/docker.py check
    .\.venv\Scripts\python.exe scripts/docker.py up
    ```

    При `check` ошибка недопустима. Не исправляйте её открытием доступа к секретам «для всех».

    В Docker Desktop слева откройте **Containers**. Появится группа **`codex-telegram-desktop`**. Раскройте её: должны работать **`relay`** и **`tunnel`**. У `relay` после запуска ожидается `healthy`. Команда `up` запускает контейнеры в фоне, поэтому её окно можно использовать дальше; отдельное окно `host` из шага 13 остаётся работающим. Все параметры контейнеров уже заданы в скачанном `compose.yml` и приведены ниже.

11. **Подключите выбранный адрес к туннелю.**

    Вернитесь в Cloudflare. После соединения контейнера нажмите Continue, если эта кнопка показана. Откройте свой туннель → **Routes → Add route → Published application**. Укажите:

    | Поле | Значение |
    |---|---|
    | Subdomain | `codex`, если именно его выбрали в шаге 9 |
    | Domain | Ваш собственный домен |
    | Path | Оставить пустым |
    | Service / Type | `HTTP` |
    | URL | `relay:8787` |

    Если есть одно общее поле Service URL, вставьте `http://relay:8787`. Нажмите **Add route** или Save. Адрес должен совпадать с шагом 10. Туннель должен стать **Healthy**. Порт `8788` в Cloudflare не указывайте. Маршрут панели становится публичным; вход в неё проверяет сам бот по данным Telegram.

12. **Запустите Desktop через подготовленное подключение.**

    Дождитесь завершения задач Codex, сохраните работу и штатно закройте Desktop через меню приложения «Выход» / Quit. Если он остался в системном трее, завершите там. Не завершайте его принудительно через Диспетчер задач.

    В PowerShell выполните:

    ```powershell
    .\.venv\Scripts\python.exe scripts/docker.py desktop-launch
    ```

    Дождитесь открытия Desktop, затем:

    ```powershell
    .\.venv\Scripts\python.exe scripts/docker.py desktop-doctor
    ```

    Требуются **`"ok": true`** и **`"native_verified": true`**.

13. **Получите личную ссылку привязки и включите связь.**

    Выполните:

    ```powershell
    .\.venv\Scripts\python.exe adapters/windows_desktop/scripts/pair.py
    ```

    Появится ссылка `https://t.me/...?...`. Сохраните её для открытия на Samsung; не публикуйте и не пересылайте другим людям. Для переноса можно открыть ссылку в своём Telegram Web и найти того же бота на телефоне: если Start уже отправлен с компьютера, повторная привязка на Samsung не нужна.

    Теперь выполните:

    ```powershell
    .\.venv\Scripts\python.exe scripts/docker.py host
    ```

    Это окно будет занято работающей программой. **Не закрывайте его.** Откройте второе окно PowerShell из той же папки пакета способом из шага 4 и выполните:

    ```powershell
    .\.venv\Scripts\python.exe scripts/docker.py status
    ```

    Ожидается **`"host_connected": true`**. При `false` подождите несколько секунд и проверьте ещё раз; если значение не меняется, переходить к телефону рано. Это подтверждает соединение, но ещё не выполнение задания моделью.

14. **Откройте панель на Samsung и выполните первый тест.**

    Откройте личную ссылку из шага 13 **в Telegram**, нажмите **Start / Запустить**. Если вы нажали Start на компьютере, просто откройте того же бота на Samsung. Должно появиться сообщение «Связь с этим компьютером установлена».

    Отправьте `/menu`, нажмите **«Открыть панель»** или кнопку **Desktop**, если она отображается в меню бота. Открывайте панель именно из Telegram. В панели выберите **«Тест управления»** из разрешённой папки `CodexTelegramTest`. Служебный чат не выбирайте. Если удобнее, команда `/chats` выводит список чатов.

    Отправьте тестовое сообщение:

    ```text
    Ответь только: Связь работает. Не меняй файлы и не запускай команды.
    ```

    Успех: сообщение появилось в том же чате Desktop, ответ виден на Samsung, панель показывает этот чат. Один этот тест подтверждает базовую переписку; вложения и ответы на запросы разрешений проверяются отдельно по [приёмочному списку](https://github.com/suhang213-alt/codex-telegram-control/blob/v0.3.0-candidate/docs/MOBILE_ACCEPTANCE.md).

**Параметры запуска Docker — что именно применяет комплект.**

Для обычного запуска копируйте из шага 10 команды `scripts/docker.py pull`, `check`, `up` с указанным Python. Вспомогательная программа подставляет ваши настройки, проверяет права файлов и запускает Docker Compose. Параметры не нужно вводить в форму **Run** отдельного образа в Docker Desktop: она не создаёт весь состав установки и локальную связь с Desktop.

| Параметр | Значение в этом выпуске | Что это даёт пользователю |
|---|---|---|
| Проект Compose | `codex-telegram-desktop` | Одна группа из двух контейнеров в Docker Desktop |
| Файл запуска | `compose.yml` из распакованного ZIP | Согласованные настройки relay и туннеля |
| Relay image | Строка `suhang213/codex-telegram-control@sha256:...` из шага 10 | Загрузка точно проверенного образа |
| Tunnel image | `cloudflare/cloudflared:2026.10.3`, также закреплён по SHA-256 в настройщике | HTTPS-туннель запускается вторым контейнером |
| Режим запуска | Compose `up -d` | Контейнеры работают в фоне |
| Перезапуск обоих контейнеров | `restart: unless-stopped` | Возобновление после перезапуска Docker, если контейнер не был явно остановлен; компьютер и Docker должны работать |
| Пользователь обоих контейнеров на Windows | `10001:10001` | Процессы работают без root |
| Память relay / tunnel | `512m` / `256m` | Отдельные ограничения памяти контейнеров; это не общий расход RAM компьютера |
| Лимит процессов relay / tunnel | `128` / `64` | Ограничение числа процессов внутри контейнеров |
| Лимит CPU | Отдельно не задан в Compose | CPU использует в рамках общих ресурсов Docker Desktop |
| Защита обоих контейнеров | `read_only: true`, `cap_drop: ALL`, `no-new-privileges:true` | Корневая файловая система только для чтения, дополнительные привилегии отключены |
| Временная память relay | `/tmp`, до `32m`, `noexec,nosuid` | Временные данные не сохраняются как рабочая папка |
| Рабочее состояние relay | Локальный `state` подключён как `/data` | Служебные данные сохраняются между перезапусками |
| Готовность relay | Проверка каждые 20 секунд, тайм-аут 5 секунд, 3 попытки; стартовый период 10 секунд | Статус `healthy`; tunnel стартует после готовности relay |
| Журналы каждого контейнера | Драйвер `local`, до 2 файлов по `5m` | Ограничение объёма журналов |
| Штатная остановка relay | До 20 секунд | Время на завершение перед остановкой |

**Порты и адреса.**

| Адрес | Где используется |
|---|---|
| `https://codex.ВАШ-ДОМЕН` | Адрес панели на телефоне; вводится в настройщик и задаётся маршрутом Cloudflare |
| `http://relay:8787` | Cloudflare → relay внутри сети Compose; именно это значение задаётся в Service URL |
| `127.0.0.1:8788` на компьютере → `8788` контейнера | Защищённый канал локального компонента к relay; опубликован только на loopback, в Cloudflare его не направляют |
| `127.0.0.1:8787` на Windows | Исходный локальный обработчик Desktop; это отдельный процесс компьютера, не конфликт с внутренним портом контейнера |

Публичный порт relay `8787` основной Compose **не публикует на Windows**: к нему обращается только контейнер tunnel. Переадресация портов на роутере не требуется. Доступ с Samsung идёт через HTTPS и данные входа Telegram.

**Откуда берутся параметры и секреты.**

Настройщик создаёт `%LOCALAPPDATA%\CodexTelegramDocker`. Для открытия нажмите **Win+R**, вставьте этот путь и нажмите Enter. В `settings.json` находятся ваши настройки; в `secrets` — отдельные защищённые правами Windows файлы. Саму папку вручную до `setup` создавать не нужно. Не пересылайте её другому пользователю.

| Параметр Compose | Значение подставляет настройщик |
|---|---|
| `CTD_IMAGE` | Выбранный image digest |
| `CTD_CLOUDFLARED_IMAGE` | Полная закреплённая версия cloudflared |
| `CTD_PUBLIC_URL` | Ваш HTTPS-адрес панели |
| `CTD_OWNER_ID` | Ваш числовой Telegram ID |
| `CTD_DOCKER_HOME` | Путь к `%LOCALAPPDATA%\CodexTelegramDocker` |
| `CTD_UID`, `CTD_GID` | `10001`, `10001` на Windows |

Эти переменные передаются дочернему Docker Compose автоматически. Токены не передаются в них: relay читает `/run/secrets/telegram_bot_token` и `/run/secrets/connector_key`, tunnel — `/run/secrets/cloudflare_tunnel_token`. Отдельный ключ connector генерируется настройщиком. Профиль Codex и рабочая папка в контейнеры не подключаются. Контейнер relay получает токен бота и остаётся доверенной частью системы; используйте указанный проверенный образ.

Внутри relay используются параметры `--public-url`, `--owner-id`, `--token-file`, `--key-file`, `--state-dir`; их значения уже связаны с таблицами выше и `/data`. Tunnel запускается с `tunnel --no-autoupdate run --token-file /run/secrets/cloudflare_tunnel_token`.

**Какие действия Docker выполняют команды инструкции.** Это объяснение, а не дополнительный набор команд для вставки:

| Команда после `scripts/docker.py` | Что выполняется |
|---|---|
| `pull` | Compose `pull`: скачать два выбранных образа |
| `check` | Compose `run --rm --no-deps relay ... --self-check`: временно проверить доступ к секретам и состоянию без Desktop и Telegram |
| `up` | Повторить self-check, затем Compose `up -d` |
| `status` | Compose `ps`, затем проверка `host_connected` внутри relay |
| `down` | Compose `down --timeout 20`; секреты и native-настройки сохраняются |
| `desktop-launch`, `desktop-doctor`, `host` | Компонент Windows на компьютере; это не дополнительные контейнеры |

Во всех вызовах Compose helper задаёт `--project-directory` с корнем распакованного комплекта и `-f` с его `compose.yml`. Поэтому рабочий запуск выполняйте опубликованными командами helper: вместе с параметрами они обеспечивают обязательную проверку прав и секретов.

**Повторный запуск после перезагрузки.** Установку заново выполнять не нужно. Откройте Docker Desktop. Из папки комплекта выполните:

```powershell
.\.venv\Scripts\python.exe scripts/docker.py up
```

Дождитесь окончания задач и штатно закройте обычный Desktop, если он запущен. Затем выполните:

```powershell
.\.venv\Scripts\python.exe scripts/docker.py desktop-launch
```

После открытия Desktop выполните по очереди:

```powershell
.\.venv\Scripts\python.exe scripts/docker.py desktop-doctor
.\.venv\Scripts\python.exe scripts/docker.py host
```

Переходите к `host` только после успешного `desktop-doctor`. Окно `host` оставьте работающим. Для проверки связи во втором PowerShell используйте `status` из шага 13. Повторная привязка Telegram не нужна. Автозапуск всей установки этим руководством не настраивается.

Для остановки нажмите Ctrl+C в окне `host`, затем выполните:

```powershell
.\.venv\Scripts\python.exe scripts/docker.py down
```

Компьютер должен оставаться включённым и не спать. Samsung может использовать мобильный Интернет — общий Wi-Fi с компьютером не требуется. Не передавайте другим людям токены, ссылку привязки, папки `%LOCALAPPDATA%\CodexTelegramDesktop` и `%LOCALAPPDATA%\CodexTelegramDocker`, профиль Codex или рабочую переписку. Им передают только ZIP/ссылку выпуска и эту инструкцию; каждый настраивает собственные аккаунты.

При ошибке сохраните номер шага и текст ошибки без токенов и ссылок привязки. Не удаляйте секретные папки и не запускайте `setup` повторно наугад: Docker setup намеренно не перезаписывает готовые настройки.

Основание команд: [инструкция Docker выпуска](https://github.com/suhang213-alt/codex-telegram-control/blob/v0.3.0-candidate/docs/DOCKER.md), [Windows adapter](https://github.com/suhang213-alt/codex-telegram-control/blob/v0.3.0-candidate/adapters/windows_desktop/README.md), код `scripts/docker.py`, `scripts/identify-owner.py`, Windows `setup.py`, `doctor.py`, `pair.py` и gateway `build.py` из commit `0d8b8d406dcc2d981de1c82c93a524a39d5ffddd`. Инструкция сверена с кодом и официальными источниками; установка на компьютере первого пользователя в рамках подготовки документа не выполнялась.
