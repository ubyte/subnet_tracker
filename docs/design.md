# subnet-tracker: дизайн-документ

Статус: черновик v6. Учтены ответы на шесть кругов вопросов. Неподтверждённые решения перечисляются в разделе «Открытые вопросы».

## 1. Назначение и границы

Split-tunnel по доменам: комплекс отслеживает IP-адреса заданных доменов и держит у клиентов OpenVPN маршруты в сети, где эти адреса находятся. Трафик к остальным адресам идёт мимо VPN.

Стек сервера: Python 3.12, SQLite 3.37+ (STRICT), Flask, Linux, systemd. Агент на клиентах: Go (раздел 7). Интерфейс web UI, CLI и агента на английском.

Окружение:
- сервер OpenVPN 2.7 на Linux; комплекс работает на том же хосте. Сервер может быть IPv4-only, IPv6-only или dual-stack;
- клиенты OpenVPN 2.6: Android, Keenetic OS, Windows; их 1–10;
- администратор открывает web UI с клиентского устройства через тот же VPN или на самом сервере через localhost; других путей на старте нет. Пароль при этом не спрашивается (раздел 14, «Доступ»);
- агент резолва работает на Windows 10+ x86-64 и Linux x86-64.

Масштаб: около 125 доменов, введённых вручную, и до 5000, загруженных пакетно.

Не входит в v1:
- wildcard-домены (`*.example.com`): DNS не перечисляет поддомены;
- рендеринг JavaScript при сканировании страниц;
- разные наборы маршрутов для разных клиентов;
- доставка маршрутов подключённым клиентам без переподключения (раздел 3.2);
- Windows-служба для агента;
- маршрутизация через подмену DNS (fake-IP), см. раздел 20.

## 2. Термины

| Термин | Значение |
|---|---|
| Домен | То, что добавил пользователь. Отображается и экспортируется под именем, которое пользователь ввёл первым (каноничное имя). Настройки, группы и теги — у домена. В DNS у домена два имени: каноничное и его www-пара. |
| Имя | DNS-имя домена: каноничное и его www-пара. Хранится в нижнем регистре, в punycode, без точки на конце. У каждого имени свой резолв, своя история и свои адреса. |
| Группа | Именованный набор доменов и глобальных маршрутов с настройками (префикс, вкл/выкл). Домен может входить в несколько групп. Большинство доменов не входит ни в одну: в UI это «No group» — безымянная группа, которую нельзя выключить; её префикс — значение по умолчанию из настроек. |
| www-пара | Второе имя домена: каноничное с `www.` или без него (раздел 9.4). Отдельно не добавляется, не меняется и не удаляется — только вместе с доменом. |
| Партия (batch) | Одно добавление: вставка в форму, сканирование, импорт или команда CLI. По ней работают Undo и «правка группы и тегов потом». |
| Тег | Свободная метка без настроек. |
| Резолвер | Способ получения адресов: системный резолвер сервера, DNS-сервер, DoH, браузер, агент. Опрашиваются все; маршруты строятся только по отмеченным. |
| Внешний резолвер | Резолвер, который сам забирает задания с сервера: браузер или агент. |
| Агент (`st-agent`) | Программа на устройстве пользователя: забирает задания, резолвит системным резолвером устройства, отдаёт результат. Каждый агент — отдельный резолвер. |
| Адрес | IP, полученный для имени хотя бы одним резолвером. |
| Наблюдение (sighting) | Факт «резолвер R вернул адрес A», с временем первого и последнего получения. |
| Глобальный маршрут | CIDR, заданный вручную. Отдаётся всем клиентам как есть, включая частные сети за сервером, без исключений и агрегации. |
| Сеть | Маршрут, полученный из адреса: адрес, расширенный до префикса (по умолчанию /23 для IPv4). |
| План маршрутов | Итоговый набор маршрутов с объяснением, откуда взялся каждый. |
| Применение (apply) | Запись файла маршрутов и доставка изменений клиентам OpenVPN. |
| Присутствие (presence) | Признак того, что администратор сейчас работает в web UI. Пока оно активно, переподключение клиентов откладывается (раздел 8.5). |

## 3. Интеграция с OpenVPN

На сервере пока ничего не настроено, поэтому интеграцию проектируем с нуля.

### 3.1 Новые подключения

Маршруты передаются строками `push "route ..."` из файла, который сервер читает при каждом подключении клиента. Файл один для всех клиентов. Его в динамический конфиг клиента дописывает наш `client-connect`-скрипт:

```
# фрагмент server.conf, который ставит install.sh
management /run/subnet-tracker/ovpn-server.sock unix
management-client-group subnet-tracker
script-security 2
client-connect /usr/lib/subnet-tracker/ovpn-client-connect
```

Include через `config` в `server.conf` не подходит: он читается только при старте или SIGHUP, а SIGHUP рвёт соединения всех клиентов. `ccd/DEFAULT` тоже не используем: он применяется только к клиентам без собственного ccd-файла.

Ручные `push "route ..."` в `server.conf` продолжают работать: наш скрипт добавляет маршруты к ним. Но управлять ручными маршрутами удобнее из UI, как глобальными маршрутами.

Отдавать ли маршруты IPv4 и IPv6, задают два флага: `routes.push_ipv4` и `routes.push_ipv6` (раздел 8.2). Комплекс сам не проверяет, есть ли на сервере нужный стек: если его нет, администратор выключает соответствующий флаг.

### 3.2 Уже подключённые клиенты

Сервер 2.7 умеет `PUSH_UPDATE`, но клиенты 2.6 его не поддерживают. К тому же update заменяет все маршруты целиком, а одна management-команда ограничена ~1 КБ. Поэтому единственный способ доставить изменения — переподключить клиента: `client-kill <cid>` отправляет ему RESTART, клиент возвращается и получает новый список.

Переподключение затрагивает и устройство самого администратора, через которое открыт web UI, и Keenetic, за которым целая сеть. Поэтому оно никогда не происходит, пока администратор работает в UI, если только он явно не нажал «Apply now». Правила и три режима применения — в разделе 8.5.

## 4. Архитектура

```mermaid
flowchart LR
  browser[Browser JS<br/>DoH fetch, presence] -- tasks / results / heartbeat --> web
  agent[st-agent<br/>Windows / Linux] -- mTLS: tasks / results --> agentapi
  web[Web UI<br/>Flask + waitress] --> core
  agentapi[Agent API<br/>stdlib https + mTLS] --> core
  cli[CLI] --> core
  core[core: services, validation] --> db[(SQLite WAL, STRICT)]
  web -. trigger .-> path[tick.path]
  cli -. trigger .-> path
  agentapi -. trigger .-> path
  timer[tick.timer<br/>every minute] --> tick
  path --> tick[subnet-tracker tick]
  tick --> resolver[server resolvers<br/>system / dns / doh] --> db
  tick --> planner[routes: plan] --> apply[openvpn: apply]
  apply --> file[routes.conf]
  apply --> mgmt[management socket]
  file --> ovpn[OpenVPN<br/>client-connect]
```

Пакет `subnet_tracker` с одной точкой входа `subnet-tracker` и подкомандами; агент — отдельная программа в каталоге `agent/`.

| Модуль | Ответственность |
|---|---|
| `core` | Схема БД, миграции, типизированные репозитории и сервисы доменов, групп, тегов, резолверов, агентов, глобальных маршрутов и настроек, журнал событий. Единственный код, который пишет в БД. |
| `core/external.py` | Общая логика внешних резолверов: выдача заданий, аренда, приём результатов, учёт сессий. Её используют и web (браузер), и Agent API. |
| `resolver` | Серверные резолверы, классификация ответов (в том числе DoH JSON от браузера), истечение наблюдений. |
| `routes` | Чистые функции: адреса → сети → исключения → агрегация → бюджет → план → текст файла. |
| `openvpn` | Атомарная запись файла, management-клиент, решение о переподключении (чистая функция) и его выполнение. |
| `discovery` | Разбор вставленного текста, скачивание страниц, извлечение доменов. |
| `textio` | Формат экспорта и импорта, diff. |
| `pki` | Генерация CA агентов, серверного сертификата и клиентских сертификатов. |
| `agentapi` | HTTPS-сервер для агентов с обязательным клиентским сертификатом. |
| `cli`, `web` | Тонкие обёртки над `core`. Web UI покрывает все функции CLI, кроме служебных (`tick`, `db migrate`, `pki init`). |
| `web/static/*.js` | `presence.js` (heartbeat и уход со страницы), `browser-dns.js` (транспорт DoH-запросов), `page.js` (Enter в форме, фильтр строк, статус итога). Логики разбора в JS нет. |
| `agent/` (Go) | `st-agent` (раздел 7). |

### 4.1 Процессы и systemd

| Unit | Назначение |
|---|---|
| `subnet-tracker-web.service` | Web UI (waitress). Пользователь `subnet-tracker`. Прав на OpenVPN нет. `After=openvpn-server@server`, `Restart=on-failure`: адрес туннеля появляется только после старта OpenVPN. |
| `subnet-tracker-agent-api.service` | Agent API (раздел 7.4). Пользователь `subnet-tracker`. Тот же порядок запуска. |
| `subnet-tracker-tick.timer` | `OnCalendar=minutely`, запускает `tick.service`. |
| `subnet-tracker-tick.service` | `Type=oneshot`, выполняет `subnet-tracker tick`. |
| `subnet-tracker-tick.path` | `PathModified=/run/subnet-tracker/trigger`. Web, CLI и Agent API дописывают строку в этот файл, чтобы `tick` запустился сразу, не дожидаясь таймера. |

«Обновить адреса» и «обновить маршруты» — отдельные команды (`resolve`, `apply`), но по расписанию их последовательно запускает один `tick`. Так нет гонок и задержки между ними. Интервал резолва хранится в БД: таймер срабатывает раз в минуту, а `tick` сам решает, пора ли резолвить, поэтому интервал меняется из UI без правки unit-файлов.

Алгоритм `tick` (выполняется под `flock /run/subnet-tracker/tick.lock`):

```
loop:
  обслужить запросы из таблицы requests (resolve_domains, apply_now, ...)
  если пора — следующая порция полного резолва серверными резолверами (500 имён)
  если порций полного резолва не осталось или их нет — выйти из цикла
истечение наблюдений (раздел 6.4)
plan = plan_routes(снимок БД, настройки, now)
записать файл, если план изменился; при добавлении маршрутов — поставить отложенное переподключение
пока есть отложенное переподключение, его срок наступает не позже чем через 55 с после старта tick:
  ждать, перечитывая reconnect_state и requests каждые 2 с
  срок наступил — переподключить клиентов
снимок подключённых клиентов из management → ovpn_clients
раз в сутки: чистка истории
```

Полный резолв 5000 доменов занимает минуты (раздел 6.3), поэтому он идёт порциями, а между порциями `tick` обслуживает новые запросы. Ожидание срока переподключения внутри `tick` нужно потому, что сроки считаются в секундах (раздел 8.5), а таймер срабатывает раз в минуту. Каждый `tick` покрывает ожиданием почти всю следующую минуту, и переподключение происходит вовремя без таймера с шагом в секунды.

### 4.2 Файлы и права

| Путь | Владелец | Содержимое |
|---|---|---|
| `/etc/subnet-tracker/config.toml` | root, 0640, группа `subnet-tracker` | Начальная конфигурация (раздел 13.1). |
| `/var/lib/subnet-tracker/state.db` | `subnet-tracker` | БД. Каталог `g+ws`, чтобы CLI работал у пользователей группы `subnet-tracker`. |
| `/var/lib/subnet-tracker/pki/` | `subnet-tracker`, 0700 | `agent-ca.key`, `agent-ca.pem`, `server.key`, `server.pem`. |
| `/opt/subnet-tracker/agent/` | root | Собранные бинарники `st-agent` и unit-файлы для скачивания из web UI. |
| `/etc/openvpn/server/subnet-tracker/routes.conf` | `subnet-tracker`, 0644 | Сгенерированный файл маршрутов; рядом `routes.conf.prev`. Должен читаться `client-connect`-скриптом, который работает после сброса привилегий OpenVPN. |
| `/usr/lib/subnet-tracker/ovpn-client-connect` | root | Дописывает `routes.conf` в `$1`. |
| `/run/subnet-tracker/` | tmpfiles.d, 0770, группа `subnet-tracker` | `trigger`, `tick.lock`, management-сокет OpenVPN. OpenVPN создаёт сокет как root до сброса привилегий. |

Пути OpenVPN задаются только в `config.toml`, а не в БД: web UI не должен иметь возможность заставить `tick` писать в произвольный файл.

## 5. Модель данных

SQLite с `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`. Все таблицы `STRICT`: у каждого столбца объявленный тип, SQLite не принимает значения другого типа. Перечисления, флаги и JSON дополнительно ограничены `CHECK`. Время хранится как `INTEGER` — секунды Unix UTC. Миграции — нумерованные SQL-файлы, версия схемы в `PRAGMA user_version`.

Перечисления в `CHECK` совпадают со `StrEnum` в Python (раздел 15); тест сверяет их.

