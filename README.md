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
| `nmea_sproxy/`          | Lightweight client-side secure UDP proxy |
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

## 🔐 Security

- **Handshake:** ECDSA-based station authentication; clients are authorized via `authorized_keys.yaml`.
- **Context:** secure UDP helpers include the `NMEA-AUTH-v1` context and transcript-building helpers for the hardened handshake path; the current compatible handshake signs station identity plus timestamp.
- **Transport:** AES-GCM for integrity + confidentiality over UDP data packets.
- **Replay/session hardening:** handshakes are timestamp-window checked, handshake replay keys and data nonces are tracked with bounded TTL caches, sessions expire, and keepalives refresh active sessions.

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
| `nmea_sproxy/`         | Леко клиентско secure UDP прокси |
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

## 🔐 Сигурност

- **Handshake:** ECDSA station authentication; клиентите се описват в `authorized_keys.yaml`.
- **Context:** secure UDP helper-ите включват `NMEA-AUTH-v1` context и transcript-building helpers за hardened handshake path; текущият compatible handshake подписва station identity плюс timestamp.
- **Транспорт:** AES‑GCM за целост и конфиденциалност на UDP data packets.
- **Replay/session hardening:** handshakes се проверяват с timestamp window, handshake replay keys и data nonces се пазят в bounded TTL caches, sessions expire-ват, а keepalive packets обновяват активни sessions.

---

## ✅ Тестове

Focused pytest coverage живее в `tests/` и покрива multipart assembly, TAG
`s`/`c`/`g` helper-и, metadata writing, `nmea_sproxy` extraction, secure UDP
helper-и и forwarding-loop behavior.

```bash
python -m pytest
```

---
