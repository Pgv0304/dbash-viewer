# DB ASH Viewer — демо-витрина на PythonAnywhere

Показ возможностей приложения на https://pgv0304.pythonanywhere.com/ вместо Библиотеки:
две синтетические базы (Oracle и PostgreSQL) с имитацией нагрузки, вход без пароля.
Подключиться к настоящим базам отсюда нельзя: бесплатный тариф PythonAnywhere не пускает
наружу подключения к портам баз (1521, 5432). Полная версия — https://84-54-28-45.sslip.io:96/.

DB ASH Viewer написан на FastAPI и собирает данные в фоновых потоках, поэтому он запускается
не как обычное веб-приложение PythonAnywhere (WSGI — потоки там не работают), а как
**ASGI-сайт** (бета-функция PythonAnywhere, команда `pa website`).

## Что в архиве

| Файл | Назначение |
|---|---|
| `app/` | код приложения |
| `requirements.txt` | зависимости |
| `config/targets.json` | две демо-базы, история хранится 6 часов |
| `run.sh` | запуск uvicorn на сокете, который выдаёт PythonAnywhere |

## Шаги

Всё делается в кабинете https://www.pythonanywhere.com/ под пользователем `pgv0304`.

**1. Сохранить Библиотеку.** Вкладка **Files** → откройте `library-app/library_web_lite/` и скачайте
`library.db` (база библиотеки). Код Библиотеки остаётся на GitHub (`Pgv0304/library-app`).

**2. Отключить Библиотеку.** Вкладка **Web** → внизу страницы **Delete pgv0304.pythonanywhere.com**.
Адрес должен освободиться: ASGI-сайт не может занять его, пока там работает WSGI-приложение.
Файлы в `~/library-app` при этом не удаляются.

**3. Загрузить архив.** Вкладка **Files** → **Upload a file** → `dbash-demo-pythonanywhere.zip`
(в домашнюю папку `/home/Pgv0304/`).

**4. Распаковать и поставить зависимости.** Вкладка **Consoles** → **Bash**:

```bash
cd ~ && unzip -o dbash-demo-pythonanywhere.zip
mkvirtualenv --python=/usr/bin/python3.10 dbash-demo
pip install -r ~/dbash-demo/requirements.txt
pip install --upgrade pythonanywhere
```

Сообщение об ошибке `typing-extensions` при установке `pythonanywhere` можно не замечать — так
сказано в документации PythonAnywhere.

**5. Получить API-токен** (нужен команде `pa`): вкладка **Account** → **API token** → **Create a new API token**.

**6. Создать сайт** в той же консоли Bash:

```bash
pa website create --domain pgv0304.pythonanywhere.com --command '/bin/bash /home/Pgv0304/dbash-demo/run.sh'
```

**7. Проверить.** Откройте https://pgv0304.pythonanywhere.com/ — должна появиться заставка DB ASH Viewer,
затем график демо-базы Oracle. Первые секунды после создания сайт может отвечать 404 — это известная
особенность беты, обновите страницу.

## Обслуживание

```bash
pa website get --domain pgv0304.pythonanywhere.com      # состояние
pa website reload --domain pgv0304.pythonanywhere.com   # перезапуск после обновления кода
pa website delete --domain pgv0304.pythonanywhere.com   # удалить витрину
```

Обновить код: загрузить новый архив, распаковать поверх (`unzip -o`), `pa website reload …`.
Журнал запуска — `~/dbash-demo/data/run.log`; журналы сайта — на вкладке **Files**, папка `/var/log/` (файлы с именем домена).

## Если что-то не так

- `pa website create` отвечает, что ASGI недоступен на этом тарифе — бета может быть открыта не всем
  бесплатным аккаунтам. Тогда остаётся вернуть Библиотеку: вкладка **Web** → **Add a new web app**
  по README Библиотеки (`library_web_lite/README.md`, раздел про PythonAnywhere).
- Ошибка «domain already exists» — WSGI-приложение Библиотеки ещё не удалено (шаг 2).
- Сайт открывается, но график пуст — подождите минуту: при старте демо-базы дописывают историю за последний час.

## Вернуть Библиотеку

```bash
pa website delete --domain pgv0304.pythonanywhere.com
```

Затем вкладка **Web** → **Add a new web app** → Manual configuration, Python 3.10, и дальше по шагам 5–8
README Библиотеки: virtualenv `library-lite-venv` и WSGI-файл уже есть, их можно указать заново.
