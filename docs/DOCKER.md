# Docker и существующий Codex Desktop

**Кандидат для приёмки.** Контейнерный режим требует приложения Desktop и небольшого компонента на том же компьютере. Реальная приёмка Docker Desktop + личный аккаунт + физический Samsung пока не выполнена. Наличие образов `linux/amd64` и `linux/arm64` не доказывает совместимость всех версий macOS/Windows.

Контейнер выполняет запросы к Telegram Bot API и передаёт HTTP-запросы панели существующему локальному обработчику. Логика бота, проверка разрешённых чатов, файлов, решений и журнал операций остаются в компоненте Mac/Windows. Он сам устанавливает исходящее соединение с локальным портом контейнера. Отдельный Codex CLI не запускается; профиль Desktop и рабочая папка не монтируются.

Контейнер — **доверенная часть бота**: у него есть BotFather token, поэтому он способен сформировать данные Telegram от имени владельца. Повторная проверка подписи на компьютере не защищает от вредоносного контейнера. Используйте проверенный образ по digest. Ограничения рабочей папки и исходные разрешения Desktop проверяются на компьютере и остаются обязательными.

## 1. Подготовьте свои компоненты

- Mac: официальный Desktop с выполненным входом, Python 3.11+ и Docker Desktop в режиме Linux-контейнеров. Поддержка Mac Intel остаётся кандидатом до отдельной проверки; Apple Silicon также требует проверки именно Docker-сценария.
- Windows: Windows x64, Python 3.12, Docker Desktop с Linux-контейнерами и совместимый native Desktop adapter. Windows ARM64 для native adapter не заявлен.
- Собственный новый Telegram-бот, числовой ID владельца, публичный HTTPS hostname и named tunnel Cloudflare. Получение ID описано в [INSTALL_MACOS](INSTALL_MACOS.md). Секреты не отправляйте в чат и не записывайте в исходники.
- Полный исходный архив **той же версии**, что runtime-образ: в нём находятся компонент компьютера и установочные команды. Только `docker pull` не устанавливает компонент Mac/Windows.

В Cloudflare создайте свой remotely-managed tunnel и настройте published hostname на HTTP service **`http://relay:8787`**. Этот адрес работает между контейнерами Compose. Публичный маршрут должен вести только на этот сервис; порт `8788` не публикуется через туннель. Токен туннеля потребуется один раз при скрытом вводе. [Официальная настройка туннеля](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/).

Публичный адрес панели укажите одинаково в native setup и Docker setup. Нативный бот и контейнерный режим не должны одновременно опрашивать один токен: перед переключением остановите прежний `run`/`start.py` через Ctrl+C.

## 2. Установите компонент и сохраните секреты

На Mac откройте терминал в папке скачанного пакета:

```sh
sh scripts/docker-install.command
```

Installer создаёт `.venv`, устанавливает пакет и запускает native setup, если его ещё нет. Существующую native-конфигурацию он сохраняет. Затем Docker setup запросит **опубликованный runtime image tag или digest**, тот же BotFather token и token вашего туннеля. Пока runtime-кандидат не опубликован, этот шаг завершать нечем: `package-check` и `latest` helper намеренно не принимает. Адрес и digest рабочего кандидата должны быть указаны в его GitHub Release; не подставляйте образ подготовительной проверки.

