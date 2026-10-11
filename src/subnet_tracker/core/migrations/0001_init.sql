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