```sql
CREATE TABLE groups (
  id          INTEGER PRIMARY KEY,
  name        TEXT    NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
  description TEXT    NOT NULL DEFAULT '',
  prefix_v4   INTEGER CHECK (prefix_v4 BETWEEN 8 AND 32),
  prefix_v6   INTEGER CHECK (prefix_v6 BETWEEN 16 AND 128),
  enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
) STRICT;

CREATE TABLE add_batches (                  -- одно добавление: вставка, сканирование, импорт, команда CLI
  id         INTEGER PRIMARY KEY,
  created_at INTEGER NOT NULL,
  kind       TEXT    NOT NULL CHECK (kind IN ('paste', 'scan', 'import', 'cli')),
  actor      TEXT    NOT NULL,
  input      TEXT    NOT NULL,              -- исходный текст, обрезанный до 64 КБ
  added      INTEGER NOT NULL,
  duplicates INTEGER NOT NULL,
  invalid    TEXT    NOT NULL CHECK (json_valid(invalid)),   -- нераспознанные токены
  undone_at  INTEGER
) STRICT;

CREATE TABLE domains (                      -- то, что добавил пользователь; настройки общие для всех имён домена
  id         INTEGER PRIMARY KEY,
  prefix_v4  INTEGER CHECK (prefix_v4 BETWEEN 8 AND 32),
  prefix_v6  INTEGER CHECK (prefix_v6 BETWEEN 16 AND 128),
  enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  comment    TEXT    NOT NULL DEFAULT '',
  source     TEXT    NOT NULL CHECK (source IN ('manual', 'discovered', 'import')),
  source_url TEXT,
  batch_id   INTEGER REFERENCES add_batches (id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL               -- изменение настроек домена, не адресов
) STRICT;
CREATE INDEX domains_batch ON domains (batch_id) WHERE batch_id IS NOT NULL;

CREATE TABLE names (                        -- DNS-имена домена: введённое пользователем и его www-пара
  id              INTEGER PRIMARY KEY,
  domain_id       INTEGER NOT NULL REFERENCES domains (id) ON DELETE CASCADE,
  name            TEXT    NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 253 AND name = lower(name)),
  canonical       INTEGER NOT NULL CHECK (canonical IN (0, 1)),   -- 1 — вариант, который пользователь ввёл первым
  last_attempt_at INTEGER,                  -- последний результат любого резолвера, включая ошибки
  last_success_at INTEGER                   -- последний ответ с адресами от любого резолвера
) STRICT;
CREATE UNIQUE INDEX names_canonical ON names (domain_id) WHERE canonical = 1;
CREATE UNIQUE INDEX names_pair ON names (domain_id) WHERE canonical = 0;
CREATE INDEX names_last_attempt ON names (last_attempt_at);
CREATE INDEX names_last_success ON names (last_success_at);

CREATE TRIGGER names_pair_shape BEFORE INSERT ON names WHEN NEW.canonical = 0
BEGIN
  SELECT RAISE(ABORT, 'pair name must differ from the canonical one by a leading www.')
  WHERE NOT EXISTS (SELECT 1 FROM names c WHERE c.domain_id = NEW.domain_id AND c.canonical = 1 AND (NEW.name = 'www.' || c.name OR c.name = 'www.' || NEW.name));
END;

CREATE TABLE domain_groups (                -- нет строк = домен без группы
  domain_id INTEGER NOT NULL REFERENCES domains (id) ON DELETE CASCADE,
  group_id  INTEGER NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
  PRIMARY KEY (domain_id, group_id)
) STRICT;

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY,
  name TEXT    NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 50)
) STRICT;

CREATE TABLE domain_tags (
  domain_id INTEGER NOT NULL REFERENCES domains (id) ON DELETE CASCADE,
  tag_id    INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
  PRIMARY KEY (domain_id, tag_id)
) STRICT;

CREATE TABLE resolvers (
  id             INTEGER PRIMARY KEY,
  name           TEXT    NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
  kind           TEXT    NOT NULL CHECK (kind IN ('system', 'dns', 'doh', 'browser', 'agent')),
  endpoint       TEXT,                      -- udp://1.1.1.1, tcp://[2606:4700:4700::1111], tls://1.1.1.1, https://...
  use_for_routes INTEGER NOT NULL DEFAULT 1 CHECK (use_for_routes IN (0, 1)),
  max_qps        INTEGER NOT NULL DEFAULT 50 CHECK (max_qps > 0),
  position       INTEGER NOT NULL,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  CHECK ((kind IN ('system', 'agent')) = (endpoint IS NULL))
) STRICT;

CREATE TABLE agents (
  id             INTEGER PRIMARY KEY,
  resolver_id    INTEGER NOT NULL UNIQUE REFERENCES resolvers (id) ON DELETE CASCADE,
  cert_sha256    TEXT    NOT NULL UNIQUE CHECK (length(cert_sha256) = 64),
  cert_not_after INTEGER NOT NULL,
  revoked_at     INTEGER,
  mode           TEXT    CHECK (mode IN ('loop', 'once')),   -- как агент запущен, по последнему poll
  last_seen_at   INTEGER,                   -- последний poll
  last_result_at INTEGER,                   -- последний принятый результат
  last_ip        TEXT,
  agent_version  TEXT,
  os             TEXT,
  hostname       TEXT,
  dns_servers    TEXT    CHECK (dns_servers IS NULL OR json_valid(dns_servers)),
  created_at     INTEGER NOT NULL
) STRICT;

CREATE TABLE name_resolves (                -- последний результат пары имя × резолвер
  name_id      INTEGER NOT NULL REFERENCES names (id) ON DELETE CASCADE,
  resolver_id  INTEGER NOT NULL REFERENCES resolvers (id) ON DELETE CASCADE,
  attempted_at INTEGER NOT NULL,            -- любой результат, включая ошибки
  answered_at  INTEGER,                     -- авторитетный ответ: ok, nodata, nxdomain, notfound
  succeeded_at INTEGER,                     -- ответ с адресами
  status       TEXT    NOT NULL CHECK (status IN ('ok', 'nodata', 'nxdomain', 'notfound', 'error', 'unreachable')),
  error        TEXT,
  cnames       TEXT    CHECK (cnames IS NULL OR json_valid(cnames)),
  reporter     TEXT,                        -- для внешних резолверов: IP и клиент отправителя
  requested_at INTEGER,                     -- «Resolve now» для внешних резолверов
  PRIMARY KEY (name_id, resolver_id)
) STRICT;
CREATE INDEX name_resolves_due ON name_resolves (resolver_id, attempted_at);

CREATE TABLE resolve_leases (               -- задания, выданные внешним резолверам
  name_id      INTEGER NOT NULL REFERENCES names (id) ON DELETE CASCADE,
  resolver_id  INTEGER NOT NULL REFERENCES resolvers (id) ON DELETE CASCADE,
  leased_until INTEGER NOT NULL,
  PRIMARY KEY (name_id, resolver_id)
) STRICT;

CREATE TABLE addresses (                   -- один IP у разных имён — разные строки
  id        INTEGER PRIMARY KEY,
  name_id   INTEGER NOT NULL REFERENCES names (id) ON DELETE CASCADE,
  ip        TEXT    NOT NULL,               -- нормализованная запись ipaddress
  family    INTEGER NOT NULL CHECK (family IN (4, 6)),
  ip_bin    BLOB    NOT NULL,               -- 4 или 16 байт big-endian: поиск по диапазону сети
  UNIQUE (name_id, ip),
  CHECK (length(ip_bin) = CASE family WHEN 4 THEN 4 ELSE 16 END)
) STRICT;
CREATE INDEX addresses_range ON addresses (family, ip_bin);

CREATE TABLE sightings (
  address_id    INTEGER NOT NULL REFERENCES addresses (id) ON DELETE CASCADE,
  resolver_id   INTEGER NOT NULL REFERENCES resolvers (id) ON DELETE CASCADE,
  first_seen_at INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL CHECK (last_seen_at >= first_seen_at),
  PRIMARY KEY (address_id, resolver_id)
) STRICT;

CREATE TABLE global_routes (
  id         INTEGER PRIMARY KEY,
  cidr       TEXT    NOT NULL UNIQUE,
  family     INTEGER NOT NULL CHECK (family IN (4, 6)),
  comment    TEXT    NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE route_groups (
  route_id INTEGER NOT NULL REFERENCES global_routes (id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
  PRIMARY KEY (route_id, group_id)
) STRICT;

CREATE TABLE plan_state (                   -- одна строка: сводка последнего вычисленного плана
  id               INTEGER PRIMARY KEY CHECK (id = 1),
  computed_at      INTEGER NOT NULL,
  plan_sha256      TEXT    NOT NULL CHECK (length(plan_sha256) = 64),
  routes_v4        INTEGER NOT NULL,
  routes_v6        INTEGER NOT NULL,
  raw_v4           INTEGER NOT NULL,        -- сетей до бюджета
  raw_v6           INTEGER NOT NULL,
  extra_ratio_v4   REAL    NOT NULL,        -- доля адресов, добавленных агрегацией
  extra_ratio_v6   REAL    NOT NULL,
  warnings         TEXT    NOT NULL CHECK (json_valid(warnings))
) STRICT;

CREATE TABLE plan_routes (                  -- маршруты текущего плана, то есть содержимое routes.conf
  id              INTEGER PRIMARY KEY,
  cidr            TEXT    NOT NULL UNIQUE,
  family          INTEGER NOT NULL CHECK (family IN (4, 6)),
  first_bin       BLOB    NOT NULL,         -- первый и последний адрес сети, формат как addresses.ip_bin
  last_bin        BLOB    NOT NULL,
  kind            TEXT    NOT NULL CHECK (kind IN ('dns', 'aggregated', 'global')),
  global_route_id INTEGER REFERENCES global_routes (id) ON DELETE SET NULL,   -- NULL у global: маршрут удалён, но остаётся в файле до следующего плана
  added_at        INTEGER NOT NULL,         -- когда маршрут впервые попал в файл
  reconnected     INTEGER NOT NULL CHECK (reconnected IN (0, 1)),   -- клиенты переподключались после его добавления
  CHECK (kind = 'global' OR global_route_id IS NULL)
) STRICT;

CREATE TABLE plan_route_parts (             -- сети из DNS, из которых собран маршрут (для kind = 'dns' — одна)
  route_id  INTEGER NOT NULL REFERENCES plan_routes (id) ON DELETE CASCADE,
  cidr      TEXT    NOT NULL,
  first_bin BLOB    NOT NULL,
  last_bin  BLOB    NOT NULL,
  PRIMARY KEY (route_id, cidr)
) STRICT;

CREATE TABLE settings (                     -- типы значений задаёт реестр настроек в Python (раздел 13.2)
  key        TEXT    PRIMARY KEY,
  value      TEXT    NOT NULL CHECK (json_valid(value)),
  updated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE scheduler_state (              -- одна строка
  id                    INTEGER PRIMARY KEY CHECK (id = 1),
  full_run_id           INTEGER REFERENCES resolve_runs (id) ON DELETE SET NULL,   -- текущий полный проход, NULL если не идёт
  full_cursor_name_id   INTEGER,            -- последнее обработанное имя прохода
  last_full_started_at  INTEGER,
  last_cleanup_at       INTEGER
) STRICT;

CREATE TABLE reconnect_state (              -- одна строка
  id                INTEGER PRIMARY KEY CHECK (id = 1),
  pending_mode      TEXT    CHECK (pending_mode IN ('auto', 'on_leave')),
  pending_since     INTEGER,
  presence_until    INTEGER,                -- администратор в UI до этого момента
  last_reconnect_at INTEGER,
  CHECK ((pending_mode IS NULL) = (pending_since IS NULL))
) STRICT;

CREATE TABLE ovpn_clients (                 -- снимок status 3, перезаписывается каждым tick
  instance      TEXT    NOT NULL,
  cid           INTEGER NOT NULL,
  common_name   TEXT    NOT NULL,
  real_address  TEXT    NOT NULL,
  virtual_v4    TEXT,
  virtual_v6    TEXT,
  connected_at  INTEGER NOT NULL,
  snapshot_at   INTEGER NOT NULL,
  PRIMARY KEY (instance, cid)
) STRICT;

CREATE TABLE web_devices (                  -- браузеры, вошедшие по паролю с недоверенного адреса
  id           INTEGER PRIMARY KEY,
  token_sha256 TEXT    NOT NULL UNIQUE CHECK (length(token_sha256) = 64),   -- сам токен только в cookie
  user_agent   TEXT    NOT NULL,
  created_ip   TEXT    NOT NULL,
  created_at   INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  last_seen_ip TEXT    NOT NULL,
  revoked_at   INTEGER
) STRICT;

CREATE TABLE requests (                     -- очередь от web, CLI и Agent API к tick
  id         INTEGER PRIMARY KEY,
  kind       TEXT    NOT NULL CHECK (kind IN ('resolve_domains', 'resolve_all', 'apply')),
  payload    TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
  created_at INTEGER NOT NULL
) STRICT;

CREATE TABLE resolve_runs (
  id                INTEGER PRIMARY KEY,
  trigger           TEXT    NOT NULL CHECK (trigger IN ('timer', 'manual', 'new_domains', 'browser', 'agent')),
  resolver_id       INTEGER REFERENCES resolvers (id) ON DELETE CASCADE,   -- для browser и agent
  started_at        INTEGER NOT NULL,
  finished_at       INTEGER,
  domains_total     INTEGER NOT NULL DEFAULT 0,
  addresses_new     INTEGER NOT NULL DEFAULT 0,
  addresses_expired INTEGER NOT NULL DEFAULT 0,
  error             TEXT,
  CHECK ((trigger IN ('browser', 'agent')) = (resolver_id IS NOT NULL))
) STRICT;

CREATE TABLE resolve_run_resolvers (        -- статистика прогона по каждому резолверу
  run_id      INTEGER NOT NULL REFERENCES resolve_runs (id) ON DELETE CASCADE,
  resolver_id INTEGER NOT NULL REFERENCES resolvers (id) ON DELETE CASCADE,
  queries     INTEGER NOT NULL,
  ok          INTEGER NOT NULL,
  notfound    INTEGER NOT NULL,             -- nodata + nxdomain + notfound
  errors      INTEGER NOT NULL,
  unreachable INTEGER NOT NULL,
  avg_ms      INTEGER,
  healthy     INTEGER NOT NULL CHECK (healthy IN (0, 1)),
  PRIMARY KEY (run_id, resolver_id)
) STRICT;

CREATE TABLE apply_runs (
  id                  INTEGER PRIMARY KEY,
  trigger             TEXT    NOT NULL CHECK (trigger IN ('tick', 'manual')),
  started_at          INTEGER NOT NULL,
  finished_at         INTEGER,
  routes_v4           INTEGER NOT NULL,
  routes_v6           INTEGER NOT NULL,
  routes_added        INTEGER NOT NULL,
  routes_removed      INTEGER NOT NULL,
  diff                TEXT    NOT NULL CHECK (json_valid(diff)),   -- {"added": [...], "removed": [...]}
  file_sha256         TEXT    NOT NULL CHECK (length(file_sha256) = 64),
  file_changed        INTEGER NOT NULL CHECK (file_changed IN (0, 1)),
  reconnect           TEXT    NOT NULL CHECK (reconnect IN ('none', 'now', 'auto', 'on_leave')),
  clients_total       INTEGER,
  clients_reconnected INTEGER,
  clients_failed      INTEGER,
  status              TEXT    NOT NULL CHECK (status IN ('ok', 'unchanged', 'deferred', 'error')),
  error               TEXT
) STRICT;

CREATE TABLE events (                       -- журнал изменений
  id        INTEGER PRIMARY KEY,
  ts        INTEGER NOT NULL,
  kind      TEXT    NOT NULL,               -- domain.added, address.new, address.expired, agent.created, agent.revoked, agent.rejected, reconnect.done, ...
  actor     TEXT    NOT NULL,               -- web:<ip>[/<common name>] | web-device:<id> | cli:<unix user> | tick | browser:<ip> | agent:<name>
  domain_id INTEGER,                        -- без FK: событие переживает удаление домена
  name      TEXT,                           -- имя, к которому относится событие (address.new и т.п.)
  details   TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(details))
) STRICT;
```

`events.kind` намеренно не ограничен `CHECK`: набор событий растёт чаще схемы, а значения задаёт `StrEnum` в Python.

