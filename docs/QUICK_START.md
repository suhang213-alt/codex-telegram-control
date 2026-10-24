# Короткий порядок установки

Скачайте ZIP из **Releases**, распакуйте его и используйте собственные аккаунты. Это кандидат: реальный Mac + Samsung ещё не проверен. Настройки и секреты прежнего владельца не нужны.

## Подготовка для обеих ОС

1. Установите официальный Desktop, войдите в свой аккаунт и откройте локальный чат в отдельной рабочей папке. Нужен Python **3.12 для Windows**, **3.11+ для Mac**.
2. В Telegram откройте официальный **BotFather**, отправьте `/newbot`, создайте бота. Скопируйте токен: вставлять его нужно только в скрытый запрос настройщика.
3. Откройте терминал в распакованной папке. Windows: в адресной строке Проводника напишите `powershell`, нажмите Enter. Mac: в Terminal напишите `cd `, перетащите папку в окно, нажмите Enter.
4. В Windows вставьте `py -3.12 scripts/identify-owner.py`, на Mac — `python3 scripts/identify-owner.py`. Введите токен скрыто, отправьте показанную случайную команду `/identify …` своему боту. Скопируйте выведенное число в поле `owner/user ID` при настройке.

## macOS → Samsung / Android или iPhone

1. Установите `cloudflared` по [официальной инструкции](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/). В отдельном Terminal вставьте:

```sh
cloudflared tunnel --url http://127.0.0.1:8787
```

Скопируйте выданный адрес `https://…trycloudflare.com`; окно оставьте открытым.

2. В терминал папки пакета вставьте:

```sh
bash scripts/install-macos-desktop.command
```

Введите свой **Telegram ID**, **путь рабочей папки Desktop**, **HTTPS-адрес туннеля** и скрыто **токен BotFather**. Для обычной единственной установки приложения и профиля нажмите Enter. Токен сохранится в Keychain.

3. Дождитесь окончания задач Desktop и закройте приложение через **Cmd+Q**. Последовательно вставьте:

```sh
.venv/bin/codex-telegram-desktop launch
.venv/bin/codex-telegram-desktop doctor
.venv/bin/codex-telegram-desktop run
```

Перед `run` дождитесь открытия Desktop и результата `gateway: connected` у `doctor`. При отказе следуйте [инструкции Mac](INSTALL_MACOS.md), не отключайте проверки.

4. На телефоне откройте своего бота: `/chats` → `/use номер` → `/panel`. Выберите прежний чат. Mac, Desktop и оба терминала должны оставаться включёнными.

## Windows → Android или iPhone

1. В Desktop создайте отдельный локальный служебный чат. Попросите Codex: **«Выведи UUID этого чата из доверенного контекста или переменной CODEX_THREAD_ID; если он недоступен, сообщи об этом, не придумывай»**. Скопируйте UUID для поля `Dedicated existing local Desktop service chat UUID`. Его существование проверит `doctor`.

2. В PowerShell папки пакета вставьте:

```powershell
py -3.12 adapters/windows_desktop/scripts/setup.py
py -3.12 adapters/windows_desktop/scripts/doctor.py --native
```

Введите `ACCEPT`, свой **Telegram ID**, **UUID служебного чата**, **путь разрешённой рабочей папки** и скрыто **токен**. Для обычного профиля и единственной версии bundled plugin нажмите Enter. Для панели на телефоне в вопросе о туннеле введите `ENABLE`; требуется установленный OpenSSH. При нескольких версиях plugin выберите совместимую установленную версию по [инструкции Windows](../adapters/windows_desktop/README.md).

3. Для настоящих запросов разрешений Desktop настройте [gateway](../adapters/windows_desktop/README.md#ответы-на-настоящие-запросы-разрешений-desktop). Пока Desktop открыт, получите список движков:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'codex.exe'" | Where-Object { $_.CommandLine -match '\bapp-server\b' } | Select-Object ProcessId,ParentProcessId
```

При единственном движке скопируйте число `ProcessId` вместо `PID` ниже. При нескольких процессах определите дочерний процесс именно Desktop по `ParentProcessId`; случайное число не выбирайте.

```powershell
py -3.12 adapters/windows_desktop/gateway/build.py --engine-pid PID
```

Сборщик проверит родителя и идентичность процесса. После успешной сборки дождитесь окончания задач и штатно закройте Desktop. Затем:

```powershell
py -3.12 adapters/windows_desktop/gateway/activate.py
py -3.12 adapters/windows_desktop/scripts/doctor.py --native
```

Не продолжайте при `FAIL` или неподтверждённом native protocol.

4. Создайте личную привязку и запустите мост:

```powershell
py -3.12 adapters/windows_desktop/scripts/pair.py
py -3.12 adapters/windows_desktop/scripts/start.py
```

После запуска `start.py` откройте на телефоне **личную ссылку, напечатанную `pair.py`**, нажмите Start и откройте панель. Ссылку привязки никому не пересылайте. Оставьте компьютер, Desktop и PowerShell включёнными.

## Передача следующему пользователю

Передайте только ссылку на репозиторий/Release и эту инструкцию. Каждый создаёт своего бота и вводит собственные значения. Не копируйте профиль Desktop, историю, каталог настроек, ключи или токены. Перед рабочими данными выполните [приёмку](MOBILE_ACCEPTANCE.md).
