**EN | BG below**

# 🛰️ AISMixer — AIS NMEA‑0183 multiplexer / deduplicator / **tag‑aware forwarder**

**Keywords:** AIS software, Automatic Identification System, NMEA 0183, AIVDM, AIVDO, multiplexer, deduplication, tag block, `s`/`c`/`g`, UDP, secure UDP, ECDSA, AES‑GCM, Raspberry Pi.

> **TL;DR**
> AISMixer merges multiple AIS receiver feeds, de‑duplicates messages, reassembles multipart using NMEA fragment fields, and forwards a clean, unified stream.
> It is **tag‑aware**: reads `s`/`c`/`g` on ingress and (per policy) **preserves / normalizes / overwrites** them on egress — e.g., pass through `c` or replace with server time; keep/normalize `s`; preserve or emit compact `g` metadata. TAG `g` is not used as the multipart assembler key.

---

## 🌿 Branches and website

This `main` branch is the primary runtime and development branch. It contains
the Python service, secure proxy helpers, runtime configuration examples, and
the test suite under `tests/`.

The public website lives on the long-lived `website` branch. GitHub Pages
deploys from that branch using `/docs` as the site root, so `docs/` is
intentionally not present on `main`.

---

## 🧭 What is AISMixer?

**AISMixer** is a Python service that aggregates AIS NMEA‑0183 (AIVDM/AIVDO) from multiple receivers, removes duplicates, reassembles multipart messages, and forwards a single logical feed to marine platforms (e.g., MarineTraffic / AISHub / VesselTracker) or your own services.

- **`aismixer`** is the mixer, deduplicator, normalizer, and tag‑aware forwarder.
- **`nmea_sproxy`** is a client-side secure UDP shovel/proxy. It does not mix
  streams; one process forwards one local UDP input to one encrypted AISMixer
  SEC input.
- 🔐 Supports **plain UDP** and **encrypted inputs** via an ECDSA handshake + AES‑GCM transport (with the lightweight client proxy `nmea_sproxy`).
- 🧩 Tag‑aware end‑to‑end (reads/manages `s`/`c`/`g`).
- 📦 Clean output as if from one logical station.

---

## 🔀 Core flow

1. Multiple AIS receivers (hardware/software) send NMEA to AISMixer (UDP or secure via `nmea_sproxy`).
2. AISMixer **de‑duplicates** identical payloads from different sources.
3. AISMixer **reassembles multipart** AIVDM using NMEA fragment fields: ingress source/assembler key, sequential message ID (`seq_id`), radio channel, current fragment number, and total fragment count.
4. AISMixer **forwards** a unified stream downstream (per‑forwarder tag policy).

```
+------------+    UDP      +-----------+      +----------------+
| Receiver A | ----------> |           | ---> | MarineTraffic  |
+------------+             |           |      | AISHub         |
                           | AISMixer  | ---> | VesselTracker  |
+------------+ Encrypted   |           |      | etc.           |
| nmea_sproxy| ----------> |           |      +----------------+
+------------+             +-----------+
```

---

## ⚙️ Tag‑aware forwarding (Tag Block `s` / `c` / `g`)

AISMixer parses Tag Block on ingress and **manages** what to emit on egress **per policy**:

- **`g` (group)** — ingress/output metadata that may be preserved or regenerated for downstream readers.
  It is **not** the assembler grouping key; multipart assembly uses NMEA fragment fields plus the ingress source/assembler key.
- **`s` (source)** — preserve incoming `s`, map by IP/authorized key, or set a server‑defined station ID.
- **`c` (timestamp)** — pass through incoming time **or** replace it with **server time** (for clock normalization).

### 🧭 Egress tag policy (per output; beta)

- `preserve` — pass through incoming field (if present).
- `normalize` — rewrite into canonical form (e.g., compact `g`, sanitized `s`).
- `overwrite` — ignore ingress and emit server value (e.g., server time for `c`).

---

## 📦 Configuration (`config.yaml`)