Вычисляемые правила:
- **Резолв, адреса, наблюдения, задания внешним резолверам — по именам**, настройки — по доменам. У `www.example.com` и `example.com` своя история и могут быть разные адреса.
- **Наблюдение активно**, если `last_seen_at >= name_resolves.answered_at` для той же пары имя × резолвер, то есть адрес был в последнем авторитетном ответе этого резолвера.
- **Наблюдение устаревает**, если в последнем ответе адреса нет, но `answered_at - last_seen_at < routes.retention`. **Истёкшее** наблюдение удаляется; адрес без наблюдений удаляется вместе с ним, в `events` пишется `address.expired`.
- **Адрес попадает в маршруты**, если у него есть неистёкшее наблюдение от резолвера с `use_for_routes = 1`. Для агента резолвер ещё и не должен быть отозван.
- **Один IP у нескольких имён** хранится отдельной строкой `addresses` для каждого имени: у каждой пары свои наблюдения и сроки. UI и CLI группируют строки по `ip` и показывают у адреса все имена, для которых он получен.
- **Адреса внутри сети** находятся запросом по диапазону: `family = ? AND ip_bin BETWEEN first_bin AND last_bin`. Сравнение BLOB одинаковой длины в SQLite побайтовое, поэтому порядок совпадает с порядком адресов.
- **Время резолва имени**: `names.last_attempt_at` и `names.last_success_at` обновляются в той же транзакции, что и `name_resolves`, результатом любого резолвера. По ним выбираются задания для внешних резолверов (раздел 6.5); в таблице доменов они показаны у каждого имени, по каждому резолверу — в карточке домена.
- **Статус имени** вычисляется только по отмеченным резолверам: `ok`, если хотя бы один вернул адреса; `notfound`, если все авторитетные ответы пустые или NXDOMAIN; `error`, если ни одного авторитетного ответа нет. **Статус домена** — `ok`, если `ok` хотя бы у одного имени. Предупреждение «not found» выдаётся по домену, поэтому `www.api.example.com` без адресов при рабочем `api.example.com` проблемой не считается.
- **Домен маршрутизируется**, если он включён и либо не входит ни в одну группу, либо хотя бы одна из его групп включена. То же правило действует для глобальных маршрутов. Большинство доменов без группы: это обычное состояние, а не «неразобранные».
- **Имена домена** — чистая функция `domain_names(canonical)`: каноничное имя и его www-пара (раздел 9.4), у каждого домена ровно два имени. Отдельно имена не добавляются и не удаляются; таблица `names` всегда совпадает с результатом этой функции.
- **Агент активен**, если `last_seen_at > now - agent.offline_after` (30 мин). Порог не зависит от интервала опроса, поэтому подходит и для агентов в цикле, и для запусков по расписанию.
- Каноничное имя неизменяемо: переименование — это удаление и добавление. Удалённый домен удаляется физически вместе с именами и адресами, след остаётся в `events`.

## 6. Резолверы

### 6.1 Виды

| `kind` | Где работает | Как |
|---|---|---|
| `system` | сервер | `dnspython` с конфигурацией из `/etc/resolv.conf`. |
| `dns` | сервер | Конкретный сервер: `udp://`, `tcp://`, `tls://` (DoT); IPv4 или IPv6. |
| `doh` | сервер | DoH (RFC 8484) через `dnspython` + `httpx`. Полезен, если обычный DNS на сервере перехватывается. |
| `browser` | браузер администратора | DoH JSON API из JS (раздел 6.6). |
| `agent` | устройство пользователя | Системный резолвер устройства через `st-agent` (раздел 7). |

Список резолверов — глобальный чек-лист на странице Resolvers. Опрашиваются все и всегда, их адреса видны в UI. Маршруты строятся только по отмеченным (`use_for_routes`). Поскольку неотмеченные тоже опрашиваются, их наблюдения актуальны и после включения галочки сразу идут в план. Для каждого резолвера видны время последнего обновления, число свежих и устаревших записей и статистика ошибок.

Резолверы при первой установке:

| Name | Kind | Endpoint | В маршрутах |
|---|---|---|---|
| system | system | — | да |
| Cloudflare | dns | `udp://1.1.1.1` | да |
| Google | dns | `udp://8.8.8.8` | да |
| Cloudflare v6 | dns | `udp://[2606:4700:4700::1111]` | да |
| Cloudflare DoH | doh | `https://cloudflare-dns.com/dns-query` | нет |
| Browser (Cloudflare) | browser | `https://cloudflare-dns.com/dns-query` | да |

Агенты добавляются по одному из UI (раздел 7.7); у каждого появляется своя строка-резолвер.

### 6.2 Классификация ответа

Для каждой пары имя × резолвер запрашиваются `A` и `AAAA`, независимо от флагов `routes.push_ipv4` / `routes.push_ipv6`: адреса обоих семейств всегда видны в UI.

| Ответ | `status` | Обновляет |
|---|---|---|
| Есть адреса | `ok` | `attempted_at`, `answered_at`, `succeeded_at` |
| NOERROR без записей | `nodata` | `attempted_at`, `answered_at` |
| NXDOMAIN | `nxdomain` | `attempted_at`, `answered_at` |
| «Не найдено» без различения NXDOMAIN и NODATA (системный резолвер агента) | `notfound` | `attempted_at`, `answered_at` |
| Timeout, SERVFAIL, REFUSED | `error` | `attempted_at` |
| Сеть недоступна (например, IPv6-резолвер на IPv4-only сервере) | `unreachable` | `attempted_at` |

`unreachable` не считается ошибкой домена. Резолвер с такими ответами помечается на странице Resolvers как недоступный, с причиной.

### 6.3 Нагрузка серверных резолверов

5000 доменов — 10 000 имён (у каждого домена два); × 2 типа записей × 5 серверных резолверов — около 100 000 запросов за полный проход. Каждый резолвер ограничен своим `max_qps` (по умолчанию 50) и общим пулом потоков `resolve.concurrency`. При 50 qps на резолвер проход занимает около 7 минут. Он идёт порциями по 500 имён, результаты каждой порции записываются короткой транзакцией. Интервал полного прохода по умолчанию 60 минут.

### 6.4 Истечение и защита от сбоев

- Наблюдения стареют только по авторитетным ответам: `retention` (по умолчанию 4 дня, глобальная настройка в UI) отсчитывается от `answered_at` этой пары имя × резолвер, а не от текущего времени. Отказ резолвера, выключенный агент или редкое открытие UI ничего не удаляют.
- Обратная сторона: адреса агента, который давно не выходил на связь, остаются в маршрутах. Поэтому главная страница предупреждает об отмеченных внешних резолверах, которые не обновлялись дольше `resolvers.stale_warning` (2 дня). Администратор может снять галочку или удалить резолвер.
- Защита от резолвера, который внезапно отвечает NXDOMAIN или пустым ответом на всё (фильтрация, перехват, captive portal): если в прогоне у резолвера доля не-`ok` среди имён, которые в прошлый раз были `ok`, больше `resolve.unhealthy_ratio` (50%), резолвер помечается `healthy = 0`. Его не-`ok` ответы в этом прогоне обрабатываются как `error`. Проверка работает только при выборке от 20 имён. Для внешних резолверов прогоном считается сессия (раздел 6.5).

### 6.5 Внешние резолверы: задания и результаты

Браузер и агенты сами приходят за работой. Логика общая (`core/external.py`); различаются транспорт и аутентификация.

Выбор заданий для резолвера R — чистая функция над снимком БД с двумя глобальными параметрами: `n` (`agent.batch` / `browser.batch`) и `m` (`agent.max_age` / `browser.max_age`):
Задание — это имя, а не домен: каноничное имя и www-пара выдаются и отвечаются независимо.
1. Кандидаты — имена, по которым R ни разу не отвечал, плюс имена с `requested_at > attempted_at` («Resolve now»). Они идут первыми.
2. Затем имена, у которых `attempted_at` для R старше `m`. Учитывается любой результат, включая ошибки, поэтому имя с ошибкой повторяется не чаще раза в `m`, а не при каждом опросе.
3. Исключаются имена с действующей арендой в `resolve_leases`.
4. Из оставшихся берутся `n` самых старых по `attempted_at`. Выданные задания арендуются на 5 минут, чтобы повторный запрос до прихода результатов не получил их снова.

Приём результатов:
- имя и резолвер должны существовать, IP должны быть валидными; не больше 64 адресов на семейство и 500 результатов за запрос;
- время наблюдения — время сервера в момент приёма, а не часы клиента;
- результат записывается так же, как результат серверного резолвера (включая `names.last_attempt_at` / `last_success_at`), аренда снимается, в `reporter` сохраняется отправитель;
- ставится запрос `apply`.

Сессия: первый результат после простоя открывает `resolve_run` с `trigger = browser` или `agent`. Сессия закрывается, когда резолвер получает пустой список заданий или после 10 минут тишины.

### 6.6 Резолв из браузера

Задача — получить адреса так, как их видит клиент: CDN отдают разные адреса в зависимости от местоположения.

Из браузерного JS нельзя обратиться к резолверу ОС или провайдера, доступен только DoH через `fetch`. JSON API Cloudflare (`https://cloudflare-dns.com/dns-query`, заголовок `Accept: application/dns-json`) и Google (`https://dns.google/resolve`) отдают `Access-Control-Allow-Origin: *`. Ответ отражает взгляд DoH-провайдера из точки, ближайшей к клиенту, но это не ответ DNS провайдера клиента. Ответ системного резолвера устройства даёт агент (раздел 7).

Когда выполняется:
- **при добавлении**: сразу после отправки формы (главная страница с итогом, раздел 14.1) скрипт резолвит новые имена, включая www-пары, через все `browser`-резолверы;
- **при открытии web UI**: режим `browser.refresh_mode` — `auto` (после загрузки главной страницы), `button` (по кнопке «Refresh browser DNS») или `off`. Результаты пишутся в БД; таблицы на уже открытой странице не перерисовываются, свежие данные видны после перезагрузки. Задания выбираются по разделу 6.5. Кнопка «Refresh all» обрабатывает пачку за пачкой, пока страница открыта.

`browser-dns.js` — только транспорт: он не разбирает ответы DoH, а пересылает их серверу как есть. Разбор и классификация выполняются в Python, где они типизированы и покрыты тестами.

```
GET  /api/browser-dns/tasks
     -> {"resolvers": [{"id": 6, "url": "https://cloudflare-dns.com/dns-query"}],
         "names": ["example.com", "www.example.com", ...]}
браузер: для каждого имени × резолвера fetch A и AAAA, не больше browser.concurrency (6) одновременно, таймаут 5 с
POST /api/browser-dns/results     (пачками по 50, CSRF-токен в заголовке)
     <- [{"name": "example.com", "resolver_id": 6, "type": "A",
          "http_status": 200, "body": {...ответ DoH JSON...}, "error": null}, ...]
```

Что ещё нужно учесть:
- CSP страницы разрешает `connect-src` к origin'ам `browser`-резолверов;
- если адрес DoH-провайдера сам попадает в маршруты VPN, запрос из браузера пойдёт через туннель и вернёт взгляд сервера. В UI показывается предупреждение, если `browser`-endpoint покрывается текущим планом;
- без JS эта функция не работает, остальной UI работает.

## 7. Агент на клиенте (`st-agent`)

### 7.1 Назначение

Программа на устройстве пользователя. Она подключается к серверу, забирает список имён, резолвит их системным резолвером устройства и отправляет результат. Для сервера каждый агент — отдельный резолвер вида `agent`, со своей галочкой «в маршрутах», временем последнего обновления и статистикой.

Агент даёт то, чего не может браузер: ответ DNS, настроенного на устройстве (роутер, провайдер, корпоративный DNS). Если провайдер подменяет ответы для заблокированных доменов, агент вернёт подменённый адрес. Поэтому, учитывать ли конкретного агента в маршрутах, решает администратор.

### 7.2 Платформы и сборка

- Windows 10+ x86-64 и Linux x86-64. Keenetic (Entware, mipsel/aarch64) не требуется, но Go собирает и под него без изменений кода.
- Go, актуальная стабильная версия, только стандартная библиотека. `CGO_ENABLED=0`, статический бинарник без runtime-зависимостей, около 6–8 МБ.
- Для Windows собираются два бинарника из одного кода: `st-agent.exe` (консольный, для ручного запуска и диагностики) и `st-agentw.exe` (`-ldflags -H=windowsgui`, без окна консоли, для планировщика заданий; пишет только в лог-файл).
- Сборка: `GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=1.0.0" -o st-agent.exe ./cmd/st-agent`, аналогично для `st-agentw.exe` и `GOOS=linux`. Go нужен на машине сборки, не на сервере: `install.sh` раскладывает готовые бинарники в `/opt/subnet-tracker/agent/`.

### 7.3 Системный резолв

`net.Resolver` из стандартной библиотеки:
- Windows: запросы идут через системный API (`GetAddrInfoW`) — с кэшем DNS-клиента Windows, `hosts` и DNS-серверами адаптеров;
- Linux (`CGO_ENABLED=0`): встроенный резолвер Go читает `/etc/resolv.conf`, `/etc/hosts` и `/etc/nsswitch.conf`. При systemd-resolved в `resolv.conf` указан `127.0.0.53`, так что запросы идут через него.

Для каждого имени: `LookupIP(ctx, "ip4", name)`, `LookupIP(ctx, "ip6", name)` и `LookupCNAME`. Результат `ok`, если хотя бы одно семейство вернуло адреса. `notfound`, если оба ответа «не найдено» (`DNSError.IsNotFound`); системный API не различает NXDOMAIN и NODATA. Иначе `error`. Системный резолвер может не возвращать AAAA на устройстве без IPv6 — это поведение системы, и агент его не исправляет.

DNS-серверы устройства (Windows — `GetNetworkParams` из `iphlpapi.dll` через `syscall`, Linux — `/etc/resolv.conf`) агент сообщает серверу только для отображения.

### 7.4 Защита соединения

Доменного имени у сервера нет, поэтому TLS строится без публичного PKI:
- **сервер для агента** аутентифицируется по пину: агент сверяет SHA-256 от открытого ключа (SPKI) серверного сертификата с пином из своего конфига. Проверка имени не выполняется. В Go это `InsecureSkipVerify: true` плюс собственная проверка в `VerifyConnection`. Пин по ключу, а не по сертификату, позволяет перевыпустить сертификат с тем же ключом;
- **агент для сервера** — mTLS. У каждого агента свой клиентский сертификат, подписанный локальным CA агентов (ECDSA P-256). Сервер требует сертификат при рукопожатии (`CERT_REQUIRED`): без сертификата от нашего CA соединение обрывается до HTTP, и код приложения для чужих клиентов не выполняется;
- **отзыв**: после рукопожатия сервер ищет `cert_sha256` в `agents`. Неизвестный или отозванный сертификат получает 403, в `events` пишется `agent.rejected`, с ограничением частоты записи. Так не нужны CRL и перезагрузка TLS-контекста при каждом изменении;
- TLS 1.3 минимум: обе стороны наши, Go реализует TLS сам, поэтому версия Windows на это не влияет;
- ключи: CA и серверный ключ создаёт `subnet-tracker pki init` при установке, пакет `cryptography`. Срок сертификатов — 10 лет, отзыв через БД.

Agent API — отдельный процесс `subnet-tracker agent-api` на стандартных `http.server.ThreadingHTTPServer` и `ssl`:
- по умолчанию слушает только адрес сервера в туннеле (`10.8.0.1:8443`). На старте агенты приходят через тот же VPN, поэтому открывать порт в файрволе не нужно;
- работа через публичный адрес предусмотрена: достаточно добавить адреса в `agent_api.listen` и `agent_api.endpoints` (раздел 13.1) и открыть порт. Для `[::]` выставляется `IPV6_V6ONLY`, чтобы IPv4 и IPv6 слушались отдельно — так работают IPv4-only, IPv6-only и dual-stack серверы;
- рукопожатие выполняется в рабочем потоке, а не в цикле accept; таймаут сокета 15 с, не больше 32 соединений, лимит размера запроса 1 МБ.

