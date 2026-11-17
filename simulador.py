"""
Simulador: publica leituras falsas no mesmo tópico do ESP32.
Use para testar o backend/dashboard sem precisar do Wokwi aberto.

  python simulador.py
"""
import json
import random
import time

import paho.mqtt.client as mqtt

BROKER = "broker.hivemq.com"
TOPICO = "pd2grupo/estacao/estacao01/dados"

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect(BROKER, 1883, 60)
client.loop_start()

chuva_pulsos = 0
while True:
    if random.random() < 0.2:
        chuva_pulsos += 1

    gas_adc = random.randint(300, 2500)
    luz_adc = random.randint(200, 4000)

    dados = {
        "estacao": "estacao01",
        "lat": -23.5505,
        "lon": -46.6333,
        "temperatura": round(random.uniform(18, 34), 1),
        "umidade": round(random.uniform(40, 90), 1),
        "temperatura_bmp": round(random.uniform(18, 34), 1),
        "pressao": round(random.uniform(1005, 1025), 1),
        "gas_adc": gas_adc,
        "gas_pct": round(gas_adc / 4095 * 100, 1),
        "luz_adc": luz_adc,
        "luz_pct": round(luz_adc / 4095 * 100, 1),
        "chuva_pulsos": chuva_pulsos,
        "chuva_mm": round(chuva_pulsos * 0.2794, 2),
    }
    client.publish(TOPICO, json.dumps(dados))
    print("Enviado:", dados)
    time.sleep(5)
flask>=3.0
flask-cors>=4.0
paho-mqtt>=2.0