```yaml
station_id: mixstation_1   # if non-empty, it always becomes s=
debug: true

sec_inputs:
  - id: secA               # optional; if station_id empty, s=secA
    listen_ip: "::"
    listen_port: 29999

udp_inputs:
  - listen_ip: "0.0.0.0"
    listen_port: 17777
    id: udpA               # optional; if station_id empty, s=udpA
  - listen_ip: "::"
    listen_port: 17777

forwarders:
  - host: 203.0.113.10
    port: 5000

udp_alias_map_file: udp_alias_map.yaml   # optional

# (beta) Optional tag policy — may land as per-forwarder or global
# tag_policy:
#   s: preserve    # preserve | normalize | overwrite
#   c: overwrite   # use server time
#   g: normalize   # preserve/regenerate output TAG g metadata; not assembler key
```

### 🔍 How `s` (source) is formed

Priority:

1. If `station_id` is non‑empty → **s = station_id**
2. Else, if the input has an `id` (incl. `sec_inputs[].id`) → **s = input.id**
3. Else:
   - UDP: if remote IP exists in `udp_alias_map.yaml` → **s = alias**
   - SEC: if a name exists in `authorized_keys.yaml` → **s = client_name** (else `ANONYMOUS`)
4. Else, if the incoming line already carries `\s:…\` → **s = that value**
5. Else → **s = IP** (dots/colons replaced with `_`)

All variants are sanitized to `[A–Za–z0–9_]` and limited to **15** characters.

### 📦 UDP IP→alias map (`udp_alias_map.yaml`)

```yaml
"127.0.0.1": "lo_alias"
"2001:db8::1234": "dock_gate"
```

---

## 🧭 Components

| Module/Dir              | Role |
|-------------------------|------|
| `aismixer.py`           | Main mixer process (UDP + secure inputs) |
| `aismixer_secure.py`    | ECDSA handshake, decryption, authentication |
| `nmea_sproxy/`          | One-input-to-one-secure-output client-side UDP shovel/proxy |
| `assembler.py`          | Multipart reassembly using NMEA fragment fields |
| `dedup.py`              | Duplicate detection/removal |
| `meta_writer.py`        | Adds NMEA tag block/prefix and CRC |
| `meta_cleaner.py`       | Removes non-standard meta headers |
| `forwarder.py`          | Sends the clean output to destinations |
| `config.yaml`           | Inputs/outputs, station ID, debug, (tag policy — beta) |
| `authorized_keys.yaml`  | Allowed client public keys (for secure inputs) |

---

## 🚀 Running

```bash
python3 aismixer.py
```

Or install as a **systemd** service using `install.sh` (copies unit to `/etc/systemd/system/`).

---

## 🔐 Secure UDP / `nmea_sproxy`

`nmea_sproxy` is the active client side of a secure UDP relation:

```text
one local UDP input -> one encrypted AISMixer SEC input
```

Stations authenticate with ECDSA, and AISMixer authorizes their public keys
through `authorized_keys.yaml`. AIS data and ping/pong liveness messages use
authenticated AES-GCM encryption. Encrypted pings help keep NAT, CGNAT, and
mobile-client mappings alive; authenticated encrypted pongs prove that the
configured peer still holds the session.

If the server has lost a session, it may send unauthenticated `NOSESSION` as a
reconnect hint. The client also reconnects when authenticated replies stop for
`peer_timeout`. `session_refresh_interval` defaults to `0`, which disables
planned periodic refresh. A changed client source IP or port requires a new
handshake; session migration is not implemented.

```bash
cd nmea_sproxy && python3 nmea_sproxy.py
sudo systemctl start nmea_sproxy
sudo systemctl start nmea_sproxy@boat
sudo systemctl start nmea_sproxy@yacht
```

Template names such as `boat` and `yacht` are operator-chosen labels, not fixed
`station1` / `station2` names. Session recovery improves long-running operation,
but UDP packet loss remains possible and delivery of every AIS sentence is not
guaranteed. See [`nmea_sproxy/README.md`](nmea_sproxy/README.md) for the detailed
operator guide.

---

## ✅ Tests

Focused pytest coverage lives under `tests/` and covers multipart assembly,
TAG `s`/`c`/`g` helpers, metadata writing, `nmea_sproxy` extraction, secure UDP
helpers, and forwarding-loop behavior.

```bash
python -m pytest
```

---


# 🇧🇬 AISMixer — AIS NMEA‑0183 мултиплексор / дедупликатор / **tag‑aware форурдър**

**Ключови думи:** AIS софтуер, Automatic Identification System, NMEA 0183, AIVDM, AIVDO, multiplexer, deduplication, tag block, `s`/`c`/`g`, UDP, secure UDP, ECDSA, AES‑GCM, Raspberry Pi.

> **TL;DR**
> AISMixer обединява няколко AIS приемни потока, премахва дубликати, сглобява мултипарт чрез NMEA fragment полета и излъчва чист, обединен поток.
> Системата е **tag‑aware**: чете `s`/`c`/`g` на вход и (според политика) **preserve / normalize / overwrite** на изход — напр. запазва `c` или го заменя със сървърно време; запазва/нормализира `s`; запазва или излъчва компактен `g` като metadata. TAG `g` не се използва като ключ за multipart сглобяване.

---

## 🌿 Клонове и сайт

Този `main` клон е основният runtime/development клон. Тук са Python услугата,
secure proxy помощните файлове, примерните runtime конфигурации и тестовете в
`tests/`.

Публичният сайт е в дългоживеещия `website` клон. GitHub Pages deploy-ва от
този клон, като използва `/docs` за site root, затова `docs/` умишлено не
присъства в `main`.

---

## 🧭 Какво е AISMixer?

**AISMixer** е Python услуга, която агрегира AIS NMEA‑0183 (AIVDM/AIVDO) от няколко приемника, премахва дубликати, сглобява мултипарт съобщения и препраща един логически поток към външни платформи или ваши услуги.

- **`aismixer`** е миксерът, дедупликаторът, нормализаторът и tag‑aware
  форурдърът.
- **`nmea_sproxy`** е клиентско secure UDP прокси за еднопосочно препращане. То
  не смесва потоци; един процес препраща един локален UDP вход към един
  криптиран AISMixer SEC вход.
- 🔐 Поддържа **обикновен UDP** и **защитен вход** чрез ECDSA handshake + AES‑GCM транспорт (клиентско прокси `nmea_sproxy`).
- 🧩 Tag‑aware от край до край (чете/управлява `s`/`c`/`g`).
- 📦 Чист изход сякаш е от една логическа станция.

---

## 🔀 Основен поток

1. Няколко приемника (хардуерни/софтуерни) изпращат NMEA към AISMixer (UDP или защитено чрез `nmea_sproxy`).
2. AISMixer **дедупликира** еднакви полезни товари от различни източници.
3. AISMixer **сглобява мултипарт** AIVDM чрез NMEA fragment полета: ingress source/assembler key, sequential message ID (`seq_id`), radio channel, current fragment number и total fragment count.
4. AISMixer **форурдва** обединения поток (per‑forwarder tag политика).

```
+------------+    UDP      +-----------+      +----------------+
| Receiver A | ----------> |           | ---> | MarineTraffic  |
+------------+             |           |      | AISHub         |
                           | AISMixer  | ---> | VesselTracker  |
