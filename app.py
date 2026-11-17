"""
Backend da Estação Meteorológica Inteligente (PD-II)

Fluxo dos dados:
  ESP32 --(Wi-Fi/MQTT)--> broker --> [este programa] --> SQLite --> API REST --> Dashboard

Como rodar:
  pip install -r requirements.txt
  python app.py
"""
import json
import sqlite3
import statistics
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from flask import Flask, jsonify, request
from flask_cors import CORS

# ------------------------------------------------------------
# CONFIGURAÇÕES (devem ser iguais às do firmware)
# ------------------------------------------------------------
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
TOPICO_DADOS = "pd2grupo-unip-4829/estacao/+/dados"      # '+' aceita qualquer estação
TOPICO_STATUS = "pd2grupo-unip-4829/estacao/+/status"
DB_ARQUIVO = "estacao.db"

# Limites físicos para validação (lógica matemática)
LIMITES = {
    "temperatura": (-40.0, 80.0),
    "umidade": (0.0, 100.0),
    "temperatura_bmp": (-40.0, 80.0),
    "pressao": (300.0, 1100.0),
    "gas_pct": (0.0, 100.0),
    "luz_pct": (0.0, 100.0),
    "chuva_mm": (0.0, 10000.0),
}

app = Flask(__name__)
CORS(app)  # permite que o dashboard (outra origem) consuma a API


# ------------------------------------------------------------
# BANCO DE DADOS (SQLite)
# ------------------------------------------------------------
def conectar():
    con = sqlite3.connect(DB_ARQUIVO)
    con.row_factory = sqlite3.Row
    return con


def criar_banco():
    with conectar() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS estacoes (
                id          TEXT PRIMARY KEY,
                latitude    REAL,
                longitude   REAL,
                status      TEXT DEFAULT 'desconhecido',
                atualizada  TEXT
            );

            CREATE TABLE IF NOT EXISTS leituras (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                estacao_id       TEXT NOT NULL,
                data_hora        TEXT NOT NULL,
                temperatura      REAL,
                umidade          REAL,
                temperatura_bmp  REAL,
                pressao          REAL,
                gas_adc          INTEGER,
                gas_pct          REAL,
                luz_adc          INTEGER,
                luz_pct          REAL,
                chuva_pulsos     INTEGER,
                chuva_mm         REAL,
                FOREIGN KEY (estacao_id) REFERENCES estacoes(id)
            );

            CREATE INDEX IF NOT EXISTS idx_leituras_estacao_data
                ON leituras (estacao_id, data_hora);
            """
        )


def agora_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------
# VALIDAÇÃO E TRATAMENTO DE DADOS INCONSISTENTES
# ------------------------------------------------------------
def validar(dados: dict) -> dict:
    """Mantém só valores numéricos dentro da faixa física; o resto vira None."""
    limpo = {}
    for campo, (minimo, maximo) in LIMITES.items():
        valor = dados.get(campo)
        if isinstance(valor, (int, float)) and minimo <= valor <= maximo:
            limpo[campo] = float(valor)
        else:
            limpo[campo] = None
    # campos inteiros brutos
    for campo in ("gas_adc", "luz_adc", "chuva_pulsos"):
        valor = dados.get(campo)
        limpo[campo] = int(valor) if isinstance(valor, (int, float)) and valor >= 0 else None
    return limpo


def salvar_leitura(dados: dict):
    estacao = str(dados.get("estacao", "desconhecida"))[:50]
    lat, lon = dados.get("lat"), dados.get("lon")
    limpo = validar(dados)

    # descarta a leitura inteira se nenhum sensor estiver válido
    if all(v is None for v in limpo.values()):
        print(f"[BACKEND] Leitura descartada (todos os campos inválidos): {dados}")
        return

    with conectar() as con:
        con.execute(
            """INSERT INTO estacoes (id, latitude, longitude, atualizada)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 latitude = excluded.latitude,
                 longitude = excluded.longitude,
                 atualizada = excluded.atualizada""",
            (estacao, lat, lon, agora_iso()),
        )
        con.execute(
            """INSERT INTO leituras
               (estacao_id, data_hora, temperatura, umidade, temperatura_bmp,
                pressao, gas_adc, gas_pct, luz_adc, luz_pct, chuva_pulsos, chuva_mm)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                estacao, agora_iso(),
                limpo["temperatura"], limpo["umidade"], limpo["temperatura_bmp"],
                limpo["pressao"], limpo["gas_adc"], limpo["gas_pct"],
                limpo["luz_adc"], limpo["luz_pct"],
                limpo["chuva_pulsos"], limpo["chuva_mm"],
            ),
        )
    print(f"[BACKEND] Leitura salva ({estacao}): T={limpo['temperatura']} "
          f"U={limpo['umidade']} P={limpo['pressao']}")