### 7.5 Протокол

JSON поверх HTTPS, префикс версии `/agent/v1`. Параметры работы задаёт сервер, поэтому администратор настраивает всех агентов централизованно.

```
POST /agent/v1/poll
  -> {"version": "1.0.0", "mode": "once", "os": "windows/amd64 10.0.19045",
      "hostname": "LAPTOP-ALEX", "dns_servers": ["192.168.1.1"]}
  <- {"poll_interval": 60, "concurrency": 8, "timeout": 5, "latest_version": "1.0.0",
      "names": ["example.com", "www.example.com", ...]}

POST /agent/v1/results
  -> {"results": [{"name": "example.com", "status": "ok",
                   "ipv4": ["93.184.215.14"], "ipv6": [],
                   "cname": "example.com.", "error": null}]}
  <- {"accepted": 1}
```

`names` — до `agent.batch` (n, по умолчанию 200) самых старых имён, у которых последний результат от этого агента старше `agent.max_age` (m, по умолчанию 60 минут); новые идут первыми (раздел 6.5).

### 7.6 Режимы запуска

```
st-agent run [--config PATH] [--log FILE]                       # цикл
st-agent once [--all] [--max-rounds N] [--config PATH] [--log FILE]   # одиночный запуск
```

- **Цикл** (`run`): `poll`; если доменов нет — пауза `poll_interval` (60 с) со случайным разбросом ±10%; если есть — резолв с параллельностью `concurrency`, отправка результатов пачками по 50 и сразу следующий `poll`. При сетевой ошибке перебираются адреса из `endpoints`, затем экспоненциальная пауза до 5 минут. Новый домен агент получает не позже чем через минуту.
- **Одиночный запуск** (`once`): один `poll`, резолв полученной пачки, отправка результатов, выход. С `--all` опрашивает сервер, пока тот не вернёт пустой список (не больше `--max-rounds`, по умолчанию 100), то есть выполняет всё, что сервер готов отдать прямо сейчас. Код выхода: `0` — успех, `1` — ошибка соединения или аутентификации, `3` — часть результатов не отправлена. Этот режим предназначен для запуска снаружи: systemd timer или планировщиком заданий.

Агент сообщает режим в `poll`, и UI показывает его в карточке агента.

Полное обновление 5000 доменов при `m = 60 минут` — около 10 000 запросов в час. В цикле это примерно 3 в секунду. При запуске `once --all` раз в 10 минут — около 1700 запросов за запуск.

### 7.7 Выдача доступа из web UI

1. Администратор создаёт агента на странице Resolvers: имя и галочка «в маршрутах».
2. Сервер генерирует ключ и клиентский сертификат, сохраняет в БД только отпечаток и срок действия и один раз показывает:
   - ссылки на бинарники для Windows и Linux;
   - файл конфигурации `st-agent.json` для скачивания;
   - команду регистрации одной строкой: `st-agent enroll <token>`, где `token` — тот же конфиг в base64url;
   - unit-файлы systemd для Linux и команды планировщика для Windows (раздел 7.8).
3. Закрытый ключ после этого на сервере не хранится. Если он потерян — «Reissue credentials»: новый ключ и сертификат, старый отзывается.

Web UI отдаёт ключ по HTTP через VPN-туннель или localhost, поэтому канал уже зашифрован.

```json
{
  "version": 1,
  "name": "laptop-alex",
  "endpoints": ["https://10.8.0.1:8443"],
  "server_spki_sha256": "3q2+7wAbCdEf...",
  "client_cert": "-----BEGIN CERTIFICATE-----\n...",
  "client_key": "-----BEGIN PRIVATE KEY-----\n..."
}
```

`endpoints` берутся из `agent_api.endpoints` в `config.toml`. Если позже открыть публичный адрес, его добавляют туда, и новые конфиги агентов будут его содержать; старые агенты получают его через «Reissue credentials».

### 7.8 Команды агента и автозапуск

```
st-agent enroll TOKEN [--config PATH]     # записывает конфиг (Linux — с правами 0600)
st-agent run ...                          # цикл (раздел 7.6)
st-agent once [--all] ...                 # одиночный запуск (раздел 7.6)
st-agent test [--config PATH]             # подключение, проверка пина, один poll без резолва
st-agent resolve DOMAIN                   # локальный резолв без сервера, для диагностики
st-agent version
```

Конфиг по умолчанию: Windows — `%LOCALAPPDATA%\st-agent\config.json`; Linux — `~/.config/st-agent/config.json`, для root — `/etc/st-agent/config.json`. Лог — stderr или `--log FILE` с ротацией по 1 МБ.

**Linux — systemd** (файлы в `deploy/agent/`, скачиваются из web UI):

```ini
# st-agent.service — одиночный запуск
[Unit]
Description=subnet-tracker resolve agent (one batch run)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/st-agent once --all --config ${CREDENTIALS_DIRECTORY}/config
LoadCredential=config:/etc/st-agent/config.json
DynamicUser=yes
```

```ini
# st-agent.timer — запуск по расписанию
[Unit]
Description=Run subnet-tracker resolve agent periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
RandomizedDelaySec=30s
Persistent=true

[Install]
WantedBy=timers.target
```

Альтернатива для постоянной работы — `st-agent-loop.service` (`Type=simple`, `st-agent run`, `Restart=on-failure`, `RestartSec=30s`). Оба варианта работают и как пользовательские юниты (`systemctl --user`) с конфигом из `~/.config/st-agent/`.

**Windows — планировщик заданий**, одной командой. Одиночный запуск каждые 10 минут без окна консоли:

```
schtasks /Create /TN st-agent /SC MINUTE /MO 10 /RL LIMITED /TR "\"%LOCALAPPDATA%\st-agent\st-agentw.exe\" once --all --log \"%LOCALAPPDATA%\st-agent\st-agent.log\""
```

Или цикл при входе пользователя:

```
schtasks /Create /TN st-agent /SC ONLOGON /RL LIMITED /TR "\"%LOCALAPPDATA%\st-agent\st-agentw.exe\" run --log \"%LOCALAPPDATA%\st-agent\st-agent.log\""
```

## 8. Маршруты

### 8.1 План маршрутов (чистая функция)

Вход: снимок доменов, групп, адресов, наблюдений, резолверов, агентов, глобальных маршрутов, настройки, `now`. Выход: `RoutePlan(routes, provenance, stats, warnings)`.

1. Маршрутизируемые домены: правило из раздела 5. Адреса берутся у всех имён домена, настройки (включённость, группы, префиксы) — у домена.
2. Их адреса, которые попадают в маршруты (наблюдение от отмеченного и не отозванного резолвера, не истёкшее), только для семейств, у которых включён `routes.push_ipv4` / `routes.push_ipv6`.
3. Эффективный префикс: у домена → у его включённых групп (если групп несколько и префиксы разные, берётся самый широкий, то есть наименьшая длина; UI предупреждает о конфликте) → `routes.default_prefix_v4` (23) / `routes.default_prefix_v6` (48).
4. `ip_network(f"{ip}/{prefix}", strict=False)`.
5. Исключения применяются только к сетям из DNS. Встроенные: `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10`, `127.0.0.0/8`, `169.254.0.0/16`, `172.16.0.0/12`, `192.168.0.0/16`, `224.0.0.0/3`, `::/128`, `::1/128`, `fc00::/7`, `fe80::/10`, `ff00::/8`. К ним добавляются `routes.server_addresses` и `routes.tunnel_networks` из `config.toml` и `routes.exclude`. Пересекающаяся сеть не выбрасывается, а урезается (`address_exclude`), в `warnings` попадает пояснение. Адрес сервера исключается обязательно: маршрут к нему через туннель рвёт сам туннель.
6. Агрегация `collapse_addresses`: вложенные сети поглощаются, соседние склеиваются.
7. Бюджет (раздел 8.3).
8. Включённые глобальные маршруты включённых семейств добавляются как есть: без исключений и агрегации, но учитываются в бюджете. Глобальный маршрут, который покрывает адрес сервера, отклоняется ещё при сохранении.
9. `provenance`: для каждого маршрута — его части (исходные сети из DNS; у агрегированного их несколько) или ссылка на глобальный маршрут. Внутри частей — адреса, у каждого адреса — имена, для которых он получен, и резолверы, которые его вернули. По нему UI и CLI отвечают на вопрос «почему этот маршрут здесь».

`tick` сохраняет план в `plan_state`, `plan_routes` и `plan_route_parts`, когда меняется его хеш. Адреса внутри маршрута не копируются: страница их получает запросом по диапазону `ip_bin` к `addresses`. Поэтому web UI не пересчитывает план на каждый запрос, а данные об адресах всегда свежие.

### 8.2 Семейства адресов и адреса сервера

`routes.push_ipv4` и `routes.push_ipv6` — простые флаги, по умолчанию оба включены. Комплекс не проверяет, есть ли на сервере соответствующий стек: на IPv4-only или IPv6-only сервере администратор выключает лишний флаг. Выключенное семейство не попадает в файл, но его адреса по-прежнему резолвятся и видны в UI.

Адреса сервера для исключений задаются в `config.toml` (`routes.server_addresses`). `install.sh` заполняет их при установке глобальными адресами интерфейсов хоста; во время работы комплекс их не определяет.

### 8.3 Бюджет маршрутов

5000 доменов после агрегации до /23 дают ориентировочно 2–4 тысячи маршрутов. Чтобы клиенты не получали больше, чем выдерживают, у плана есть бюджет: `routes.max_v4` / `routes.max_v6`, по умолчанию 1000 / 300. Замеры не планируются: администратор подстраивает бюджет по опыту эксплуатации.

Алгоритм (чистая функция, детерминированная):
1. Сети сортируются; для каждой пары соседних строится наименьшая общая надсеть `S`.
2. Стоимость кандидата — сколько адресов `S` не покрыто текущими сетями; выигрыш — сколько сетей внутри `S` минус один. Кандидаты, у которых `S` пересекается с исключениями или короче `routes.min_aggregate_prefix_v4` / `_v6` (/12 и /32), отбрасываются.
3. Пока число сетей больше бюджета, выбирается кандидат с минимальной стоимостью на единицу выигрыша, сети внутри `S` заменяются на `S`, соседние кандидаты пересчитываются (куча).
4. Если бюджет недостижим, в `warnings` пишется предупреждение, план остаётся с минимально возможным числом маршрутов.

В статистику плана входят число сетей до и после агрегации и доля дополнительно покрытого адресного пространства. Это показывается над таблицей маршрутов на главной странице.

### 8.4 Файл

```
# managed by subnet-tracker, do not edit
# generated 2026-10-11T03:20:00Z, 987 IPv4 + 112 IPv6 routes
push "route 149.154.160.0 255.255.240.0"
push "route 172.217.16.0 255.255.254.0"
push "route-ipv6 2001:67c:4e8::/48"
```

Строки отсортированы. Хеш считается по строкам `push`, без заголовка, поэтому повторная генерация без изменений не меняет файл. Запись атомарная: временный файл в том же каталоге, `fsync`, `rename`; предыдущая версия сохраняется в `routes.conf.prev`. Сравнение идёт с файлом на диске, а не с БД, так что ручная правка тоже обнаружится и будет перезаписана.

Файл пишется сразу, как только план изменился: новые подключения получают маршруты без задержки. Откладывается только переподключение уже подключённых клиентов.

### 8.5 Переподключение клиентов

Web UI открывается через тот же VPN, поэтому немедленное переподключение после добавления одного домена оборвало бы сессию самого администратора. Решение о переподключении учитывает, работает ли администратор в UI сейчас.

**Присутствие.** `reconnect_state.presence_until` — момент, до которого администратор считается работающим в UI:
- любой запрос к web UI, прошедший проверку доступа (загрузка страницы, отправка формы), продлевает его до `now + apply.idle_delay` (30 с) на сервере, поэтому механизм работает и без JS;
- `presence.js` на каждой странице продлевает его при взаимодействии без запросов к серверу (клик, нажатие клавиши, ввод, прокрутка): `POST /api/presence` не чаще раза в 10 секунд;
- при закрытии страницы или уходе с неё (`pagehide`) `presence.js` отправляет `navigator.sendBeacon('/api/presence/leave')`, и сервер ставит `presence_until = now + apply.leave_grace` (5 с). Короткий запас нужен, потому что `pagehide` срабатывает и при переходе на другую страницу UI: новая страница загрузится и снова продлит присутствие.

Откладываются все клиенты, включая Keenetic и чужие устройства, а не только устройство администратора: так проще, и все получают маршруты одновременно.

**Режимы.** Когда план изменился и маршруты добавились, UI показывает три кнопки. Нажимать их не обязательно: после добавления из web UI режим уже выбран (см. ниже).

| Кнопка | Режим | Когда переподключаются клиенты |
|---|---|---|
| Apply now | `now` | Сразу. Соединение администратора тоже прервётся на несколько секунд. |
| Apply when I leave | `on_leave` | Через `apply.idle_delay` после последнего действия в UI или через `apply.leave_grace` после закрытия страницы. Взаимодействие со страницей отодвигает срок. `apply.min_interval` не учитывается. |
| Later | `auto` | Автоматически: не раньше чем через `apply.min_interval` (60 мин) после прошлого переподключения и не пока администратор в UI. |

Режим по умолчанию зависит от того, откуда пришли изменения:
- добавление из web UI (вставка, сканирование, импорт) — `apply.after_add`, по умолчанию `on_leave`. Основной сценарий «вставил, нажал Enter, закрыл окно» доставляет маршруты сразу после закрытия окна без единого нажатия кнопки. Режим передаётся в запросе `apply` вместе с `resolve_domains`. `tick` сначала резолвит новые домены и ставит `on_leave`, только если маршруты действительно добавились. Если окно уже закрыто, срок прошёл, и переподключение выполняется сразу после записи файла;
- всё остальное (новые адреса при плановом резолве, CLI) — `auto`.

Более сильный режим не заменяется более слабым: `now` > `on_leave` > `auto`. Если к сроку в плане не осталось маршрутов с `plan_routes.reconnected = 0` (например, после Undo), `tick` снимает отложенное переподключение без действий. При удалении маршрутов переподключения нет: лишний маршрут не мешает, трафик в эту сеть просто продолжает идти через VPN. `apply.strategy = none` отключает переподключения совсем, остаётся только файл.

**Решение — чистая функция:**

```python
def reconnect_due_at(state: ReconnectState, settings: ApplySettings) -> Timestamp | None:
    match state.pending_mode:
        case None:
            return None
        case PendingMode.ON_LEAVE:
            return state.presence_until or state.pending_since
        case PendingMode.AUTO:
            window = (state.last_reconnect_at or 0) + settings.min_interval
            return max(window, state.presence_until or 0, state.pending_since)
```

`tick` выполняет переподключение, когда `now >= due_at`. Если срок наступает в пределах 55 секунд от старта `tick`, он ждёт, перечитывая `reconnect_state` каждые 2 секунды: присутствие может продлиться, а режим смениться (раздел 4.1).