+------------+ Encrypted   |           |      | и др.          |
| nmea_sproxy| ----------> |           |      +----------------+
+------------+             +-----------+
```

---

## ⚙️ Tag‑aware форурдване (Tag Block `s` / `c` / `g`)

AISMixer чете Tag Block на входа и **решава** какво да излъчи на изхода **по политика**:

- **`g` (group)** — ingress/output metadata, която може да бъде запазена или регенерирана за downstream получатели.
  Не е assembler grouping key; multipart assembly използва NMEA fragment полета плюс ingress source/assembler key.
- **`s` (source)** — запази входния `s`, мапни по IP/ключ, или задай сървърен station ID.
- **`c` (timestamp)** — пропусни входното време **или** замени със **сървърно време** (нормализация на часовниците).

### 🧭 Политика на изход (per output; beta)

- `preserve` — запази входното поле (ако е налично).
- `normalize` — пренапиши в канонична форма (напр. компактен `g`, sanitized `s`).
- `overwrite` — игнорирай входа и издай сървърна стойност (напр. сървърно време за `c`).

---

## 📦 Конфигурация (`config.yaml`)

```yaml
station_id: mixstation_1   # ако е непразно → винаги става s=
debug: true

sec_inputs:
  - id: secA               # по избор; ако station_id е празно, s=secA
    listen_ip: "::"
    listen_port: 29999

udp_inputs:
  - listen_ip: "0.0.0.0"
    listen_port: 17777
    id: udpA               # по избор; ако station_id е празно, s=udpA
  - listen_ip: "::"
    listen_port: 17777