def atualizar_status(estacao: str, status: str):
    with conectar() as con:
        con.execute(
            """INSERT INTO estacoes (id, status, atualizada) VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status = excluded.status,
                                             atualizada = excluded.atualizada""",
            (estacao, status, agora_iso()),
        )


# ------------------------------------------------------------
# MQTT (roda em thread separada)
# ------------------------------------------------------------
def ao_conectar(client, userdata, flags, reason_code, properties=None):
    print(f"[MQTT] Conectado ao broker (código: {reason_code})")
    client.subscribe(TOPICO_DADOS)
    client.subscribe(TOPICO_STATUS)


def ao_receber(client, userdata, msg):
    try:
        partes = msg.topic.split("/")          # pd2grupo/estacao/<id>/<tipo>
        estacao_id, tipo = partes[2], partes[3]
        texto = msg.payload.decode("utf-8")

        if tipo == "status":
            atualizar_status(estacao_id, texto)
        elif tipo == "dados":
            salvar_leitura(json.loads(texto))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as erro:
        print(f"[MQTT] Mensagem ignorada ({erro}): {msg.payload[:80]}")


def iniciar_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = ao_conectar
    client.on_message = ao_receber
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_forever()   # reconecta sozinho


# ------------------------------------------------------------
# API REST
# ------------------------------------------------------------
@app.get("/api/estacoes")
def listar_estacoes():
    with conectar() as con:
        linhas = con.execute("SELECT * FROM estacoes").fetchall()
    return jsonify([dict(l) for l in linhas])


@app.get("/api/leituras/ultima")
def ultima_leitura():
    estacao = request.args.get("estacao", "estacao01")
    with conectar() as con:
        linha = con.execute(
            "SELECT * FROM leituras WHERE estacao_id = ? ORDER BY id DESC LIMIT 1",
            (estacao,),
        ).fetchone()
    if linha is None:
        return jsonify({"erro": "sem dados"}), 404
    return jsonify(dict(linha))


@app.get("/api/leituras")
def historico():
    estacao = request.args.get("estacao", "estacao01")
    limite = min(int(request.args.get("limite", 100)), 1000)
    with conectar() as con:
        linhas = con.execute(
            "SELECT * FROM leituras WHERE estacao_id = ? ORDER BY id DESC LIMIT ?",
            (estacao, limite),
        ).fetchall()
    return jsonify([dict(l) for l in reversed(linhas)])


@app.get("/api/estatisticas")
def estatisticas():
    """Média, mínimo, máximo e desvio padrão das últimas N leituras."""
    estacao = request.args.get("estacao", "estacao01")
    limite = min(int(request.args.get("limite", 100)), 1000)
    campos = ["temperatura", "umidade", "pressao", "gas_pct", "luz_pct"]

    with conectar() as con:
        linhas = con.execute(
            "SELECT * FROM leituras WHERE estacao_id = ? ORDER BY id DESC LIMIT ?",
            (estacao, limite),
        ).fetchall()

    resultado = {}
    for campo in campos:
        valores = [l[campo] for l in linhas if l[campo] is not None]
        if valores:
            resultado[campo] = {
                "media": round(statistics.mean(valores), 2),
                "minimo": min(valores),
                "maximo": max(valores),
                "desvio_padrao": round(statistics.pstdev(valores), 2),
                "amostras": len(valores),
            }
    return jsonify(resultado)


@app.get("/api/alertas")
def alertas():
    """Alertas simples baseados na última leitura (regras lógicas)."""
    estacao = request.args.get("estacao", "estacao01")
    with conectar() as con:
        l = con.execute(
            "SELECT * FROM leituras WHERE estacao_id = ? ORDER BY id DESC LIMIT 1",
            (estacao,),
        ).fetchone()
    if l is None:
        return jsonify([])

    lista = []
    if l["temperatura"] is not None and l["temperatura"] >= 35:
        lista.append({"tipo": "calor", "msg": "Temperatura elevada (≥ 35 °C)"})
    if l["umidade"] is not None and l["umidade"] <= 30:
        lista.append({"tipo": "ar_seco", "msg": "Ar muito seco (≤ 30 %)"})
    if l["gas_pct"] is not None and l["gas_pct"] >= 60:
        lista.append({"tipo": "qualidade_ar", "msg": "Qualidade do ar ruim"})
    if l["chuva_mm"] is not None and l["chuva_mm"] > 0:
        lista.append({"tipo": "chuva", "msg": f"Chuva acumulada: {l['chuva_mm']:.2f} mm"})
    return jsonify(lista)


@app.get("/")
def raiz():
    return jsonify({"servico": "API Estação Meteorológica", "status": "ok"})


# ------------------------------------------------------------
if __name__ == "__main__":
    criar_banco()
    threading.Thread(target=iniciar_mqtt, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
