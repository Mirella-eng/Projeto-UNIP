#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include "DHT.h"
#include <Adafruit_Sensor.h>
#include <Adafruit_BMP085.h>

// ======================================
// CONFIGURACOES DE REDE E MQTT
// ======================================
// No Wokwi a rede Wi-Fi simulada se chama "Wokwi-GUEST" (sem senha, canal 6)
const char* WIFI_SSID = "Wokwi-GUEST";
const char* WIFI_PASS = "";

// Broker publico e gratuito (nao precisa de cadastro)
const char* MQTT_BROKER = "broker.hivemq.com";
const int   MQTT_PORT   = 1883;

// IMPORTANTE: troque "pd2grupo" por um nome unico do seu grupo
// (o broker e publico, qualquer um pode ler/escrever no mesmo topico)
const char* ID_ESTACAO  = "estacao01";
const char* TOPICO_DADOS  = "pd2grupo/estacao/estacao01/dados";
const char* TOPICO_STATUS = "pd2grupo/estacao/estacao01/status";

// Localizacao da estacao (usada pelo dashboard/mapa - geometria analitica)
const float LATITUDE  = -23.5505;
const float LONGITUDE = -46.6333;

// ======================================
// PINOS DOS COMPONENTES
// ======================================
#define DHTPIN       4
#define DHTTYPE      DHT22

#define PIN_GAS      34
#define PIN_LDR      35
#define PIN_CHUVA    27

#define PIN_SDA      21
#define PIN_SCL      22

// ======================================
// SENSORES E CLIENTES
// ======================================
DHT dht(DHTPIN, DHTTYPE);
Adafruit_BMP085 bmp;
bool bmpDisponivel = false;

WiFiClient espClient;
PubSubClient mqtt(espClient);

// ======================================
// PLUVIOMETRO (botao simulando a bascula)
// ======================================
int pulsosChuva = 0;
int ultimaLeituraBotao = HIGH;
int estadoEstavelBotao = HIGH;
unsigned long tempoDebounce = 0;
const unsigned long TEMPO_DEBOUNCE = 50;

// Cada pulso (basculada) equivale a ~0,2794 mm de chuva (padrao de pluviometros)
const float MM_POR_PULSO = 0.2794;

// ======================================
// CONTROLE DE TEMPO
// ======================================
unsigned long ultimaLeituraSensores = 0;
const unsigned long INTERVALO_LEITURA = 5000;  // 5 s (publica a cada leitura)

unsigned long ultimaTentativaMqtt = 0;
const unsigned long INTERVALO_RECONEXAO = 5000;

// ======================================
// LIMITES FISICOS PARA VALIDACAO
// ======================================
const float TEMP_MIN = -40.0, TEMP_MAX = 80.0;   // faixa do DHT22
const float UMID_MIN = 0.0,   UMID_MAX = 100.0;
const float PRES_MIN = 300.0, PRES_MAX = 1100.0; // faixa do BMP180 (hPa)

bool dentroDaFaixa(float v, float minimo, float maximo) {
  return !isnan(v) && v >= minimo && v <= maximo;
}

// ======================================
// WI-FI E MQTT
// ======================================
void conectarWiFi() {
  Serial.print("Conectando ao Wi-Fi");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);

  int tentativas = 0;
  while (WiFi.status() != WL_CONNECTED && tentativas < 40) {
    delay(250);
    Serial.print(".");
    tentativas++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(" conectado!");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println(" FALHA (tentara novamente no loop)");
  }
}

void reconectarMqtt() {
  if (WiFi.status() != WL_CONNECTED) {
    conectarWiFi();
    return;
  }

  if (millis() - ultimaTentativaMqtt < INTERVALO_RECONEXAO) return;
  ultimaTentativaMqtt = millis();

  String clientId = String("esp32-") + ID_ESTACAO + "-" + String(random(0xffff), HEX);
  Serial.print("Conectando ao broker MQTT... ");

  // Last Will: se a estacao cair, o broker publica "offline" automaticamente
  if (mqtt.connect(clientId.c_str(), NULL, NULL, TOPICO_STATUS, 1, true, "offline")) {
    Serial.println("OK");
    mqtt.publish(TOPICO_STATUS, "online", true);
  } else {
    Serial.print("falhou, rc=");
    Serial.println(mqtt.state());
  }
}

