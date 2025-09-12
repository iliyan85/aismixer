# AISMixer

**(BG | EN below)**

---

## 🛰️ Какво е AISMixer?

`AISMixer` е Python услуга, която агрегира и миксира AIS NMEA съобщения от множество независими приемници, и ги препраща като единен поток към външни платформи като [MarineTraffic](https://www.marinetraffic.com), [AISHub](https://www.aishub.net), [VesselTracker](https://www.vesseltracker.com) и други.

🔐 Поддържа както обикновен (нешифриран) вход по UDP, така и защитени криптирани входове (NMEA over ECDSA + AES-GCM).

---

## 🧭 Основна идея

- Няколко AIS приемника (физически или софтуерни) подават NMEA съобщения към AISMixer.
- Потокът може да идва от различни IP адреси, през различни портове, вкл. през защитено криптирано прокси (nmea_sproxy).
- AISMixer премахва повтарящите се съобщения, добавя мета-информация и ги обединява.
- Външните платформи получават финален „логически“ поток, все едно идва от една станция.

---

## 📦 Компоненти

| Компонент | Роля |
|----------|------|
| `aismixer.py` | Основен процес, слушащ UDP и secure входове |
| `aismixer_secure.py` | Криптиран handshake, дешифриране и проверка на съобщенията |
| `nmea_sproxy/` | Лек клиент-прокси за шифроване и изпращане на трафик към миксера |
| `meta_writer.py` | Добавя NMEA префикси и `CRC` |
| `dedup.py` | Премахва дублиращи се съобщения |
| `config.yaml` | Конфигурация на входове, изходи, debug и име на станцията |
| `authorized_keys.yaml` | Публични ключове на разрешените клиенти (ECDSA) |

---

## 🔀 Схема на потоците

```
   +------------+     UDP     +-----------+           +----------------+
   | Receiver A | ----------> |           |           |                |
   +------------+             |           |           |                |
                              |           | --------> | MarineTraffic  |
   +------------+  Encrypted  | AISMixer  | --------> | AISHub         |
   | nmea_sproxy| ----------> |           | --------> | VesselTracker  |
   +------------+             |           |           | etc.           |
                              +-----------+           +----------------+
```

---

## ⚙️ Конфигурация (`config.yaml`)

```yaml
station_id: mixstation_1   # ако е непразно, винаги става s=
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
  - host: 5.9.207.224
    port: 5000

udp_alias_map_file: udp_alias_map.yaml   # по избор
```
@@
## 🔎 Как се формира `s` (NMEA TAG)
Приоритет:
1) ако `station_id` е непразно → **s = station_id**;
2) иначе ако има `input.id` (вкл. `sec_inputs[].id`) → **s = input.id**;
3) иначе:
   - UDP: ако IP е в `udp_alias_map.yaml` → **s = alias**;
   - SEC: ако има име от `authorized_keys.yaml` → **s = client_name** (иначе `ANONYMOUS`);
4) иначе, ако входящият ред носи `\s:…\` → **s = тази стойност**;
5) иначе → **s = IP** (точки/двуеточия → `_`).

Всички варианти минават през sanitize **[A–Za–z0–9_]** и твърд лимит **15**.

### (по избор) IP→alias мапинг за UDP (`udp_alias_map.yaml`)
```yaml
"127.0.0.1": "lo_alias"
"2001:db8::1234": "dock_gate"
```
@@

---

## 🚀 Стартиране

```bash
python3 aismixer.py
```

### За systemd:

- Използвайте `install.sh` за инсталация
- Файлът `aismixer.service` може да се копира в `/etc/systemd/system/`

---

## 🌍 English version

---

## 🛰️ What is AISMixer?

`AISMixer` is a Python-based service that aggregates and merges AIS NMEA messages from multiple independent receivers and forwards the resulting stream to public marine tracking platforms such as [MarineTraffic](https://www.marinetraffic.com), [AISHub](https://www.aishub.net), and others.

🔐 It supports both unencrypted UDP input and encrypted input via ECDSA handshake + AES-GCM encryption (via `nmea_sproxy`).

---

## 🧭 Core Idea

- Multiple AIS receivers (hardware or software) send NMEA messages to AISMixer
- Sources can be UDP or encrypted (via `nmea_sproxy`)
- AISMixer deduplicates, optionally adds metadata, and forwards as a single logical feed
- Marine tracking services receive the combined stream from "one virtual station"

---

## 📦 Components

| Component | Description |
|----------|-------------|
| `aismixer.py` | Main mixer process |
| `aismixer_secure.py` | Handles secure connections and decryption |
| `nmea_sproxy/` | Lightweight secure UDP client/proxy |
| `meta_writer.py` | Adds NMEA prefix and CRC |
| `dedup.py` | Deduplication engine |
| `config.yaml` | Config for listeners, forwarders, debug |
| `authorized_keys.yaml` | List of authorized client public keys |

---

## 🔀 Stream Architecture

```
   +------------+     UDP     +-----------+           +----------------+
   | Receiver A | ----------> |           |           |                |
   +------------+             |           |           |                |
                              |           | --------> | MarineTraffic  |
   +------------+  Encrypted  | AISMixer  | --------> | AISHub         |
   | nmea_sproxy| ----------> |           | --------> | VesselTracker  |
   +------------+             |           |           | etc.           |
                              +-----------+           +----------------+
```

---
## ⚙️ Configuration (`config.yaml`)

```yaml
station_id: mixstation_1   # if non-empty, it always becomes s=
debug: true

sec_inputs:
  - id: secA               # optional; if station_id is empty, s=secA
    listen_ip: "::"
    listen_port: 29999

udp_inputs:
  - listen_ip: "0.0.0.0"
    listen_port: 17777
    id: udpA               # optional; if station_id is empty, s=udpA
  - listen_ip: "::"
    listen_port: 17777

forwarders:
  - host: 5.9.207.224
    port: 5000

udp_alias_map_file: udp_alias_map.yaml   # optional
```
@@
## 🔎 How `s` (NMEA TAG) is formed

**Priority:**
1. If `station_id` is non-empty → **s = station_id**
2. Else, if the input has an `id` (incl. `sec_inputs[].id`) → **s = input.id**
3. Else:
   - **UDP:** if the remote IP exists in `udp_alias_map.yaml` → **s = alias**
   - **SEC:** if there is a name from `authorized_keys.yaml` → **s = client_name** (otherwise `ANONYMOUS`)
4. Else, if the incoming line already carries `\s:…\` → **s = that value**
5. Else → **s = IP** (dots/colons replaced with `_`)

_All variants are sanitized to `[A–Z a–z 0–9 _]` and hard-limited to **15** characters._

### (optional) IP→alias mapping for UDP (`udp_alias_map.yaml`)
```yaml
"127.0.0.1": "lo_alias"
"2001:db8::1234": "dock_gate"
```
@@

---

## 🚀 Running

```bash
python3 aismixer.py
```

Or install as a systemd service using `install.sh`

---

📝 Licensed by Iliyan Iliev (c) 2025 
Contributions welcome.

---

## 🛡️ Лиценз

Проектът е публикуван под лиценза **CC BY-NC 4.0**  
[Прочетете условията тук](https://creativecommons.org/licenses/by-nc/4.0/)  
За комерсиално използване, моля свържете се с автора.