import os
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import base64

# 1. Генериране на ключова двойка
private_key = ec.generate_private_key(ec.SECP256R1())

# 2. Запис на частния ключ
with open(os.path.join('nmea_sproxy', 'station_private.key'), 'wb') as f:
    f.write(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    ))

# 3. Извличане и запис на публичния ключ (PEM формат)
public_key = private_key.public_key()
with open(os.path.join('nmea_sproxy', 'station_public.pem'), 'wb') as f:
    f.write(public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ))

# 4. Извличане на публичния ключ в компресиран X962 формат
compressed = public_key.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.CompressedPoint
)

print("📦 Base64-компресиран публичен ключ:")
print(base64.b64encode(compressed).decode())