// ======================================
// SETUP
// ======================================
void setup() {
  Serial.begin(115200);
  analogReadResolution(12);

  dht.begin();
  Wire.begin(PIN_SDA, PIN_SCL);

  bmpDisponivel = bmp.begin();
  Serial.println(bmpDisponivel ? "BMP180 conectado com sucesso!"
                               : "ERRO: BMP180 nao encontrado!");

  pinMode(PIN_CHUVA, INPUT_PULLUP);

  Serial.println();
  Serial.println("================================");
  Serial.println(" ESTACAO METEOROLOGICA ESP32");
  Serial.println("================================");

  conectarWiFi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(512);

  Serial.println("Inicializacao concluida!");
}

// ======================================
// PLUVIOMETRO (debounce do colega, mantido)
// ======================================
void lerPluviometro() {
  int leituraAtual = digitalRead(PIN_CHUVA);

  if (leituraAtual != ultimaLeituraBotao) {
    tempoDebounce = millis();
    ultimaLeituraBotao = leituraAtual;
  }

  if (millis() - tempoDebounce > TEMPO_DEBOUNCE) {
    if (leituraAtual != estadoEstavelBotao) {
      estadoEstavelBotao = leituraAtual;
      if (estadoEstavelBotao == LOW) {
        pulsosChuva++;
      }
    }
  }
}

// ======================================
// LEITURA, VALIDACAO E PUBLICACAO
// ======================================
void lerEPublicar() {
  float umidade     = dht.readHumidity();
  float temperatura = dht.readTemperature();

  float temperaturaBMP = NAN;
  float pressao = NAN;
  if (bmpDisponivel) {
    temperaturaBMP = bmp.readTemperature();
    pressao = bmp.readPressure() / 100.0;
  }

  int valorGas = analogRead(PIN_GAS);
  int valorLDR = analogRead(PIN_LDR);

  // Conversoes simples
  float luminosidadePct = (valorLDR / 4095.0) * 100.0;
  float gasPct          = (valorGas / 4095.0) * 100.0;
  float chuvaMm         = pulsosChuva * MM_POR_PULSO;

  // Validacao (logica): cada leitura so e valida se estiver na faixa fisica
  bool dhtOk = dentroDaFaixa(temperatura, TEMP_MIN, TEMP_MAX) &&
               dentroDaFaixa(umidade, UMID_MIN, UMID_MAX);
  bool bmpOk = bmpDisponivel &&
               dentroDaFaixa(pressao, PRES_MIN, PRES_MAX) &&
               dentroDaFaixa(temperaturaBMP, TEMP_MIN, TEMP_MAX);

  // Monta o JSON manualmente (campos invalidos viram null)
  char tTemp[16], tUmid[16], tPres[16], tTbmp[16];
  if (dhtOk) {
    snprintf(tTemp, sizeof(tTemp), "%.1f", temperatura);
    snprintf(tUmid, sizeof(tUmid), "%.1f", umidade);
  } else {
    strcpy(tTemp, "null");
    strcpy(tUmid, "null");
  }
  if (bmpOk) {
    snprintf(tPres, sizeof(tPres), "%.1f", pressao);
    snprintf(tTbmp, sizeof(tTbmp), "%.1f", temperaturaBMP);
  } else {
    strcpy(tPres, "null");
    strcpy(tTbmp, "null");
  }

  char payload[400];
  snprintf(payload, sizeof(payload),
    "{\"estacao\":\"%s\",\"lat\":%.4f,\"lon\":%.4f,"
    "\"temperatura\":%s,\"umidade\":%s,"
    "\"temperatura_bmp\":%s,\"pressao\":%s,"
    "\"gas_adc\":%d,\"gas_pct\":%.1f,"
    "\"luz_adc\":%d,\"luz_pct\":%.1f,"
    "\"chuva_pulsos\":%d,\"chuva_mm\":%.2f}",
    ID_ESTACAO, LATITUDE, LONGITUDE,
    tTemp, tUmid, tTbmp, tPres,
    valorGas, gasPct, valorLDR, luminosidadePct,
    pulsosChuva, chuvaMm);

  Serial.println();
  Serial.println("========== DADOS AMBIENTAIS ==========");
  Serial.println(payload);

  if (mqtt.connected()) {
    bool ok = mqtt.publish(TOPICO_DADOS, payload);
    Serial.println(ok ? ">> Publicado no MQTT" : ">> ERRO ao publicar");
  } else {
    Serial.println(">> MQTT desconectado, dado nao enviado");
  }
  Serial.println("======================================");
}

// ======================================
// LOOP PRINCIPAL
// ======================================
void loop() {
  if (!mqtt.connected()) {
    reconectarMqtt();
  } else {
    mqtt.loop();
  }

  lerPluviometro();

  if (millis() - ultimaLeituraSensores >= INTERVALO_LEITURA) {
    ultimaLeituraSensores = millis();
    lerEPublicar();
  }
}