**Выполнение:** через management `status 3` → список CID → для каждого клиента `client-kill <cid>` с паузой `apply.reconnect_spread` (2 с). `last_reconnect_at` обновляется, `pending_mode` сбрасывается, в `apply_runs` и `events` пишется результат. Management-клиент работает через unix-сокет, у каждой команды свой таймаут. Снимок `CLIENT_LIST` (common name, реальный и виртуальные адреса, время подключения) пишется в `ovpn_clients`, и web UI показывает клиентов, не обращаясь к OpenVPN.

Если management недоступен, файл уже записан, переподключение остаётся отложенным, статус `error`, следующий `tick` повторяет попытку.

## 9. Обнаружение доменов

### 9.1 Разбор ввода

- Текст делится на токены по пробелам, переводам строк и запятым.
- Токен со схемой (`<scheme>://`) разбирается через `urlsplit().hostname`; без схемы (в том числе `//host`) считается `[userinfo@]host[:port][/path]`.
- Нормализация: нижний регистр, без точки на конце, IDNA (UTS46, пакет `idna`) → punycode. В UI имя показывается в Unicode.
- Имя без `www.` длиннее 249 символов невалидно: его пара с `www.` не поместится в 253 символа `names.name`.
- Имя должно состоять хотя бы из двух меток, последняя метка — не из одних цифр: `localhost`, `foo` и `1.2.3.400` нераспознаны.
- `*.example.com` → `example.com` с предупреждением, что wildcard не поддерживается.
- Невалидные токены попадают в список нераспознанного с исходной строкой.
- Дубли игнорируются: токен, чьё имя или www-пара уже есть в `names`, молча пропускается и только считается в итоге. Повторы внутри вставки схлопываются так же: `example.com` и `www.example.com` в одной вставке дают один домен, каноничным становится тот, что встретился первым.
- IP и CIDR не добавляются сами: в итоге появляется кнопка «Add as global route», нажимать её не обязательно. Адрес, в том числе хост URL, становится сетью `/32` или `/128`; CIDR с ненулевыми битами хоста приводится к сети (`1.2.3.4/24` → `1.2.3.0/24`), неверная длина префикса — нераспознанное.
- Разбор — чистая функция `parse_paste(text, existing_names) -> ParseResult[PastePlan(new_domains, duplicates, networks, wildcards)]`: нераспознанное — ошибки `ParseResult` с номером строки и исходным токеном, `wildcards` — имена, введённые как `*.name`. Запись — одна транзакция, которая создаёт `add_batches`, домены и их имена.

### 9.2 Сканирование страниц (отдельная кнопка)

Сканирование запускается кнопкой «Scan pages», а не основной «Add». Это единственный путь добавления с шагом подтверждения.

- Скачиваются только переданные страницы, без обхода ссылок, не больше 20 URL за раз. Таймаут `discovery.timeout`, не больше `discovery.max_bytes`, не больше 5 редиректов, параллельно, общий лимит 30 с.
- Источники доменов: цепочка редиректов, заголовок `Content-Security-Policy`, атрибуты `src`, `href`, `srcset`, `action`, `poster`, `data-src`, `<link rel=preconnect|dns-prefetch>`, `<meta http-equiv=refresh>`, `url(...)` во встроенных стилях, строки `https://host` и `//host` во встроенных скриптах.
- Классификация через Public Suffix List:
  - тот же сайт (тот же регистрируемый домен) — предвыбраны;
  - сторонние (CDN, API, шрифты) — показаны, но не выбраны, сгруппированы по регистрируемому домену;
  - `discovery.ignore` (`w3.org`, `schema.org`, `ogp.me` и т.п.) — перечислены одной строкой в конце, без чекбоксов.
- Для каждого найденного домена сохраняется, где он найден (`script src`, `CSP` …), чтобы показать в UI.
- Защита от SSRF: адреса назначения проверяются после резолва, частные и loopback-адреса запрещены.
- По умолчанию сканирование создаёт одну новую группу, и в неё попадают все выбранные домены, включая хосты самих URL. Имя группы по умолчанию — регистрируемый домен первого URL (`example.com`). Если URL с разных сайтов, к имени добавляется `+N` (`example.com +2`); если имя занято — суффикс ` (2)`. На шаге подтверждения имя можно изменить, выбрать существующую группу или «No group». Группа создаётся только при добавлении хотя бы одного домена.
- Уже отслеживаемые домены (совпадает имя или его www-пара) показаны без чекбокса с пометкой `exists` и в группу не переносятся.

### 9.3 Большие списки

Отдельного пакетного режима нет: 5000 доменов вставляются в ту же форму и добавляются тем же Enter, без подтверждения. Отличается только итог: при больше чем 50 доменах в нём одни счётчики, а сами домены видны в таблице доменов (новые сверху). Резолв идёт как обычно: серверные резолверы — порциями по 500 имён (раздел 4.1), браузер — по `browser.batch` за открытие страницы, агенты — по `agent.batch`. www-пары удваивают число имён. Сканирование для больших списков недоступно (лимит 20 URL). Импорт (раздел 12) нужен, когда вместе с доменами задаются группы, теги или префиксы.

### 9.4 www-пары

- Каноничное имя домена — то, которое пользователь ввёл первым: вставил `www.example.com` — каноничное `www.example.com`, пара `example.com`; вставил `example.com` — наоборот. Под каноничным именем домен показывается в таблицах и пишется в экспорт.
- Пара — каноничное имя с `www.` впереди или без него: `pair_name(name) -> str`, чистая функция над текстом. Имя, которое начинается с `www.`, теряет ровно один `www.`, остальные получают его: `api.example.com` ↔ `www.api.example.com`, у `www.www.example.com` пара `www.example.com`, а не `www.www.www.example.com`. Пара есть у каждого домена; ни PSL, ни домен верхнего уровня, ни существование имён не проверяются: у `www.co.uk` пара `co.uk`, у `www.localhost` — `localhost`.
- Домен и его пара всегда меняются вместе: добавляются одной операцией, настройки, группы и теги у них общие (хранятся у домена), удаление домена удаляет оба имени. Отдельно добавить, удалить или изменить пару нельзя; отдельных пометок пары нет ни в UI, ни в формате экспорта.
- Вставка любого из двух имён уже существующего домена — дубль и игнорируется.
- Резолв, история и адреса у имён свои: у `example.com` и `www.example.com` бывают разные адреса и разные статусы. Таблица доменов показывает оба имени отдельными строками внутри домена (раздел 14.1).
- Пары без адресов стоят только лишних запросов к DNS и предупреждений не дают.

## 10. CLI

`argparse`, вывод — таблица или `--json`. Коды выхода: `0` успех, `1` ошибка, `2` неверные аргументы, `3` частичный успех (например, часть доменов не отрезолвилась).

```
subnet-tracker status [--json]
subnet-tracker add TEXT_OR_URL... | -                            # то же, что вставка в web UI: без подтверждений, дубли пропускаются
subnet-tracker domain list [--group G|none] [--tag T] [--batch ID] [--search S] [--state ok|error|stale|disabled] [--sort last-attempt|last-success|name] [--json]
subnet-tracker domain show NAME
subnet-tracker domain add NAME... [--from-file FILE] [--group G]... [--tag T]... [--prefix4 N] [--prefix6 N] [--comment C] [--no-resolve]
subnet-tracker domain edit NAME... [--add-group G]... [--rm-group G]... [--add-tag T]... [--rm-tag T]... [--prefix4 N|inherit] [--prefix6 N|inherit] [--enable | --disable] [--comment C]
subnet-tracker domain rm NAME... [--yes]
subnet-tracker group list | add NAME [--prefix4 N] [--prefix6 N] [--description D] | edit NAME ... | rm NAME [--with-members] [--yes]
subnet-tracker tag list | rename OLD NEW | rm NAME
subnet-tracker route list | add CIDR [--group G]... [--comment C] | edit CIDR ... | rm CIDR
subnet-tracker resolver list | add NAME --kind system|dns|doh|browser [--endpoint E] [--routes | --no-routes] | edit NAME ... | rm NAME | test NAME [DOMAIN]
subnet-tracker agent list | add NAME [--no-routes] [-o FILE] | show NAME | reissue NAME [-o FILE] | revoke NAME | rm NAME
subnet-tracker settings list | get KEY | set KEY VALUE | reset KEY
subnet-tracker scan URL... [--new-group NAME | --group G | --no-group] [--include-third-party] [--yes]
subnet-tracker batch list [--limit N] | show ID | undo ID
subnet-tracker export [--with-addresses] [-o FILE]
subnet-tracker import FILE|- [--mode merge|replace] [--group G]... [--tag T]... [--dry-run] [--yes]
subnet-tracker resolve [NAME...] [--wait]
subnet-tracker routes list [--kind dns|aggregated|global] [--pending] [--search IP|CIDR|DOMAIN] [--json]
subnet-tracker routes show CIDR [--all-addresses] [--json]      # части, адреса и имена для каждого адреса
subnet-tracker routes explain IP|CIDR                            # какой маршрут покрывает и почему, или почему не покрыт
subnet-tracker address list [--shared] [--search S] [--family 4|6] [--json]
subnet-tracker address show IP [--json]                          # все имена с этим адресом, резолверы, сроки, маршрут
subnet-tracker apply [--dry-run] [--reconnect now|auto|none]
subnet-tracker history resolve|apply|events [--limit N] [--domain NAME] [--resolver NAME]
subnet-tracker tick
subnet-tracker web                                               # запуск web UI (из systemd)
subnet-tracker web hash-password                                 # спрашивает пароль, печатает строку для admin_password_hash
subnet-tracker web devices list | revoke ID
subnet-tracker agent-api
subnet-tracker pki init | show
subnet-tracker db migrate
```

`agent add` и `agent reissue` выводят конфиг агента (в файл с `-o`) и команду `st-agent enroll`. `add` печатает итог: добавлено, пропущено как дубли, нераспознано. Команды `domain` принимают любое из двух имён домена и действуют на домен целиком: `domain rm www.X` и `domain rm X` удаляют оба имени. `scan` без `--yes` выводит кандидатов и спрашивает подтверждение; без `--group` и `--no-group` создаёт новую группу по правилу из раздела 9.2. `batch undo` удаляет домены партии, которые ещё существуют. Команды, меняющие данные, ставят запрос в `requests` и пишут в trigger-файл. `resolver test` для внешних резолверов выдаёт ошибку: они работают только по заданиям. Режима `on_leave` в CLI нет: присутствие отслеживается только в web UI.

## 11. Конкурентность и отказы

- Пишут в БД четыре процесса: web (включая результаты из браузера и присутствие), Agent API, CLI и `tick`. Транзакции короткие; сетевой ввод-вывод выполняется вне транзакций.
- `tick` защищён `flock`; кроме того, systemd не запускает второй экземпляр oneshot-сервиса. Срабатывания таймера, пока идёт `tick`, пропускаются; запросы, пришедшие за это время, обслуживаются между порциями резолва и во время ожидания срока переподключения.
- Management недоступен: файл всё равно пишется, переподключение остаётся отложенным и повторяется следующим `tick`.
- Отказ резолвера или агент не на связи: истечения нет (раздел 6.4).
- Агент оборвался после получения заданий: аренда истекает через 5 минут, и задания выдаются снова.
- Браузер закрыт без `pagehide` (сбой, потеря сети): присутствие истекает само через `apply.idle_delay`.
- Повреждённый или вручную изменённый `routes.conf` перезаписывается: сравнение идёт с файлом на диске.

## 12. Текстовый формат экспорта и импорта

Построчный формат, удобный для ручной правки и `diff`:

```
# subnet-tracker export v1

[settings]
resolve.interval = 60m
routes.default_prefix_v4 = 23

[group "Video"]
description = Video hosting
prefix_v4 = 24

youtube.com                    #video #google
googlevideo.com    /16         #video        @Google
ytimg.com                      #video        ; static content

[ungrouped]
example.org        /22 v6/40   !disabled
www.example.net                             ; каноничное имя с www, пара example.net

[routes]
149.154.160.0/20   @Messengers   ; Telegram ranges
192.168.10.0/24                  ; LAN behind the server
```

Грамматика строки домена: `<domain> [/N] [v6/N] [#tag]... [@extra-group]... [!disabled] [; комментарий]`. Одна строка — один домен под каноничным именем; www-пара в файл не пишется, при импорте она выводится по правилам раздела 9.4. Строка, чьё имя совпадает с парой уже описанного домена, считается дублем: при импорте — предупреждение с номером строки. Секция `[group "X"]` задаёт основную группу для доменов под ней, а `@Y` добавляет домен ещё в одну группу. Строки `key = value` сразу после заголовка группы — её свойства. Строки до первого заголовка считаются `[ungrouped]`, поэтому простой список доменов — тоже допустимый файл. Секция `[addresses]` (`name ip resolver first_seen last_seen`, по каждому имени, включая пары) выгружается только с `--with-addresses`.

Резолверы, агенты и ключи в экспорт не входят. Для полного бэкапа нужны `sqlite3 .backup` и каталог `pki/`.

Импорт:
- `merge` (по умолчанию): новые добавляются, атрибуты перечисленных обновляются, остальное не трогается;
- `replace`: всё, чего нет в файле, удаляется;
- `--group` и `--tag` (в UI — поля формы) назначаются всем доменам из файла, что удобно для пакетной загрузки;
- сначала всегда строится diff (чистая функция), который показывается в CLI (`--dry-run`) и в UI перед подтверждением;
- ошибки разбора выводятся с номерами строк, файл с ошибками не применяется частично.

## 13. Конфигурация

### 13.1 `config.toml`: начальная конфигурация, меняется только руками

```toml
db = "/var/lib/subnet-tracker/state.db"
pki_dir = "/var/lib/subnet-tracker/pki"

[web]
bind = ["10.8.0.1:8080", "127.0.0.1:8080"]
allowed_hosts = []                         # допустимые значения Host; пусто = адреса из bind и localhost
trusted_networks = ["127.0.0.0/8", "::1", "10.8.0.0/24"]   # вход без пароля; CIDR, отдельный IP или диапазон "10.8.0.2-10.8.0.9"; install.sh добавляет tunnel_networks
admin_password_hash = ""                   # пусто = входа по паролю нет, остальным адресам 403
remember_for = "365d"                      # срок cookie устройства, вошедшего по паролю; продлевается при каждом визите
secret_key_file = "/var/lib/subnet-tracker/secret_key"

[agent_api]
listen = ["10.8.0.1:8443"]                 # публичный адрес добавляется сюда и в endpoints при необходимости
endpoints = ["https://10.8.0.1:8443"]      # попадают в конфиги агентов

[routes]
file = "/etc/openvpn/server/subnet-tracker/routes.conf"
server_addresses = ["203.0.113.10"]        # install.sh заполняет адресами интерфейсов
tunnel_networks = ["10.8.0.0/24"]

[[openvpn]]
name = "server"
management = "unix:/run/subnet-tracker/ovpn-server.sock"
```

Файл читается в типизированную `dataclass` (`tomllib` + явная проверка); неизвестные ключи и неверные типы — ошибка запуска.

### 13.2 Настройки в БД: меняются из UI и CLI