На Windows из PowerShell можно выполнить команды без изменения политики выполнения скриптов:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe scripts/docker.py desktop-setup
.\.venv\Scripts\python.exe scripts/docker.py setup
```

Есть и `scripts/docker-install.ps1`, если ваша политика разрешает обычное выполнение PowerShell-скриптов. Native setup Windows требует отдельного служебного локального чата и совместимого bundled plugin. В нём оставьте прежний tunnel provider выключенным; HTTPS обслуживает Cloudflare-контейнер. Для ответов на исходные разрешения Desktop предварительно выполните [подготовку Windows gateway](../adapters/windows_desktop/README.md#ответы-на-настоящие-запросы-разрешений-desktop). Docker не заменяет эту проверку.

Docker-файлы установки сохраняются отдельно:

| Платформа | Каталог |
|---|---|
| Mac | `~/Library/Application Support/codex-telegram-docker` |
| Windows | `%LOCALAPPDATA%/CodexTelegramDocker` |

Внутри: `settings.json`, отдельные `secrets/telegram_bot_token`, `secrets/connector_key`, `secrets/cloudflare_tunnel_token` и `state/`. На Mac файлы создаются с правами `0600`, каталоги — `0700`; на Windows каталог ограничивается ACL текущего пользователя, а owner/DACL повторно проверяются при загрузке. Setup требует **новый, ещё не существующий каталог**, чтобы не сохранить чужие права от прежней папки, и не заменяет существующие секреты. `--home` позволяет выбрать другой отдельный каталог вне репозитория и профиля Desktop; передавайте его во всех последующих командах.

Секрет бота остаётся также в native Keychain/DPAPI: он нужен исходному обработчику для проверки Telegram initData. Helper не извлекает его из хранилища, а просит повторный скрытый ввод. Ключ канала генерируется отдельно и не совпадает с внутренним native gateway token. Compose получает secrets как файлы, не через `ARG`, `ENV` или аргументы команды.

## 3. Запустите контейнеры

Далее пример для Mac. На Windows заменяйте `.venv/bin/python` на `.\.venv\Scripts\python.exe`:

```sh
.venv/bin/python scripts/docker.py pull
.venv/bin/python scripts/docker.py check
.venv/bin/python scripts/docker.py up
```

`pull` скачивает явно указанную версию. `check` проверяет конфигурацию, чтение файлов secrets и запись служебного состояния **под непривилегированным пользователем контейнера**, без Telegram, Desktop и модели. `up` повторяет проверку перед запуском.

На Mac Compose использует UID/GID текущего пользователя; на Windows — UID/GID `10001`. Для файловых secrets Compose не переносит объявленные `uid/gid/mode`, поэтому успешность реального `check` обязательна. При отказе не делайте секреты общедоступными и не запускайте контейнер под root: сохраните сообщение об ошибке и проверьте права файлов/совместимость Docker Desktop. [Особенности Compose secrets](https://docs.docker.com/reference/compose-file/services/#secrets).

Контейнер имеет `read_only`, `cap_drop: ALL`, `no-new-privileges`, ограниченный tmpfs и единственный записываемый каталог данных своего relay. Docker socket, устройства, профиль `.codex`, базы Desktop и разрешённая рабочая папка ему не предоставляются. Relay получает только два своих секрета; tunnel — только секрет Cloudflare.

Контейнерному relay выделено до 512 МБ памяти. Транспорт принимает до 21 МБ декодированного тела; более строгие ограничения исходного native handler сохраняются. Превышение лимита возвращает явный отказ, а не обрезанный файл.

## 4. Запустите native host

На Mac дождитесь завершения активных задач и штатно закройте Desktop. Затем:

```sh
.venv/bin/python scripts/docker.py desktop-launch
.venv/bin/python scripts/docker.py desktop-doctor
.venv/bin/python scripts/docker.py host
```

`desktop-launch` не завершает запущенные процессы: при уже открытом Desktop запуск будет отклонён. `desktop-doctor` выполняет native compatibility check без задания модели. `host` остаётся в терминале; он включает relay только для этого процесса. Системные переменные и автозагрузка не меняются.

На Windows `desktop-launch` использует заранее подготовленный native gateway; без подготовки команда откажет. `desktop-doctor` повторяет native check, а `host` запускает прежний Windows adapter с relay transport. Обычный старт Desktop без gateway поддерживает только ту часть возможностей, которую подтверждает адаптер; не считайте ответы на исходные разрешения проверенными без gateway-приёмки.

Во втором терминале:

```sh
.venv/bin/python scripts/docker.py status
```

`healthy` означает работу контейнера. `host_connected: true` означает свежую проверенную связь с локальным обработчиком; это не доказательство успешного входа в модель или выполнения задания. При несовпадении owner, bot token или public URL handshake должен отклоняться. Пока host не подключён, запросы к нему недоступны; отдельный CLI не используется как запасной путь.

На Samsung откройте собственного бота. Для Mac: `/chats` → `/use номер` → `/panel`. Для Windows сначала выполните одноразовую привязку по инструкции адаптера, затем `/menu`. Выберите уже существовавший чат в разрешённой папке. Новые чаты создавайте в Desktop. Первая реальная отправка расходует лимиты аккаунта и выполняется только как явная приёмка владельца.

## Остановка, обновление и проверка

Сначала Ctrl+C в терминале `host`, затем:

```sh
.venv/bin/python scripts/docker.py down
```

Desktop остаётся открытым. Команда не удаляет secrets, native-настройки или журналы. Для возврата к запуску Desktop без shim штатно закройте приложение и выполните `desktop-rollback`. Для обычного режима бота используйте исходную native-команду без relay-переменных; не запускайте два polling-владельца одновременно.

При обновлении сохраните private-каталог, скачайте согласованный исходный архив, установите его в `.venv`, выберите проверенный новый image digest в private `settings.json`, затем `pull`, `check`, `up` и повторите native/мобильную приёмку. Профиль Desktop и рабочие данные не копируются. После обновления самого Desktop его hash/signature gate может потребовать новой настройки адаптера; не отключайте проверку ради запуска.

Offline-проверки упаковки:

```sh
python3 -B scripts/docker_tests.py
docker build --target runtime -t codex-telegram-control:runtime-smoke .
python3 -B scripts/docker_tests.py --container-smoke
```

Последняя команда использует только искусственные secrets, сеть `none`, отдельный временный state, проверяет nonroot/read-only/состав mounts и удаляет только созданный ей тестовый контейнер. Она не подтверждает native macOS/Windows, настоящий Telegram, Cloudflare или физический Android. Для выпуска нужны также проверки из [MOBILE_ACCEPTANCE](MOBILE_ACCEPTANCE.md): исходный чат, двусторонняя история, файл, разрешение/отказ, остановка, разрыв связи и отсутствие повторной мутации при неизвестном результате.

`compose.local.yml` — дополнительный файл только для локальной диагностики: он открывает public listener на `127.0.0.1:8789`. Основной Compose не публикует public listener на хосте; native backend сохраняет собственный `127.0.0.1:8787`. Управляющий порт `8788` нельзя включать в туннель или проксировать наружу.
