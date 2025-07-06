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
station_name: mixstation_1
debug: true

udp_inputs:
  - listen_ip: "0.0.0.0"
    listen_port: 17777
  - listen_ip: "::"
    listen_port: 17777

secure_inputs:
  - listen_ip: "::"
    listen_port: 29999

forwarders:
  - ["5.9.207.224", 5000]  # примерен сървър
```

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