Таблица `settings` хранит JSON-значения по ключу. Тип, значение по умолчанию и проверку каждой настройки задаёт реестр в Python: `Setting[T]` с `parse` и `validate`. Сервисы получают один типизированный объект `Settings`. Ключ-значение выбран вместо таблицы со столбцом на каждую настройку, чтобы новая настройка не требовала миграции.

| Ключ | По умолчанию | Назначение |
|---|---|---|
| `resolve.interval` | `60m` | Интервал полного прохода серверных резолверов. |
| `resolve.timeout` | `3s` | Таймаут одного запроса. |
| `resolve.concurrency` | `32` | Общий пул потоков. |
| `resolve.unhealthy_ratio` | `0.5` | Порог для пометки резолвера нездоровым в прогоне. |
| `resolvers.stale_warning` | `2d` | Предупреждать, если отмеченный внешний резолвер не обновлялся дольше. |
| `browser.refresh_mode` | `auto` | `auto`, `button`, `off`. |
| `browser.max_age` | `24h` | m для браузера: записи старше этого обновляются. |
| `browser.batch` | `300` | n для браузера: доменов за одно открытие страницы. |
| `browser.concurrency` | `6` | Одновременных DoH-запросов из браузера. |
| `agent.max_age` | `60m` | m для агентов: записи старше этого отдаются агенту. |
| `agent.batch` | `200` | n для агентов: доменов за один `poll`. |
| `agent.poll_interval` | `60s` | Пауза между опросами в цикле, когда заданий нет. |
| `agent.concurrency` | `8` | Параллельных резолвов в агенте. |
| `agent.timeout` | `5s` | Таймаут одного резолва в агенте. |
| `agent.offline_after` | `30m` | Агент считается неактивным, если молчит дольше. |
| `routes.retention` | `4d` | Сколько держать адрес после исчезновения из ответов резолвера. |
| `routes.default_prefix_v4` | `23` | Префикс по умолчанию. |
| `routes.default_prefix_v6` | `48` | Префикс по умолчанию. |
| `routes.push_ipv4` / `routes.push_ipv6` | `true` / `true` | Отдавать ли клиентам маршруты семейства. |
| `routes.exclude` | `[]` | Дополнительные исключения. |
| `routes.max_v4` / `routes.max_v6` | `1000` / `300` | Бюджет маршрутов. |
| `routes.min_aggregate_prefix_v4` / `_v6` | `12` / `32` | Предел агрегации. |
| `apply.strategy` | `reconnect` | `reconnect` или `none`. |
| `apply.after_add` | `on_leave` | Режим переподключения после добавления из web UI: `on_leave`, `auto`, `now`. |
| `apply.min_interval` | `60m` | Минимальный интервал между автоматическими переподключениями. |
| `apply.idle_delay` | `30s` | Сколько ждать после последнего действия администратора в UI. |
| `apply.leave_grace` | `5s` | Запас после закрытия страницы. |
| `apply.reconnect_spread` | `2s` | Пауза между переподключениями клиентов. |
| `discovery.timeout` | `10s` | Таймаут скачивания страницы. |
| `discovery.max_bytes` | `5MB` | Лимит размера страницы. |
| `discovery.user_agent` | браузерный UA | User-Agent при скачивании. |
| `discovery.ignore` | `["w3.org", "schema.org", "ogp.me"]` | Домены, которые не предлагаются. |
| `history.retention` | `90d` | Хранение `resolve_runs`, `apply_runs`, `events`. |

Резолверы и агенты хранятся в своих таблицах и редактируются на странице Resolvers.

## 14. Web UI

Flask + Jinja2, всё рендерится на сервере. Pico.css (classless) плюс немного своего CSS. Все формы — обычные POST с редиректом обратно и работают без JS. Таблицы приходят с сервера целиком в ответе на загрузку страницы: без пагинации, подгрузки и раскрывающихся блоков, поэтому htmx не нужен. Собственный JS — только `presence.js`, `browser-dns.js` и `page.js` (Enter в форме добавления, фильтр строк таблицы доменов, статус в полосе итога), без логики разбора. UI слушает адрес в VPN и localhost.

**Доступ.** UI открывается сразу, без формы входа: на старте до него можно добраться только через VPN или с localhost, и именно это служит аутентификацией. Решение принимает чистая функция `access_decision(remote_ip, host, sec_fetch_site, origin, device_cookie, config) -> Access`:

1. `Host` не из `web.allowed_hosts` — 421. Это защита от DNS rebinding: чужая страница не может перепривязать своё имя на `10.8.0.1` и читать UI от имени браузера администратора.
2. `remote_ip` из `web.trusted_networks` (по умолчанию localhost и сети туннеля) — доступ без пароля. Элемент списка — CIDR, отдельный адрес (`10.8.0.6`, то есть /32 или /128) или диапазон `first-last`; при загрузке конфига каждый элемент хранится как диапазон адресов `first-last` (CIDR и отдельный адрес тоже становятся диапазонами), а адрес проверяется вхождением в диапазон. Множество доверенных адресов то же, что у сетей из `summarize_address_range`. Берётся адрес TCP-соединения; `X-Forwarded-For` игнорируется, прокси перед UI нет.
3. Иначе — действующая cookie устройства из `web_devices`: доступ, `last_seen_at` и срок cookie продлеваются.
4. Иначе форма входа, если задан `admin_password_hash`. После входа создаётся запись в `web_devices` и cookie на `web.remember_for` (365 дней), так что пароль вводится один раз на браузер. Если пароль не задан — 403 со страницей «open via VPN or localhost».

Сценарий 4 нужен на будущее, если UI откроют наружу; сейчас он не используется. Без пароля особенно важна защита от CSRF: страница с любого сайта, открытая на устройстве в VPN, может отправить форму на `10.8.0.1:8080` с доверенного адреса. Поэтому каждый POST (формы, API, `sendBeacon`) требует CSRF-токен из сессии, а запросы с `Sec-Fetch-Site: cross-site` или чужим `Origin` отклоняются. Сессионная cookie (подписанная Flask, `HttpOnly`, `SameSite=Lax`, срок 1 год) хранит только CSRF-токен и выдаётся молча при первом запросе. В журнал событий автор пишется как `web:<ip>`, а если адрес есть в `ovpn_clients` — с common name клиента.

**Быстрое открытие.** Главная страница (раздел 14.1) отдаётся целиком, поэтому её объём растёт с числом доменов:

| Масштаб | Строк в таблицах | HTML | gzip | Рендер на сервере |
|---|---|---|---|---|
| 125 доменов | ~250 имён, ~400 адресов в маршрутах | ~150 КБ | ~20 КБ | < 100 мс |
| 5000 доменов | ~10 000 имён, ~15 000 адресов | ~6 МБ | ~0,5 МБ | ~1 с |

Чтобы форма добавления была готова сразу при любом масштабе:
- страница отдаётся потоком (`stream_template`) в порядке блоков: шапка, форма, итог, статус, затем большие таблицы. Форма появляется и получает фокус (встроенный `focus()` сразу после поля), пока таблицы ещё грузятся; Enter работает до конца загрузки;
- ответ сжимается gzip на лету с `Z_SYNC_FLUSH` после каждого блока, чтобы поток не задерживался буфером;
- данные для страницы — несколько больших SELECT без запроса на каждую строку; план читается из готовых `plan_state` и `plan_routes`. Сборка модели страницы из строк БД — чистая функция `build_main_page(snapshot) -> MainPage`;
- у блоков таблиц `content-visibility: auto`: браузер не раскладывает невидимые строки; в строках нет JS и встроенных стилей, только классы;
- статика (Pico.css, свои JS и CSS) лежит локально, имена файлов содержат хеш, `Cache-Control: max-age=31536000, immutable`. Браузерный резолв стартует после загрузки и не задерживает страницу.

### 14.1 Главная страница

Адрес `/`, основная и почти единственная рабочая страница. На ней сверху вниз: шапка, форма добавления, итог последнего добавления, действия, статус и предупреждения, таблица доменов, таблица маршрутов, таблица глобальных настроек. Все строки всех таблиц приходят в одном ответе, раскрывающихся блоков и подгрузки нет. Поиск — Ctrl+F браузера; у таблицы доменов есть ещё поле фильтра, которое скрывает строки на клиенте без запросов к серверу.

#### Шапка, форма, итог, действия, статус

```
subnet-tracker                                          Resolvers  Groups  History  Import/Export
Reconnect pending (+2 routes): when you leave — 30s after your last action          [Now] [Later]

+-----------------------------------------------------------------------------------------------+
| Paste URLs or domains                                                                         |
+-----------------------------------------------------------------------------------------------+
Enter adds, Shift+Enter for a new line                                        [Add] [Scan pages...]

Added 2: example.com, www.static.ex.net.  Skipped 1 already tracked: youtube.com.  Not recognized: "foo bar".
1.2.3.0/24 looks like a network: [Add as global route]                         [Undo] [Set group or tags...]
Resolving 4 names: 2 done, 2 waiting.  Routes +2: connected clients get them when you close this page.  [Now] [Later]

[Update all now] [Refresh browser DNS] [Download routes.conf] [Export]

Status    resolve: full run 03:00 (3m 41s), next 04:00; 127 domains, 251 names, 412 addresses
          routes: 987 IPv4 / 112 IPv6; file 03:20 (+2 / -1); clients reconnected 02:31
          OpenVPN: phone-alex (you), keenetic-home, laptop, phone-2
          agents: laptop-alex active (once, 03:12), office-pc silent 2d; browser DNS: 214 names stale
Warnings  office-pc (agent, used for routes) has not reported for 2 days
          foo.example.com: no addresses from any checked resolver for 3 days
          1.2.3.0/23 trimmed by exclusion 1.2.3.4/32 (server address)
          cloudflare-dns.com is routed via VPN: browser DNS reflects server location
```

- Полоса переподключения видна, пока оно отложено; во втором режиме текст `automatically at 03:31, not while you are here [Now] [When I leave]`.
- **Форма** и её обработка — в подразделе «Добавление» ниже.
- **Итог** виден только после добавления (`/?batch=<id>`). Строку «Resolving…» обновляет `page.js` лёгким запросом `GET /api/batch/<id>` раз в 2 секунды, пока резолв не закончится. Это единственный фоновый запрос страницы, кроме присутствия и браузерного DNS; таблицы при этом не меняются, свежие данные видны после перезагрузки.
- **Update all now** ставит полный проход серверных резолверов и выставляет `requested_at` всем именам для внешних резолверов. **Refresh browser DNS** запускает браузерный резолв пачками, пока страница открыта (раздел 6.6).
- «(you)» — клиент, чей виртуальный адрес совпадает с адресом, с которого открыт UI.

#### Добавление

Сценарий: открыть UI → Ctrl+V → Enter (или [Add]) → закрыть окно. На этом пути нет ни одного вопроса и подтверждения; группа, теги и префикс не спрашиваются, их можно поменять потом.

- Поле — `<textarea>`, в фокусе сразу после загрузки. Многострочная вставка сохраняет переводы строк. `page.js` превращает Enter в отправку формы (`requestSubmit()`), Shift+Enter — в перевод строки; во время ввода через IME (`isComposing`) Enter не перехватывается. Без JS форма отправляется кнопкой [Add]. Пустая форма не отправляется.
- `POST /add`: разбор по разделу 9.1, имена по разделу 9.4, дубли пропускаются, новые домены и их имена создаются одной транзакцией без группы и тегов. Затем в `requests` ставятся `resolve_domains` и `apply` с режимом `apply.after_add`, пишется trigger-файл, и ответ — редирект на `/?batch=<id>`. Окно можно закрыть сразу после Enter: дальше всё делает `tick` (раздел 8.5).
- После редиректа поле снова пустое и в фокусе: можно вставлять следующий список. Новые домены — в начале таблицы доменов.
- Сразу после редиректа `browser-dns.js` резолвит новые имена через DoH, если `browser.refresh_mode` не `off`. Если окно закрыто раньше, адреса придут от серверных резолверов и агентов.
- Строка про клиентов появляется, только если маршруты добавились; если новые адреса уже покрыты существующими маршрутами — `Routes: no change, nothing to push`.
- [Undo] удаляет домены партии, которые ещё существуют, и отмечает `add_batches.undone_at`. Подтверждения нет: Undo и есть страховка для добавления без подтверждения. Если маршруты уже ушли в файл, `tick` уберёт их при следующем плане.
- [Set group or tags...] открывает групповое редактирование (раздел 14.3) для доменов партии.
- При больше чем 50 доменах в итоге одни счётчики (раздел 9.3).
- [Scan pages...] — отдельный сценарий с подтверждением (раздел 14.4).

#### Таблица доменов

```
Domains — 127 domains, 251 names                                                     Filter [__________]
[ ] Domain              Groups  Tags   Net   Name                 Status    Addresses                                   Resolved by                               Success  Attempt
[ ] example.com         —       —      /23   example.com          ok        93.184.215.14                               system, Cloudflare, Browser (CF)          03:12    03:12
                                             www.example.com      ok        93.184.215.14                               system, Cloudflare                        03:12    03:12
[ ] youtube.com         Video   video  /24*  youtube.com          ok        142.250.74.14, 142.250.74.46                system, Cloudflare, laptop-alex           03:12    03:12
                                             www.youtube.com      ok        142.250.74.14, 142.250.75.110 (stale 3d)    system, Cloudflare, (Cloudflare DoH)      03:12    03:12
[ ] www.static.ex.net   —       —      /23   www.static.ex.net    ok        93.184.215.20                               system, Browser (CF)                      03:00    03:00
                                             static.ex.net        notfound  —                                           system: NXDOMAIN, Cloudflare: NXDOMAIN    —        03:00
[ ] foo.example.com     Work    —      /23   foo.example.com      notfound  —                                           system: NXDOMAIN, Google: timeout         08.10    03:00
                                             www.foo.example.com  notfound  —                                           system: NXDOMAIN                          —        03:00
Selected: 0   [Resolve] [Enable] [Disable] [Edit groups and tags...] [Delete...]
```

- Домен — группа строк (`<tbody>`): столбцы домена (чекбокс, каноничное имя-ссылка на карточку, группы, теги, префикс) объединены по вертикали (`rowspan`), справа по строке на каждое имя: сначала каноничное, затем пара.
- У каждого имени — свой статус, все его адреса, резолверы и время последнего успеха и последней попытки (`names.last_success_at`, `last_attempt_at`).
- «Addresses»: все известные адреса имени. Адреса, которые сейчас не дают маршрут, приглушены, рядом указана причина: `stale 3d`, `not routed` (только неотмеченные резолверы), `disabled` (домен или все его группы выключены).
- «Resolved by»: резолверы, вернувшие адреса в последнем ответе; неотмеченные — в скобках; резолверы с ошибкой или пустым ответом — с кодом (`Google: timeout`, `system: NXDOMAIN`).
- `*` у префикса — переопределён у домена или группы; в `title` указано, откуда он взят.
- Порядок — новые домены сверху. Заголовки столбцов — ссылки `?sort=name|status|success|attempt`, это обычная перезагрузка страницы.
- Таблица — одна форма: чекбоксы и кнопки групповых действий. [Resolve], [Enable], [Disable] выполняются сразу; [Edit groups and tags...] открывает групповое редактирование (раздел 14.3); [Delete...] ведёт на страницу подтверждения со списком доменов и обоих их имён. Удаление не входит в путь добавления, поэтому подтверждение здесь уместно.
- Фильтр (`page.js`) скрывает группы строк, где ни одно имя, группа или тег не содержит введённого текста. Без JS поле не показывается.