forwarders:
  - host: 203.0.113.10
    port: 5000

udp_alias_map_file: udp_alias_map.yaml   # по избор

# (beta) По избор — политика за тагове (per‑forwarder или глобална)
# tag_policy:
#   s: preserve
#   c: overwrite   # сървърно време
#   g: normalize   # preserve/regenerate output TAG g metadata; not assembler key
```

### 🔍 Как се формира `s`

Приоритет:

1. Ако `station_id` е непразно → **s = station_id**
2. Иначе, ако входът има `id` (вкл. `sec_inputs[].id`) → **s = input.id**
3. Иначе:
   - UDP: ако remote IP е в `udp_alias_map.yaml` → **s = alias**
   - SEC: ако има име в `authorized_keys.yaml` → **s = client_name** (иначе `ANONYMOUS`)
4. Иначе, ако входящият ред вече носи `\s:…\` → **s = тази стойност**
5. Иначе → **s = IP** (точки/двуеточия → `_`)

Всички варианти се sanitize‑ват до `[A–Za–z0–9_]` и лимит **15** символа.

### 📦 UDP IP→alias (`udp_alias_map.yaml`)

```yaml
"127.0.0.1": "lo_alias"
"2001:db8::1234": "dock_gate"
```

---

## 🧭 Компоненти

| Компонент              | Роля |
|------------------------|------|
| `aismixer.py`          | Основен процес (UDP + secure входове) |
| `aismixer_secure.py`   | ECDSA handshake, дешифриране, автентикация |
| `nmea_sproxy/`         | Клиентско UDP прокси: един вход към един защитен изход |
| `assembler.py`         | Сглобяване на мултипарт чрез NMEA fragment полета |
| `dedup.py`             | Премахване на дубликати |
| `meta_writer.py`       | Добавя NMEA tag block/префикс и CRC |
| `meta_cleaner.py`      | Премахва нестандартни мета‑хедъри |
| `forwarder.py`         | Изпраща изчистения изход към дестинациите |
| `config.yaml`          | Входове/изходи, station ID, debug, (tag политика — beta) |
| `authorized_keys.yaml` | Публични ключове на разрешените клиенти |

---

## 🚀 Стартиране

```bash
python3 aismixer.py
```

Или като **systemd** услуга чрез `install.sh`.

---

## 🔐 Secure UDP / `nmea_sproxy`

`nmea_sproxy` е активната клиентска страна на една secure UDP връзка:

```text
един локален UDP вход -> един криптиран AISMixer SEC вход
```

Станциите се автентикират с ECDSA, а AISMixer разрешава публичните им ключове
чрез `authorized_keys.yaml`. AIS данните и ping/pong съобщенията за liveness
използват автентикирано AES-GCM криптиране. Криптираните ping съобщения помагат
да се запази NAT, CGNAT или mobile-client mapping, а автентикираните криптирани
pong отговори доказват, че конфигурираният peer още държи сесията.

Ако сървърът е загубил сесията, може да изпрати неавтентикиран `NOSESSION` като
подсказка за повторно свързване. Клиентът се свързва отново и когато няма
автентикирани отговори за `peer_timeout`. `session_refresh_interval` по
подразбиране е `0`, което изключва планираното периодично обновяване. Промяна
на клиентския source IP или port изисква нов handshake; session migration не е
реализирана.

```bash
cd nmea_sproxy && python3 nmea_sproxy.py
sudo systemctl start nmea_sproxy
sudo systemctl start nmea_sproxy@boat
sudo systemctl start nmea_sproxy@yacht
```

Template имена като `boat` и `yacht` са избрани от оператора етикети, а не
фиксирани имена `station1` / `station2`. Възстановяването на сесии подобрява
дългата работа, но UDP packet loss остава възможен и доставката на всяко AIS
изречение не е гарантирана. Подробното ръководство за оператори е в
[`nmea_sproxy/README.md`](nmea_sproxy/README.md).

---

## ✅ Тестове

Focused pytest coverage живее в `tests/` и покрива multipart assembly, TAG
`s`/`c`/`g` helper-и, metadata writing, `nmea_sproxy` extraction, secure UDP
helper-и и forwarding-loop behavior.

```bash
python -m pytest
```

---