#### Таблица маршрутов

Дерево «маршрут → части → адреса → имена» полностью развёрнуто:

```
Routes — 987 IPv4 / 112 IPv6 (2840 networks before aggregation; budget 1000 / 300; +3.2% address space)
Route / part / address   Kind, state                                        Names                                      Resolved by
142.250.64.0/21          aggregated, in file, reconnect pending
  142.250.64.0/23
    142.250.64.78                                                           youtube.com, www.youtube.com, ytimg.com    system, Cloudflare, laptop-alex
    142.250.65.14                                                           googlevideo.com                            laptop-alex
  142.250.74.0/24        prefix /24 from group Video
    142.250.74.14                                                           youtube.com, www.youtube.com               system, Cloudflare
104.16.0.0/23            dns, in file, clients reconnected
    104.16.5.7                                                              cdn.example.com, static.foo.org, api.bar.net, ...   system, Google
149.154.160.0/20         global "Telegram ranges", group Messengers, in file                                               [Disable] [Delete]
Add global route  CIDR [______________]  comment [______________]  group [— v]  [Add]
```

- У маршрута `dns` с одной частью строка части не выводится, адреса идут сразу под маршрутом.
- У адреса перечислены все имена, для которых он получен, полностью, без сокращения. Так видно, что разные имена разделяют один адрес.
- Резолверы — объединение по всем именам адреса; разбивка по именам — в карточке домена.
- Показываются только адреса, которые дают маршрут; остальные видны в таблице доменов с причиной.
- Глобальные маршруты — строки того же дерева, без адресов; добавление — строкой формы под таблицей; группы и комментарий меняются на странице Groups.
- Удалённые при последнем применении маршруты в дереве не показываются; их видно в History.
- Почему конкретный IP не покрыт маршрутом, объясняет `subnet-tracker routes explain IP`; в UI это Ctrl+F по таблице доменов.

#### Таблица глобальных настроек

Одна форма со всеми настройками из раздела 13.2, сгруппированными по разделам, и кнопкой [Save]. Строка: название, поле, значение по умолчанию, описание, кнопка [Reset], если значение отличается от умолчания. Изменения пишутся в `events`.

```
Settings                                                                                       [Save]
Resolve      every [60m]  timeout [3s]  concurrency [32]  warn if an external resolver is silent [2d]
Resolvers    used for routes: [x] system [x] Cloudflare [x] Google [x] Cloudflare v6 [ ] Cloudflare DoH
                              [x] Browser (CF) [x] laptop-alex [ ] office-pc
Browser DNS  refresh (o) on page open ( ) by button ( ) off;  send [300] oldest names older than [24h]
Agents       send [200] oldest names older than [60m] per poll; loop poll every [60s], concurrency [8],
             timeout [5s], inactive after [30m]
Routes       retention [4d]  default prefix v4 [23] v6 [48]  push IPv4 [x] IPv6 [x]
             budget v4 [1000] v6 [300]  extra exclusions [...]
Apply        strategy [reconnect v]  after adding here [when I leave v]  automatic at most every [60m]
             wait after my last action [30s]  after closing the page [5s]  spread [2s]
Discovery    timeout [10s]  max size [5MB]  user agent [...]  ignore list [...]
History      keep [90d]
Access       read-only, from config.toml: you are 10.8.0.6 (phone-alex), trusted network 10.8.0.0/24;
             trusted: 127.0.0.0/8, ::1, 10.8.0.0/24; allowed hosts: 10.8.0.1:8080, 127.0.0.1:8080, localhost:8080;
             password login: off
```

Блок Access только для чтения: параметры доступа задаются в `config.toml`, чтобы UI не мог расширить круг тех, кому он доверяет. Если пароль включён, под блоком — запомненные устройства (браузер, первый и последний вход, адрес) с [Revoke].

### 14.2 Карточка домена

Открывается по ссылке из таблицы доменов (`/domains/<id>`). Здесь меняются настройки домена и видна подробная история каждого имени.

```
youtube.com                                                    [Resolve now] [Delete...]
Groups [Video x] [+]   Tags [video, google]   Enabled [x]
Prefix v4 [  ] inherited /24 from group Video   Prefix v6 [  ] default /48
Comment [__________________]                                                    [Save]

youtube.com — ok, last success 03:12, last attempt 03:12
  Resolver        Routes  Status       Last success  Last attempt  Details
  system          yes     ok           03:00         03:00         CNAME youtube-ui.l.google.com
  Cloudflare      yes     ok           03:00         03:00         CNAME youtube-ui.l.google.com
  Cloudflare v6   yes     unreachable  —             03:00
  Browser (CF)    yes     ok           10.10 21:14   10.10 21:14   from 10.8.0.6, Android
  laptop-alex     yes     ok           03:12         03:12         agent, DNS 192.168.1.1
  Address              Seen by                          First   Last         State            Route                 Also used by
  142.250.74.14        system, Cloudflare, laptop-alex  02.10   11.10 03:12  active           142.250.74.0/24       www.youtube.com, ytimg.com
  2a00:1450:4001::e    system, Google                   02.10   11.10 03:00  active           2a00:1450:4001::/48   googlevideo.com

www.youtube.com — ok, last success 03:12, last attempt 03:12
  Resolver        Routes  Status       Last success  Last attempt  Details
  system          yes     ok           03:00         03:00         CNAME youtube-ui.l.google.com
  Cloudflare DoH  no      ok           03:00         03:00
  Address              Seen by                          First   Last         State            Route                 Also used by
  142.250.74.14        system, Cloudflare               02.10   11.10 03:12  active           142.250.74.0/24       youtube.com, ytimg.com
  142.250.75.110       Cloudflare DoH (not routed)      09.10   10.10 14:30  stale, 3d left   —                     —

History
  11.10 03:12  address.new      youtube.com 142.250.74.14 (laptop-alex)
  02.10 18:12  domain.added     by 10.8.0.6 (phone-alex), source https://youtube.com/watch?v=...
```

- Настройки общие для обоих имён; у каждого имени — свои таблицы резолверов и адресов, всё развёрнуто.
- «Resolve now» ставит оба имени в очередь серверных резолверов и выставляет им `requested_at` для внешних, чтобы браузер и агенты взяли их первыми.
- [Delete...] удаляет домен с обоими именами после подтверждения.
- «Also used by» — другие имена с тем же адресом.

### 14.3 Групповое редактирование

Открывается из итога добавления ([Set group or tags...], домены партии) и из таблицы доменов ([Edit groups and tags...], выбранные домены). Страница перечисляет домены и предлагает поля: добавить в группу или убрать из группы (в том числе новая группа по имени), добавить и убрать теги, префиксы v4 и v6 (оставить / задать / наследовать), включить или выключить. [Apply] применяет всё одной транзакцией и возвращает на главную.

### 14.4 Scan pages

Вторая кнопка формы добавления; отдельный сценарий с подтверждением (раздел 9.2). URL берутся из поля формы.

```
Scan: https://example.com/some/page
  [x] example.com          new      page host
  [x] static.example.com   new      script src, CSP
  [ ] api.example.com      exists
Third-party domains
  [ ] cdn.jsdelivr.net     script src
  [ ] fonts.gstatic.com    link href
Ignored: w3.org, schema.org, ogp.me
Errors
  https://broken.example — timeout after 10s

Put into  (o) new group [example.com______]   ( ) existing [— v]   ( ) No group
                                                   [Cancel] [Add selected (2)]
```

Строки — домены под тем именем, которое станет каноничным; www-пары не выводятся, они создаются автоматически. После [Add selected] — возврат на главную с той же полосой итога и тем же режимом переподключения, что после вставки.

### 14.5 Resolvers

```
Resolvers — all are queried; only checked ones feed routes
Routes  Name             Kind     Source                                 Updated       Fresh / stale  Status
[x]     system           system   /etc/resolv.conf                       03:00         5000 / 0       ok 4998, err 2, 12 ms
[x]     Cloudflare       dns      udp://1.1.1.1                          03:00         5000 / 0       ok, 9 ms
[x]     Google           dns      udp://8.8.8.8                          03:00         5000 / 0       ok, 15 ms
[x]     Cloudflare v6    dns      udp://[2606:4700:4700::1111]           03:00         —              unreachable
[ ]     Cloudflare DoH   doh      https://cloudflare-dns.com/dns-query   03:00         4990 / 10      ok, 40 ms
[x]     Browser (CF)     browser  https://cloudflare-dns.com/dns-query   10.10 21:14   4100 / 900     last from 10.8.0.6, Android
[x]     laptop-alex      agent    Windows 10, once, DNS 192.168.1.1      03:12         5000 / 0       active, v1.0.0
[ ]     office-pc        agent    Linux, loop, DNS 127.0.0.53            09.10 18:00   0 / 5000       silent 2d
                                                         [+ Add resolver] [+ Add agent]  [Save]
```

Галочка «Routes» — та же, что в таблице настроек на главной; параметры браузерного резолва и агентов тоже там. «Fresh» — имена, по которым у резолвера есть результат моложе его m (для серверных — моложе `resolve.interval`). Клик по строке открывает карточку резолвера: статистика прогонов, ошибки, имена без ответа. Перед удалением резолвера показывается, сколько адресов уйдёт из маршрутов.

Создание агента:

```
Add agent
Name [laptop-alex]     Use for routes [x]                     [Create]
```

После создания (закрытый ключ показывается только сейчас):

```
Agent "laptop-alex" created. The private key is shown only now.

1. Download the agent:  [Windows x86-64 (st-agent.exe, st-agentw.exe)]  [Linux x86-64]
2. Get the config:      [Download st-agent.json]
   or enroll with one command:
   st-agent enroll eyJ2ZXJzaW9uIjoxLCJuYW1lIjoibGFwdG9wLWFsZXgiLCJlbmRwb2ludHMiOlsi...   [Copy]
3. Check:               st-agent test
4. Run on schedule:     Windows: [Copy schtasks command: every 10 min]  [Copy: loop at logon]
                        Linux:   [Download st-agent.service + st-agent.timer]  [Download st-agent-loop.service]

Endpoints: https://10.8.0.1:8443
Server key pin: sha256/3q2+7wAbCdEf...
```

Карточка агента: активен или молчит, режим (loop / once), последний `poll` и последний результат, версия (с пометкой, если устарела), ОС, имя хоста, IP, DNS-серверы, срок сертификата, свежие / устаревшие / ни разу не резолвленные имена, последние сессии. Кнопки: [Reissue credentials] [Revoke] [Delete].

### 14.6 Groups

- Первая строка — «No group»: число доменов без группы, префиксы по умолчанию из таблицы настроек на главной. Выключить её нельзя, переименовать тоже; это не строка в `groups`, а отсутствие строк в `domain_groups`.
- Группы: имя, описание, число доменов и маршрутов, префиксы, переключатель вкл/выкл. При удалении группы выбирается, что делать с участниками, которые входят только в неё: оставить без группы (по умолчанию) или удалить.
- Глобальные маршруты: CIDR, группа, комментарий, вкл/выкл, удаление. Добавляются и строкой формы под таблицей маршрутов на главной.
- Теги: имя, число доменов, переименование (если имя занято — слияние), удаление.

### 14.7 History

- Resolve: прогоны и сессии внешних резолверов (время, тип, резолвер, длительность, новые и истёкшие адреса); у каждой строки ссылка на страницу прогона со статистикой по резолверам (ok / notfound / errors / unreachable, среднее время, healthy).
- Apply: время, IPv4/IPv6, +/−, режим переподключения, клиентов переподключено, статус; полный diff — на странице применения по ссылке.
- Events: журнал с фильтрами по типу, домену, резолверу и автору.
- Графики за 7 и 30 дней (число маршрутов, новые и истёкшие адреса) — SVG, который рендерится на сервере, без JS.

### 14.8 Import/Export

- [Download export], опция «with addresses».
- Импорт: textarea или загрузка файла, режим merge/replace, необязательные группы и теги для всех строк → diff (added / changed / removed, полностью) → [Apply]. Подтверждение здесь остаётся, потому что `replace` удаляет домены. Для простого списка доменов удобнее форма на главной. После применения переподключение идёт в режиме `apply.after_add`, и на главной показывается тот же итог, что после вставки, с [Undo] для добавленного.

## 15. Принципы реализации и качество кода

**Python:**
- Полная типизация, `mypy --strict` без исключений для кода приложения; `Any` в публичных сигнатурах запрещён. Линтер и форматтер — `ruff`.
- Доменные типы — `@dataclass(frozen=True, slots=True)`; идентификаторы и время — `NewType` (`DomainId`, `NameId`, `ResolverId`, `Timestamp`); перечисления — `StrEnum`, совпадающие с `CHECK` в SQL. Тест сверяет каждое `StrEnum` со списком значений в схеме.
- Доступ к БД — написанный руками SQL в модулях-репозиториях без ORM. Строки `sqlite3.Row` преобразуются в dataclass явными функциями; наружу из репозиториев выходят только типизированные объекты.
- Вычисления — чистые функции над неизменяемыми снимками: план маршрутов, бюджет, выбор заданий, решение о переподключении, классификация ответов, разбор ввода и импорта, diff. Побочные эффекты — тонкий слой вокруг них.
- Разбор и проверка возвращают результат с типизированным списком ошибок (`ParseResult[T]`) вместо исключений: ошибки нужно показать пользователю все сразу и с номерами строк. Исключения остаются для отказов ввода-вывода.
- Каждая завершённая единица работы — в своём модуле; модули небольшие.

**SQL:** все таблицы `STRICT`, перечисления, флаги и JSON ограничены `CHECK`, связи — внешними ключами с явным `ON DELETE`. Схема меняется только миграциями.

**Go (агент):** только стандартная библиотека, `go vet`, табличные тесты; сетевой и DNS-слой за интерфейсами, чтобы тестировать цикл без сети.

**JS:** только транспорт, без логики, которую нужно тестировать; `// @ts-check` и JSDoc-типы.

**Тесты пишутся до реализации:** для ошибки сначала падающий тест, для новой функции — тест, задающий поведение. Проверка перед коммитом: `ruff check`, `ruff format --check`, `mypy --strict`, `pytest`, `go vet ./...`, `go test ./...`.

## 16. Безопасность

- Web UI слушает адрес в VPN и localhost и пускает без пароля с `web.trusted_networks` (раздел 14, «Доступ»). Чтобы доверие к адресу источника было обоснованным, `install.sh` добавляет правило nftables: порт UI принимается только с интерфейса туннеля и `lo`. Иначе хост из LAN сервера мог бы прописать маршрут на `10.8.0.1` через сервер. Плюс `rp_filter=1` на внешних интерфейсах.
- Доверие к сети туннеля распространяется на всё, что за клиентом с NAT. Если Keenetic пускает в туннель домашнюю сеть через NAT, любое устройство в ней (в том числе гостевое) откроет UI. Сузить круг можно: выдать клиентам постоянные адреса через `ifconfig-push` в их ccd-файлах и оставить в `trusted_networks` только адреса или диапазон нужных устройств. По умолчанию доверяется вся сеть туннеля.
- Localhost доверен целиком: любой локальный пользователь сервера получает доступ к UI. На однопользовательском сервере это допустимо; иначе `127.0.0.0/8` убирается из `trusted_networks`, и вход идёт по паролю.
- Защита от CSRF: CSRF-токен на всех POST (включая `sendBeacon`: токен передаётся в теле), отказ при `Sec-Fetch-Site: cross-site` и чужом `Origin`. Защита от DNS rebinding: проверка `Host` по `web.allowed_hosts`.
- Вход по паролю (когда включён): pbkdf2, ограничение частоты попыток по адресу, токен устройства — 32 случайных байта, в БД хранится только SHA-256, отзыв из блока Access на главной и из CLI. Прав на OpenVPN у web UI нет.
- API результатов из браузера: только для запросов, прошедших `access_decision`, валидация по разделу 6.5.
- Agent API: по умолчанию только адрес в туннеле; mTLS с CA агентов, TLS 1.3, отзыв через БД, лимиты на соединения и размер запросов, валидация по разделу 6.5. Закрытые ключи агентов на сервере не хранятся; ключ CA лежит в `pki/` с правами 0700.
- Скомпрометированный агент может подсунуть адреса только для существующих доменов; они проходят исключения и бюджет. Галочка «в маршрутах» и отзыв ограничивают ущерб.
- Сканирование страниц: защита от SSRF, лимиты по размеру и времени.
- Все домены и CIDR валидируются; встроенные исключения защищают туннель и локальные сети; глобальный маршрут, который покрывает адрес сервера, отклоняется.
- Management-сокет — unix в каталоге с доступом по группе.

## 17. Тестирование

`pytest` с временной БД, к которой применены все миграции; `hypothesis` — для свойств алгоритмов маршрутов; `go test` для агента.

| Область | Что проверяется |
|---|---|
| Схема | Каждый `CHECK` отвергает недопустимые значения, `STRICT` отвергает чужие типы; `StrEnum` совпадают со схемой; миграции применяются к пустой БД и к БД предыдущей версии. |
| `routes.plan` | Наследование префикса, конфликт нескольких групп; правило «хотя бы одна группа включена»; урезание исключениями; глобальные маршруты обходят исключения; граница `retention`; учёт только отмеченных и не отозванных резолверов; флаги семейств; `provenance`: у агрегированного маршрута все части, адрес нескольких доменов — один адрес со всеми именами, удаление одного из таких доменов маршрут не убирает. |
| `core` (адреса и план) | Запрос по диапазону `ip_bin` на границах сети для IPv4 и IPv6 (первый и последний адрес входят, соседние — нет); группировка строк `addresses` по IP; сохранение плана в `plan_*` только при смене хеша; `reconnected` сбрасывается у новых маршрутов и выставляется после переподключения. |
| `web.access` | `access_decision` для всех веток: чужой `Host` — 421; localhost и сеть туннеля пускаются без пароля; `trusted_networks` из CIDR, отдельных адресов и диапазонов `first-last` (IPv4 и IPv6, неверный диапазон — ошибка конфига); адрес вне доверенных без cookie — вход или 403 (если пароля нет); действующая cookie продлевается, отозванная — нет; `X-Forwarded-For` не влияет; POST без CSRF-токена или с `Sec-Fetch-Site: cross-site` отклоняется. |
| `routes.budget` | Свойства (`hypothesis`): каждая входная сеть покрыта результатом; результат не пересекает исключения; число маршрутов не больше бюджета, если он достижим; результат детерминирован. Плюс примеры с ожидаемым минимальным лишним пространством. |
| `resolver` | Классификация ответов `dnspython` и DoH JSON от браузера (фикстуры Cloudflare и Google: A, AAAA, CNAME, NXDOMAIN, HTTP-ошибки); `unreachable`; отказ не вызывает истечения; `unhealthy_ratio`; истечение по каждой паре имя × резолвер; обновление `names.last_attempt_at` и `last_success_at`; у двух имён домена независимые результаты и адреса. |
| `core/external` | Выбор заданий: новые первыми, `requested_at`, порог m по `attempted_at` (с ошибками), аренда, ровно n самых старых; приём и валидация результатов; сессии. |
| `openvpn.reconnect` | `reconnect_due_at` для всех режимов: `on_leave` отодвигается присутствием и не смотрит на `min_interval`; `auto` ждёт и окна, и конца присутствия; `now` срабатывает сразу; удаление маршрутов не ставит переподключение. Присутствие: heartbeat продлевает, leave сокращает до `leave_grace`, переход между страницами не вызывает переподключения. |
| `openvpn` | Подставной management-сервер на unix-сокете: разбор `status 3`, `client-kill` с паузой, ожидание срока внутри `tick`, повтор при недоступности. |
| `agentapi`, `pki` | Соединение без сертификата и с сертификатом чужого CA обрывается при рукопожатии; отозванный — 403; выпуск, перевыпуск, отзыв. |
| `st-agent` | Разбор конфига и `enroll`-токена; проверка пина (свой ключ проходит, чужой — нет); классификация ошибок резолва; `once` (одна пачка), `once --all` (до пустого ответа, предел раундов, коды выхода); цикл и backoff против подставного сервера. |
| Взаимодействие | Настоящий `st-agent` против настоящего `agent-api` с временным PKI: регистрация, `poll`, результаты в БД, отзыв. |
| `discovery` | `parse_paste`: IDN, порты, userinfo, IP и CIDR (уходят в `networks`), wildcard, повторы во вставке, дубли с существующими, нераспознанное, имя без `www.` длиннее 249 символов невалидно, а с `www.` длиной 253 — допустимо. Извлечение доменов из HTML-фикстур, классификация по PSL, имя новой группы сканирования (один сайт, несколько сайтов, занятое имя). |
| Имена и пары | `pair_name` для `example.com`, `www.example.com`, `api.example.com`, `www.co.uk`, `www.www.example.com`, IDN — только по тексту, без PSL; каноничным остаётся введённое первым (`www.X` → пара `X` и наоборот); триггер отклоняет пару, которая отличается не на `www.`; у домена не больше одного каноничного имени и одной пары, новый домен создаётся сразу с обоими; удаление домена удаляет оба имени и их адреса; вставка любого из двух имён существующего домена — дубль; предупреждение «not found» только когда адресов нет ни у одного имени. |
| Добавление | Одна транзакция на партию; дубли не меняют существующие домены; `example.com www.example.com` в одной вставке — один домен с каноничным `example.com`; итог считает добавленные, дубли и нераспознанное; Undo удаляет только оставшиеся домены партии; `apply.after_add` ставит `on_leave` только если маршруты добавились, а окно, закрытое до конца резолва, даёт переподключение сразу после записи файла. |
| `textio` | Round-trip экспорт → импорт, простой список доменов, группы через `@`, каноничное имя с `www` сохраняется, строка с именем-парой уже описанного домена — дубль с предупреждением, ошибки с номерами строк, diff для merge и replace. |
| `web.main_page` | `build_main_page` над снимком: домен с двумя именами даёт две строки с общими столбцами домена; у каждого имени свои адреса, статус, резолверы и времена; приглушённые адреса с причиной; дерево маршрутов (часть не выводится у `dns` с одной частью; у адреса все имена); настройки со значениями по умолчанию. Число SQL-запросов на страницу не зависит от числа доменов. |
| `web`, `cli` | Основные сценарии через Flask test client и вызов `main(argv)`: вставка → редирект на главную с итогом без промежуточных шагов, главная содержит все строки таблиц без дополнительных запросов, групповое редактирование, удаление с подтверждением, Scan с новой группой, API присутствия, три кнопки применения, сохранение настроек. |
| Интеграция | OpenVPN в network namespace или контейнере: клиент получает маршруты при подключении и после `client-kill`. |

## 18. Этапы

1. Каркас проекта (`pyproject.toml`, `ruff`, `mypy --strict`, `pytest`), схема и миграции с тестами ограничений и триггеров, типизированные репозитории, `parse_paste`, www-пары и партии, CLI (`add`, домены, группы, теги, резолверы, глобальные маршруты, настройки, `batch`); `routes.plan`, `routes.budget`, `textio`.
2. Серверные резолверы, `tick`, systemd timer и path.
3. Применение: файл, сохранение плана в `plan_*`, `client-connect`-скрипт, management, решение о переподключении и ожидание срока в `tick`; `install.sh` с фрагментом `server.conf` и правилом nftables для порта UI.
4. Web UI: доступ (`access_decision`, CSRF, `Host`); главная страница целиком — форма и итог (Undo, Set group or tags), действия, статус, таблицы доменов, маршрутов и настроек, потоковая отдача и gzip; карточка домена, групповое редактирование, присутствие и три кнопки применения, Resolvers без агентов, History; CLI `routes show`, `address list/show`.
5. Внешние резолверы: `core/external`, резолв из браузера.
6. Агент: `pki`, `agent-api`, `st-agent` (цикл и одиночный запуск), unit-файлы и команды планировщика, создание и отзыв агентов в UI и CLI.
7. Сканирование страниц с новой группой, Import/Export в UI, графики, вход по паролю и `web_devices` (для доступа не через VPN).

## 19. Структура репозитория

```
pyproject.toml
src/subnet_tracker/
  __main__.py
  config.py
  types.py    NewType, StrEnum, общие dataclass
  core/       db.py, migrations/*.sql, domains.py, names.py, batches.py, groups.py, tags.py, resolvers.py, agents.py, external.py, global_routes.py, settings.py, events.py, addresses.py, plan_store.py, web_devices.py
  resolver/   run.py, backends.py, classify.py, doh_json.py, expire.py
  routes/     plan.py, budget.py, render.py
  openvpn/    mgmt.py, apply.py, reconnect.py
  discovery/  parse.py (parse_paste), www.py, fetch.py, extract.py, scan_group.py
  textio/     export.py, parse.py, diff.py
  pki/        ca.py, issue.py
  agentapi/   server.py, handlers.py
  cli/        main.py, commands/*.py
  web/        app.py, access.py, csrf.py, gzip_stream.py, main_page.py (build_main_page), views/*.py (main.py, domain.py, bulk_edit.py, scan.py, ...), api/ (browser_dns.py, presence.py, batch_status.py), templates/, static/ (pico.min.css, presence.js, browser-dns.js, page.js)
tests/
agent/
  go.mod
  cmd/st-agent/main.go
  internal/   config/, client/, resolve/, proto/, sysdns/, runner/
deploy/
  systemd/    subnet-tracker-web.service, subnet-tracker-agent-api.service, subnet-tracker-tick.{service,timer,path}
  tmpfiles.d/ subnet-tracker.conf
  openvpn/    ovpn-client-connect, server.conf.snippet
  agent/      st-agent.service, st-agent.timer, st-agent-loop.service
  install.sh
docs/design.md
```

Зависимости сервера (PyPI в venv `/opt/subnet-tracker`): `flask`, `waitress`, `dnspython[doh]`, `httpx`, `idna`, `publicsuffixlist`, `cryptography`. Для разработки: `pytest`, `hypothesis`, `mypy`, `ruff`. Агент: только стандартная библиотека Go.

## 20. Рассмотренные альтернативы

- **Include через `config` + SIGHUP.** Каждое изменение перезапускает сервер и рвёт соединения всех клиентов. Отвергнуто.
- **PUSH_UPDATE.** Клиенты 2.6 его не поддерживают, а на сервере 2.7 одна команда вмещает около 25 маршрутов. Вернуться, если клиенты обновятся до 2.7 и в OpenVPN появится `reload-push-options sync` ([PR #916](https://github.com/OpenVPN/openvpn/pull/916)).
- **Отдельные таймеры для резолва и применения.** Появляются гонки и задержка между шагами. Заменено одним `tick`.
- **Таймер `tick` с шагом 5–10 секунд ради сроков переподключения.** Тысячи запусков Python в сутки впустую. Вместо этого `tick` ждёт близкий срок сам.
- **Регистрация агента через CSR и одноразовый токен.** Ключ генерируется на устройстве и не покидает его. Но серверу нужен эндпоинт, принимающий соединения без клиентского сертификата, то есть открытый для всех. Выбрана выдача готового ключа через web UI по защищённому туннелю.
- **Агент на Python или как PowerShell-скрипт.** Python тянет интерпретатор и пакеты, PowerShell не покрывает Linux, а TLS с клиентским сертификатом в нём неудобен. Go даёт один статический бинарник для обеих платформ.
- **Отдельный TLS-терминатор (nginx) перед Agent API.** Ещё один компонент на сервере; стандартного `ssl` в Python достаточно для 1–10 агентов.
- **Пагинация, подгрузка и раскрывающиеся блоки (htmx).** Страница была бы лёгкой при любом масштабе, но данные пришлось бы искать по частям, а Ctrl+F находил бы только видимое. Выбрана одна страница со всеми данными, отдаваемая потоком.
- **www-пара как отдельный домен со ссылкой на каноничный.** Пару можно было бы удалить или изменить отдельно, и понадобились бы пометки в UI и формате экспорта. Выбрано разделение «домен — имена»: пара существует только как второе имя домена.
- **Разбор DoH-ответов в JS.** Логику пришлось бы дублировать и тестировать в JS. Браузер пересылает ответы как есть, разбор — в Python.
- **Маршрутизация через подмену DNS (fake-IP).** Клиентам пушится DNS-сервер VPN и один маршрут на служебный диапазон; сервер отвечает на отслеживаемые домены фиктивными адресами и делает NAT. Плюсы: точная маршрутизация по домену, wildcard, маршруты не меняются, нет проблемы с их числом и переподключениями. Минусы: весь DNS клиентов идёт через VPN, нужны свой DNS-сервер и NAT на сервере. При 5000 доменах это серьёзный кандидат на отдельный режим позже.

## 21. Открытые вопросы

1. При 5000 доменах главная страница весит около 6 МБ HTML (около 0,5 МБ в gzip), и на телефоне таблицы будут дорисовываться несколько секунд; форма при этом готова сразу (раздел 14, «Быстрое открытие»). **[предположение: допустимо, отдаём всё]** Если окажется медленно, вариант без подгрузки и раскрытий — настройка, которая выводит на главной только домены, добавленные или изменившиеся за последние N дней, а полный список — отдельной страницей того же вида.

Решено в последних кругах:
- переподключение откладывается для всех клиентов (раздел 8.5);
- `trusted_networks` принимает CIDR, отдельные адреса и диапазоны; по умолчанию — вся сеть туннеля и localhost (раздел 14, «Доступ»);
- добавление без подтверждений, дубли пропускаются, после добавления из UI режим `on_leave` (разделы 9.1, 14.1, 8.5);
- домен и его www-пара — одна запись с двумя именами: меняются и удаляются только вместе, каноничное имя — введённое первым, резолв и адреса у имён свои (разделы 5, 9.4);
- главная страница содержит форму, действия, таблицы доменов, маршрутов и настроек, всё приходит одним ответом (раздел 14.1).
