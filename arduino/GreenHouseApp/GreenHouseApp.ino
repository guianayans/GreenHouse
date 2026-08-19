#include <LiquidCrystal_I2C.h>
#include <DHT.h>
#include <math.h> // Incluído para garantir exp()
#include <avr/wdt.h> // Para o reset via watchdog (comando "REINICIAR")
#include <EEPROM.h>

// --------------------------------------------------
// 1) Configurações e variáveis globais
// --------------------------------------------------
#define DHTPIN 2
#define DHTTYPE DHT22

// 4 "slots" físicos fixos: cada slot é um par sensor+bomba que anda sempre
// junto (slot 0 = sensor A0 + relé pino 8; slot 1 = A1+9; etc). A
// TOPOLOGIA (quantos vasos existem e a quais vasos cada slot pertence) é
// configurável em runtime via comando serial "TOPO:" — não mexe nesses
// arrays de pinos físicos, só na atribuição slot→vaso abaixo.
#define NUM_SLOTS 4

const int soilMoisturePins[NUM_SLOTS] = {A0, A1, A2, A3};
const int relayPins[NUM_SLOTS] = {8, 9, 10, 11};

// Calibração por slot
const int valor_seco[NUM_SLOTS]  = {553, 557, 551, 560};
const int valor_umido[NUM_SLOTS] = {243, 240, 241, 238};

// Detecção de sensor desconectado/instável: um pino analógico flutuando
// (sem sensor nenhum ligado) capta ruído elétrico e varia MUITO entre
// leituras tiradas com poucos ms de diferença (um sensor de verdade não
// muda de umidade nesse intervalo). Se a diferença entre a maior e a
// menor das 7 amostras passar de LIMITE_SPREAD_INSTAVEL, essa rodada conta
// como "instável"; depois de LIMIAR_DEBOUNCE rodadas seguidas instáveis, o
// slot é considerado desconectado (evita marcar/desmarcar à toa por causa
// de um pico isolado de ruído).
const int LIMITE_SPREAD_INSTAVEL = 150;
const int LIMIAR_DEBOUNCE = 3;

// Período de carência logo após ligar/reiniciar: o circuito do sensor
// (capacitor/ADC) ainda pode estar assentando eletricamente, o que faria a
// detecção de instabilidade acima demorar mais pra "confiar" nesse sensor
// especificamente (parecendo um delay estranho só nele, comparado aos
// outros que já assentaram mais rápido). Ignorar a checagem por alguns
// segundos no início faz todo mundo aparecer junto, de imediato.
const unsigned long PERIODO_CARENCIA_SENSOR_MS = 8000;

int instabilidadeContagem[NUM_SLOTS] = {0, 0, 0, 0};
bool sensorDesconectado[NUM_SLOTS]   = {false, false, false, false};

// Suavização (média móvel exponencial) por slot, pra não deixar o valor
// exibido pular de leitura em leitura mesmo com o sensor bem conectado
// (ruído de rede elétrica, chaveamento do relé da bomba etc.) — só é
// atualizada quando o slot NÃO está desconectado.
float leituraSuavizada[NUM_SLOTS] = {-1, -1, -1, -1}; // -1 = ainda sem leitura válida

// Topologia: a qual vaso (0-indexado) cada slot pertence. Sensor e bomba
// de um mesmo slot físico têm atribuições INDEPENDENTES (modo avançado
// permite, por exemplo, um sensor no vaso 1 mas a bomba daquele slot no
// vaso 2, ou um vaso monitorado sem bomba nenhuma) — no modo simples
// (o padrão), o app Python simplesmente manda o mesmo vaso pros dois.
// Default de fábrica antes do primeiro "TOPO:" chegar: 2 vasos de 2 slots
// cada (replica o comportamento original do app, com 2 vasos fixos).
int sensorParaVaso[NUM_SLOTS] = {0, 0, 1, 1};
int bombaParaVaso[NUM_SLOTS]  = {0, 0, 1, 1};
int numVasos = 2;

// Limites de umidade — configuráveis em runtime via comando serial "CFG:"
// enviado pelo app Python (fonte da verdade fica no banco de dados do
// app; estes valores aqui são só o default até a primeira config chegar).
int moistureThresholdLow = 45;
int moistureInterruptThreshold = 65;

// Liga/desliga a rega automática (checagem por tempo/umidade no loop()) —
// controlado pelo toggle "Rega automática" em Ajustes. Quando false, só a
// rega manual (botões/REGAR:) funciona; o Arduino nunca rega sozinho.
bool regaAutomaticaAtiva = true;

// Intervalos de tempo
unsigned long ultimaRegaMillis  = 0;
unsigned long intervaloRega     = 7200000UL;  // ms, default 2h
unsigned long ultimoDisplayMillis = 0;
unsigned long intervaloDisplay    = 10000;  // 10s

// Vazão / Volume — o tempo necessário de rega é calculado por vaso (ver
// calcularTempoNecessario), porque o número de bombas simultâneas agora
// varia conforme quantos slots pertencem a cada vaso.
float vazao_ml_por_segundo = 15.63;
float volume_necessario    = 1000;

// Modo de cultivo (Vegetativo/Floração) — só um rótulo + preset que o
// Python decide; aqui é usado só para exibir no LCD e devolver via GET_CFG.
String modoAtual = "VEGA";

// --------------------------------------------------
// Persistência em EEPROM — config e topologia mudam raramente, então em
// vez de depender só do Python reenviar "CFG:"/"TOPO:" bem no momento
// certo depois de cada boot (janela apertada, sujeita a corrida com as
// telas iniciais do LCD), o Arduino grava tudo na EEPROM assim que recebe
// um comando válido, e lê de lá logo no início do setup() — fica correto
// desde o primeiro instante, mesmo se o Python demorar ou nunca conseguir
// reenviar (ex: container do app fora do ar).
// --------------------------------------------------
// 0xA6 (mudou de 0xA5): o layout da struct mudou (slotParaVaso único virou
// sensorParaVaso+bombaParaVaso independentes) — trocar o magic garante que
// uma EEPROM gravada pela versão antiga do firmware seja tratada como
// "virgem" em vez de lida com o layout errado (dados corrompidos/lixo).
#define EEPROM_MAGIC 0xA6 // marca "já tem config salva" — distingue de EEPROM virgem (0xFF de fábrica)

struct ConfigPersistida {
  uint8_t magic;
  int low;
  int high;
  int intervaloMin;
  float volume;
  uint8_t modoFlora; // 0 = VEGA, 1 = FLORA
  int numVasos;
  int8_t sensorParaVaso[NUM_SLOTS]; // -1 = sem vaso (sensor desativado)
  int8_t bombaParaVaso[NUM_SLOTS];  // -1 = sem vaso (bomba desativada)
  uint8_t autoAtiva; // 0/1 — toggle "Rega automática"
};

void salvarConfigNaEEPROM() {
  ConfigPersistida c;
  c.magic = EEPROM_MAGIC;
  c.low = moistureThresholdLow;
  c.high = moistureInterruptThreshold;
  c.intervaloMin = (int)(intervaloRega / 60000UL);
  c.volume = volume_necessario;
  c.modoFlora = (modoAtual == "FLORA") ? 1 : 0;
  c.numVasos = numVasos;
  for (int i = 0; i < NUM_SLOTS; i++) {
    c.sensorParaVaso[i] = (int8_t)sensorParaVaso[i];
    c.bombaParaVaso[i] = (int8_t)bombaParaVaso[i];
  }
  c.autoAtiva = regaAutomaticaAtiva ? 1 : 0;
  EEPROM.put(0, c); // EEPROM.put só regrava bytes que realmente mudaram
}

void carregarConfigDaEEPROM() {
  ConfigPersistida c;
  EEPROM.get(0, c);
  if (c.magic != EEPROM_MAGIC) return; // EEPROM virgem (Arduino novo/nunca configurado) — mantém defaults hardcoded

  moistureThresholdLow = c.low;
  moistureInterruptThreshold = c.high;
  intervaloRega = (unsigned long)c.intervaloMin * 60000UL;
  volume_necessario = c.volume;
  modoAtual = c.modoFlora ? "FLORA" : "VEGA";
  numVasos = c.numVasos;
  for (int i = 0; i < NUM_SLOTS; i++) {
    sensorParaVaso[i] = c.sensorParaVaso[i];
    bombaParaVaso[i] = c.bombaParaVaso[i];
  }
  regaAutomaticaAtiva = (c.autoAtiva != 0);
}

// Tela atual do LCD, alternando a cada intervaloDisplay: 0 = temp/umidade/VPD,
// 1 = vasos 1-2, 2 = vasos 3-4 (só entra no ciclo quando numVasos > 2).
int telaAtual = 0;

// Leitura DHT a cada 2s
unsigned long lastDHTReadMillis = 0;
unsigned long dhtInterval       = 2000;
float lastTemp  = NAN;
float lastHumid = NAN;

// LCD e DHT
LiquidCrystal_I2C lcd(0x27, 20, 4);
DHT dht(DHTPIN, DHTTYPE);

// Contagem de regas
int contagemRegas = 0;

// --------------------------------------------------
// 1.1) Protocolo serial (comandos vindos do app Python)
// --------------------------------------------------
String bufferSerial = "";
bool regaEmAndamento = false;
bool pedidoParada = false;

// Teste manual de bombas (comando "TESTE_BOMBAS:LIGAR"/"TESTE_BOMBAS:DESLIGAR")
// — liga cada uma das 4 bombas por vez (10s cada, nunca duas juntas), sem
// passar pela lógica normal de rega (sem cálculo de volume/tempo, sem
// checar umidade). Só pra testar fiação/funcionamento. "DESLIGAR" interrompe
// o ciclo na hora (mesmo mecanismo do "PARAR" da rega normal).
bool testeBombasAtivo = false;

// Controle manual de UMA bomba por vez (comando "TESTE_BOMBA:LIGAR:<slot>"/
// "TESTE_BOMBA:DESLIGAR") — pra você escolher e ligar uma bomba específica
// na hora que quiser, sem esperar o ciclo automático. -1 = nenhuma ligada.
// Desliga sozinha depois de BOMBA_MANUAL_TIMEOUT_MS por segurança.
int bombaManualAtiva = -1;
unsigned long bombaManualInicioMillis = 0;
const unsigned long BOMBA_MANUAL_TIMEOUT_MS = 2UL * 60UL * 1000UL; // 2 min

// --------------------------------------------------
// 2) Funções de cálculo e utilitárias
// --------------------------------------------------

/**
 * @brief Calcula o VPD com base em temperatura e umidade.
 */
float calcularVPD(float temperatura, float umidade) {
  if (isnan(temperatura) || isnan(umidade)) return NAN;
  float es = 0.6108 * exp((17.27 * temperatura) / (temperatura + 237.3));
  float ea = (umidade / 100.0) * es;
  return es - ea;
}

/**
 * @brief Classifica o VPD em estágios de cultivo.
 */
String classificarVPD(float vpd) {
  if (isnan(vpd)) return "Indef";
  if (vpd < 0.7 || vpd >= 2.0) {
    return "Atenção";
  }
  else if (vpd < 1.0) {
    return "Vegetativo";
  }
  else if (vpd < 1.4) {
    return "Flora IN";
  }
  else {
    return "Flora OUT";
  }
}

/**
 * @brief Lê várias amostras de um sensor e retorna a mediana. Também
 *        informa (via "spread") a diferença entre a maior e a menor
 *        amostra, usada pra detectar pino flutuando/sensor desconectado.
 */
int lerMedianaSensor(int pin, int *spread) {
  const int N = 7;
  int leituras[N];

  for (int i = 0; i < N; i++) {
    leituras[i] = analogRead(pin);
    delay(5);
  }
  // Ordena para achar a mediana
  for (int i = 0; i < N - 1; i++) {
    for (int j = i + 1; j < N; j++) {
      if (leituras[j] < leituras[i]) {
        int temp = leituras[i];
        leituras[i] = leituras[j];
        leituras[j] = temp;
      }
    }
  }
  *spread = leituras[N - 1] - leituras[0];
  return leituras[N / 2];
}

/**
 * @brief Lê e suaviza a umidade (%) de um slot, com debounce de sensor
 *        desconectado/instável. Retorna false (e não atualiza a média
 *        móvel) se o slot estiver marcado como desconectado.
 */
bool lerUmidadeSlotSuavizada(int slot, int *umidadeSuavizada) {
  int spread;
  int valorAdc = lerMedianaSensor(soilMoisturePins[slot], &spread);

  // Logo após o boot, o circuito do sensor (capacitor/ADC) ainda pode estar
  // assentando eletricamente — ignora a checagem de instabilidade nesse
  // período de carência, pra todos os vasos aparecerem de imediato no
  // painel/LCD em vez de alguns demorarem mais que outros pra "confiar".
  // Depois desse período, a detecção de sensor desconectado passa a valer normalmente.
  bool emPeriodoDeCarencia = millis() < PERIODO_CARENCIA_SENSOR_MS;

  if (!emPeriodoDeCarencia) {
    if (spread > LIMITE_SPREAD_INSTAVEL) {
      if (instabilidadeContagem[slot] < LIMIAR_DEBOUNCE) instabilidadeContagem[slot]++;
    } else {
      if (instabilidadeContagem[slot] > 0) instabilidadeContagem[slot]--;
    }
    sensorDesconectado[slot] = (instabilidadeContagem[slot] >= LIMIAR_DEBOUNCE);
  }

  if (sensorDesconectado[slot]) return false;

  int umid = map(valorAdc, valor_seco[slot], valor_umido[slot], 0, 100);
  umid = constrain(umid, 0, 100);

  if (leituraSuavizada[slot] < 0) {
    leituraSuavizada[slot] = umid; // primeira leitura válida, sem histórico ainda
  } else {
    leituraSuavizada[slot] = leituraSuavizada[slot] * 0.7 + umid * 0.3;
  }

  *umidadeSuavizada = (int)(leituraSuavizada[slot] + 0.5);
  return true;
}

/**
 * @brief Calcula a umidade média de um vaso, a partir de quantos SENSORES
 *        (1 a 4) estiverem atribuídos a ele — independente de quantas
 *        bombas o vaso tem (modo avançado permite sensor sem bomba, ou
 *        vice-versa). Slots com sensor desconectado/instável não entram
 *        na média. Retorna -1 se o vaso não tiver NENHUM sensor confiável
 *        no momento (sem sensor atribuído, ou todos desconectados) —
 *        sentinela que os chamadores tratam como "sem sensor", nunca 0%.
 */
int calcularUmidadeMedia(int vasoIndex) {
  long soma = 0;
  int quantidade = 0;

  for (int slot = 0; slot < NUM_SLOTS; slot++) {
    if (sensorParaVaso[slot] != vasoIndex) continue;

    int umid;
    if (!lerUmidadeSlotSuavizada(slot, &umid)) continue; // desconectado, ignora

    soma += umid;
    quantidade++;
  }

  if (quantidade == 0) return -1;
  return soma / quantidade;
}

/**
 * @brief Tempo de rega necessário pra atingir o volume alvo, dado quantas
 *        bombas vão rodar simultaneamente (mais bombas = enche mais rápido).
 */
// Teto de segurança: nunca deixa uma rega (manual ou automática) ficar
// bloqueando o Arduino por mais que isso de tempo ATIVO de bomba, mesmo
// que o "Volume alvo" configurado seja muito alto pra vazão das bombas —
// protege contra ficar horas travado sem mandar nenhuma leitura se o
// volume for configurado errado, ou se a umidade nunca atingir o corte
// (ex: sensor instável durante a rega).
const unsigned long TEMPO_MAXIMO_REGA_MS = 5UL * 60UL * 1000UL; // 5 min de bomba ativa

unsigned long calcularTempoNecessario(int totalBombas) {
  if (totalBombas <= 0) totalBombas = 1;
  unsigned long tempo = (unsigned long)((volume_necessario / (vazao_ml_por_segundo * totalBombas)) * 1000);
  if (tempo > TEMPO_MAXIMO_REGA_MS) tempo = TEMPO_MAXIMO_REGA_MS;
  return tempo;
}

// --------------------------------------------------
// 3) Funções de exibição no LCD
// --------------------------------------------------

/**
 * @brief Tela de apresentação inicial.
 */
void telaApresentacao() {
  lcd.setCursor(0, 0);
  lcd.print("====================");
  lcd.setCursor(0, 1);
  lcd.print("======>GREEN>>======");
  lcd.setCursor(0, 2);
  lcd.print("======>HOUSE>>======");
  lcd.setCursor(0, 3);
  lcd.print("====================");
}

/**
 * @brief Monta a linha de cabeçalho "= GreenHouse [MODO] =", sempre com
 *        20 colunas, pra caber certinho no LCD.
 */
String linhaCabecalhoComModo() {
  String meio = "GreenHouse [" + modoAtual + "]";
  if (meio.length() > 18) meio = meio.substring(0, 18);

  // Espaço fixo de 1 coluna de cada lado do texto; o "=" decorativo só
  // preenche o que sobrar, e só nos dois lados IGUAL — nunca um "=" sozinho
  // de um lado só. Quando sobra 1 coluna (número ímpar, ex: "VEGA" é 1
  // caractere mais curto que "FLORA"), essa coluna vira espaço em branco
  // em vez de virar um "=" solto e torto.
  int espacoRestante = 20 - meio.length() - 2;
  if (espacoRestante < 0) espacoRestante = 0;
  int esquerda = espacoRestante / 2; // arredonda pra baixo — sobra ímpar vira espaço, nunca "="
  int direita = esquerda;

  String linha = "";
  for (int i = 0; i < esquerda; i++) linha += '=';
  linha += ' ';
  linha += meio;
  linha += ' ';
  for (int i = 0; i < direita; i++) linha += '=';

  if (linha.length() > 20) linha = linha.substring(0, 20);
  while (linha.length() < 20) linha += ' '; // preenche com espaço (invisível), nunca "=" solto
  return linha;
}

/**
 * @brief Mostra temperatura, umidade e VPD no LCD.
 */
void verificarUmidadeComMensagem() {
  float vpd = calcularVPD(lastTemp, lastHumid);
  String classificacao = classificarVPD(vpd);

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(linhaCabecalhoComModo());

  lcd.setCursor(0, 1);
  lcd.print("Temp: ");
  if (!isnan(lastTemp)) {
    lcd.print(lastTemp, 1);
    lcd.print("C  ");
  } else {
    lcd.print("Erro");
  }

  lcd.setCursor(0, 2);
  lcd.print("Umid: ");
  if (!isnan(lastHumid)) {
    lcd.print(lastHumid, 1);
    lcd.print("%  ");
  } else {
    lcd.print("Erro");
  }

  lcd.setCursor(0, 3);
  lcd.print("VPD : ");
  if (!isnan(vpd)) {
    lcd.print(vpd, 2);
    lcd.print(" ");
    lcd.print(classificacao);
  } else {
    lcd.print("Erro");
  }
}

/**
 * @brief Mostra uma tela de até 2 vasos (vasoInicio e vasoInicio+1), no
 *        mesmo formato de sempre: "Última rega" no topo, uma linha de
 *        barra por vaso, "Próx rega em" embaixo. Com mais de 2 vasos
 *        configurados, essa tela é chamada duas vezes em rodízio (vasos
 *        1-2, depois vasos 3-4) em vez de tentar espremer tudo numa tela
 *        só — ver telaAtual no loop().
 */
void mostrarTelaVasos(int vasoInicio) {
  unsigned long agora = millis();
  unsigned long tempoDesdeUltimaRega = agora - ultimaRegaMillis;
  int minutosDesdeUltimaRega = tempoDesdeUltimaRega / 60000;

  int minutosParaProximaRega = (intervaloRega - tempoDesdeUltimaRega) / 60000;
  if (minutosParaProximaRega < 0)  minutosParaProximaRega = 0;
  if (minutosParaProximaRega > 120) minutosParaProximaRega = 120;

  lcd.setCursor(0, 0);
  lcd.print("Ultima rega: ");
  lcd.print(minutosDesdeUltimaRega);
  lcd.print("m   ");

  for (int i = 0; i < 2; i++) {
    int v = vasoInicio + i;
    int linha = 1 + i;

    if (v >= numVasos) {
      lcd.setCursor(0, linha);
      lcd.print("                    ");
      continue;
    }

    int moistureAvg = calcularUmidadeMedia(v);

    lcd.setCursor(0, linha);
    lcd.print("Vaso ");
    lcd.print(v + 1);
    lcd.print(": ");

    if (moistureAvg < 0) {
      lcd.print("Sem sensor  "); // "Vaso N: " (8) + 12 = 20 colunas certinho
      continue;
    }

    int barraTamanho = map(moistureAvg, 45, 65, 0, 8);
    barraTamanho = constrain(barraTamanho, 0, 8);

    // Exibe uma barrinha de "█" (char 255)
    for (int j = 0; j < barraTamanho; j++) {
      lcd.print((char)255);
    }
    for (int j = barraTamanho; j < 10; j++) {
      lcd.print(" ");
    }

    lcd.setCursor(17, linha);
    lcd.print(moistureAvg);
    lcd.print("%");
  }

  lcd.setCursor(0, 3);
  if (!regaAutomaticaAtiva) {
    lcd.print("Rega automatica OFF ");
  } else if (minutosParaProximaRega > 0) {
    lcd.print("Prox rega em: ");
    lcd.print(minutosParaProximaRega);
    lcd.print("m  ");
  } else {
    lcd.print("Iniciando Rega!     ");
  }
}

// --------------------------------------------------
// 4) Funções de rega e verificação
// --------------------------------------------------

/**
 * @brief Espera em passos de 1s (em vez de um delay() único), permitindo
 *        drenar comandos seriais durante a rega e interromper na hora se
 *        chegar um "PARAR". Também manda a telemetria (DADOS:) a cada
 *        passo — sem isso, o dashboard/app fica sem NENHUMA leitura durante
 *        toda a rega (que pode levar minutos, dependendo do volume
 *        configurado), parecendo que o Arduino travou. Desliga os relés
 *        informados e retorna false se foi parado.
 */
bool aguardarComChecagem(unsigned long duracaoMs, int* reles, int totalReles) {
  unsigned long decorrido = 0;
  while (decorrido < duracaoMs) {
    delay(1000);
    decorrido += 1000;
    lerComandosSerial();
    enviarDadosSerial();
    if (pedidoParada) {
      for (int i = 0; i < totalReles; i++) digitalWrite(reles[i], HIGH);
      return false;
    }
  }
  return true;
}

/**
 * @brief Teste manual: liga cada uma das 4 bombas por vez (10s cada),
 *        desligando antes de passar pra próxima — nunca duas ao mesmo
 *        tempo. Só pra testar fiação/funcionamento, sem lógica de volume
 *        ou umidade. Pode ser interrompido a qualquer momento com "PARAR"
 *        (mesmo mecanismo da rega normal).
 */
void testarBombasSequencial() {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(linhaCabecalhoComModo());
  lcd.setCursor(0, 3);
  lcd.print("     Aguarde...     ");

  for (int slot = 0; slot < NUM_SLOTS; slot++) {
    lcd.setCursor(0, 2);
    lcd.print("Testando bomba ");
    lcd.print(slot + 1);
    lcd.print("    ");

    digitalWrite(relayPins[slot], LOW);
    int rele[1] = { relayPins[slot] };
    bool concluiu = aguardarComChecagem(10000, rele, 1);
    digitalWrite(relayPins[slot], HIGH);

    if (!concluiu) {
      pedidoParada = false; // aguardarComChecagem já desligou o relé
      break;
    }
  }
  testeBombasAtivo = false;
  Serial.println(F("TESTE_BOMBAS_OK:CONCLUIDO"));
}

/**
 * @brief Inicia o ciclo de rega para o vaso especificado, ligando
 *        simultaneamente todas as bombas atribuídas a ele (1 a 4, conforme
 *        a topologia configurada). "motivo" é "AUTO" ou "MANUAL".
 */
void iniciarRega(int vasoIndex, String motivo) {
  int relesDoVaso[NUM_SLOTS];
  int totalReles = 0;
  for (int slot = 0; slot < NUM_SLOTS; slot++) {
    if (bombaParaVaso[slot] == vasoIndex) {
      relesDoVaso[totalReles] = relayPins[slot];
      totalReles++;
    }
  }

  if (totalReles == 0) {
    // Vaso sem nenhum slot atribuído (topologia inconsistente) — não tem
    // bomba nenhuma pra acionar, aborta com segurança.
    Serial.print(F("ERRO:VASO_SEM_SLOTS:"));
    Serial.println(vasoIndex + 1);
    return;
  }

  unsigned long tempoNecessarioVaso = calcularTempoNecessario(totalReles);
  unsigned long tempoIrrigacaoTotal = 0;
  unsigned long inicioMillis = millis();

  regaEmAndamento = true;
  pedidoParada = false;

  Serial.print(F("REGA_INICIO:"));
  Serial.print(vasoIndex + 1);
  Serial.print(F(":"));
  Serial.println(motivo);

  int moistureAvg = calcularUmidadeMedia(vasoIndex);
  // Se o tempo calculado pelo volume/vazão já bateu no teto de segurança,
  // o fim do laço abaixo é o teto interrompendo, não o volume alvo de
  // verdade sendo atingido — o log deixa isso explícito (útil pra notar
  // que o volume configurado está alto demais pra vazão das bombas).
  bool tetoDeSegurancaAtivo = tempoNecessarioVaso >= TEMPO_MAXIMO_REGA_MS;
  String causa = tetoDeSegurancaAtivo ? "TEMPO_MAXIMO_ATINGIDO" : "VOLUME_ATINGIDO";

  while (tempoIrrigacaoTotal < tempoNecessarioVaso) {
    for (int i = 0; i < totalReles; i++) digitalWrite(relesDoVaso[i], LOW);

    if (!aguardarComChecagem(20000, relesDoVaso, totalReles)) {
      causa = "PARADA_MANUAL";
      break;
    }

    for (int i = 0; i < totalReles; i++) digitalWrite(relesDoVaso[i], HIGH);

    tempoIrrigacaoTotal += 20000;

    moistureAvg = calcularUmidadeMedia(vasoIndex);
    if (moistureAvg >= moistureInterruptThreshold) {
      causa = "LIMITE_UMIDADE";
      break;
    }

    // Aguarda absorção (bombas já desligadas)
    if (!aguardarComChecagem(30000, relesDoVaso, totalReles)) {
      causa = "PARADA_MANUAL";
      break;
    }
  }

  // Garante que os relés fiquem desligados, qualquer que tenha sido o motivo de saída
  for (int i = 0; i < totalReles; i++) digitalWrite(relesDoVaso[i], HIGH);

  unsigned long duracaoSegundos = (millis() - inicioMillis) / 1000;
  moistureAvg = calcularUmidadeMedia(vasoIndex);

  contagemRegas++;
  Serial.print(F("REGA_FIM:"));
  Serial.print(vasoIndex + 1);
  Serial.print(F(":"));
  Serial.print(motivo);
  Serial.print(F(":"));
  Serial.print(duracaoSegundos);
  Serial.print(F(":"));
  Serial.print(moistureAvg);
  Serial.print(F(":"));
  Serial.println(causa);

  pedidoParada = false;
  regaEmAndamento = false;
}

/**
 * @brief Rega manual disparada pelo comando serial "REGAR:<vaso>".
 */
void regarManual(int vasoIndex) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(linhaCabecalhoComModo());
  lcd.setCursor(0, 1);
  lcd.print("Rega manual Vaso ");
  lcd.print(vasoIndex + 1);
  lcd.setCursor(0, 3);
  lcd.print("     Aguarde...     ");

  iniciarRega(vasoIndex, "MANUAL");

  ultimaRegaMillis = millis();
}

/**
 * @brief Verifica se os vasos precisam de rega. Se sim, executa ciclo de
 *        rega, um vaso por vez. Genérico pra 1-4 vasos: em vez de tentar
 *        mostrar o status de cada vaso numa linha fixa do LCD (que não
 *        cabe com mais de 2 vasos), mostra um resumo "Vasos secos: X/N" e
 *        qual vaso está sendo regado no momento.
 */
void verificarUmidadeERega() {
  bool vasoSeco[NUM_SLOTS] = {false, false, false, false};
  int totalSecos = 0;

  for (int v = 0; v < numVasos; v++) {
    int moistureAvg = calcularUmidadeMedia(v);
    // moistureAvg == -1 = sem sensor confiável nesse vaso agora — nunca
    // trata como "seco" (senão regaria um vaso sem leitura de verdade).
    if (moistureAvg >= 0 && moistureAvg < moistureThresholdLow) {
      vasoSeco[v] = true;
      totalSecos++;
    }
  }

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(linhaCabecalhoComModo());
  lcd.setCursor(0, 1);
  // Pequeno respiro entre trocar de linha e escrever o próximo caractere —
  // sem isso, o primeiro caractere da linha 1 (o "V" de "Vasos") às vezes
  // se perde, escrito rápido demais logo depois dos 20 caracteres da linha
  // 0 (o display por I2C não tem tempo de assentar no novo endereço).
  delay(2);
  lcd.print("Vasos secos: ");
  lcd.print(totalSecos);
  lcd.print("/");
  lcd.print(numVasos);
  lcd.print("   ");
  lcd.setCursor(0, 3);
  lcd.print("     Aguarde...     ");

  for (int v = 0; v < numVasos; v++) {
    if (!vasoSeco[v]) continue;

    lcd.setCursor(0, 2);
    lcd.print("Regando Vaso ");
    lcd.print(v + 1);
    lcd.print("       ");

    iniciarRega(v, "AUTO");

    int moistureAvg = calcularUmidadeMedia(v);
    String linha3;
    if (moistureAvg >= moistureInterruptThreshold) {
      linha3 = "Limite " + String(moistureInterruptThreshold) + "% atingido";
    } else {
      linha3 = "Volume atingido V" + String(v + 1);
    }
    while (linha3.length() < 20) linha3 += ' ';
    if (linha3.length() > 20) linha3 = linha3.substring(0, 20);

    lcd.setCursor(0, 3);
    lcd.print(linha3);
    delay(4000); // um resumo rápido por vaso, pra não acumular muito tempo quando vários vasos estão secos ao mesmo tempo
    lcd.setCursor(0, 3);
    lcd.print("     Aguarde...     ");
  }

  // Atualiza tempo da última rega
  ultimaRegaMillis = millis();
}

// --------------------------------------------------
// 5) Comunicação Serial
// --------------------------------------------------

/**
 * @brief Envia dados (umidade de cada vaso, tempo de rega, VPD etc.) via
 *        Serial para o Python. Formato variável (1 a 4 vasos), no mesmo
 *        estilo chave=valor usado em "CFG:"/"TOPO:":
 *        DADOS:VASOS=3;U1=62;U2=58;U3=71;ULTIMA=3;PROXIMA=45;TEMP=24.1;UMI=55.3;VPD=1.12;VPDC=Flora IN
 */
void enviarDadosSerial() {
  unsigned long agora = millis();
  int minutosDesdeUltimaRega = (agora - ultimaRegaMillis) / 60000;
  int minutosParaProximaRega =
    (intervaloRega - (agora - ultimaRegaMillis)) / 60000;
  if (minutosParaProximaRega < 0) {
    minutosParaProximaRega = 0;
  }

  float vpd = calcularVPD(lastTemp, lastHumid);
  String classificacao = classificarVPD(vpd);

  Serial.print(F("DADOS:VASOS="));
  Serial.print(numVasos);

  for (int v = 0; v < numVasos; v++) {
    Serial.print(F(";U"));
    Serial.print(v + 1);
    Serial.print(F("="));
    Serial.print(calcularUmidadeMedia(v));
  }

  Serial.print(F(";ULTIMA="));
  Serial.print(minutosDesdeUltimaRega);
  Serial.print(F(";PROXIMA="));
  Serial.print(minutosParaProximaRega);
  Serial.print(F(";TEMP="));
  if (!isnan(lastTemp)) Serial.print(lastTemp, 1);
  else Serial.print(F("Err"));
  Serial.print(F(";UMI="));
  if (!isnan(lastHumid)) Serial.print(lastHumid, 1);
  else Serial.print(F("Err"));
  Serial.print(F(";VPD="));
  if (!isnan(vpd)) Serial.print(vpd, 2);
  else Serial.print(F("Err"));
  Serial.print(F(";VPDC="));
  Serial.print(classificacao);
  Serial.print(F(";AUTO="));
  Serial.println(regaAutomaticaAtiva ? 1 : 0);
}

/**
 * @brief Responde ao comando "GET_CFG" com a configuração atual, no mesmo
 *        formato que o Python usa pra enviar "CFG:".
 */
void enviarConfigAtual() {
  Serial.print(F("CFG:LOW="));
  Serial.print(moistureThresholdLow);
  Serial.print(F(";HIGH="));
  Serial.print(moistureInterruptThreshold);
  Serial.print(F(";INTERVALO="));
  Serial.print(intervaloRega / 60000UL);
  Serial.print(F(";VOLUME="));
  Serial.print(volume_necessario, 0);
  Serial.print(F(";MODO="));
  Serial.print(modoAtual);
  Serial.print(F(";AUTO="));
  Serial.println(regaAutomaticaAtiva ? 1 : 0);
}

/**
 * @brief Responde ao comando "GET_TOPO" com a topologia atual (quantos
 *        vasos, e a qual vaso cada sensor/bomba pertence — S=sensor,
 *        B=bomba, independentes), no mesmo formato que o Python usa pra
 *        enviar "TOPO:".
 */
void enviarTopologiaAtual() {
  Serial.print(F("TOPO:N="));
  Serial.print(numVasos);
  for (int slot = 0; slot < NUM_SLOTS; slot++) {
    Serial.print(F(";S"));
    Serial.print(slot);
    Serial.print(F("="));
    Serial.print(sensorParaVaso[slot] + 1);
  }
  for (int slot = 0; slot < NUM_SLOTS; slot++) {
    Serial.print(F(";B"));
    Serial.print(slot);
    Serial.print(F("="));
    Serial.print(bombaParaVaso[slot] + 1);
  }
  Serial.println();
}

/**
 * @brief Interpreta uma linha de comando recebida via Serial.
 *        Formatos aceitos:
 *          CFG:LOW=45;HIGH=65;INTERVALO=120;VOLUME=1000;MODO=VEGA
 *          GET_CFG
 *          TOPO:N=3;S0=1;S1=1;S2=2;S3=3   (vasos 1-indexados, slot 0-indexado)
 *          GET_TOPO
 *          REGAR:1  /  REGAR:2  /  ...
 *          PARAR
 *          REINICIAR
 */
void processarComando(String linha) {
  linha.trim();
  if (linha.length() == 0) return;

  if (linha.startsWith("CFG:")) {
    String corpo = linha.substring(4);
    bool ok = true;
    int inicio = 0;

    while (inicio < (int)corpo.length()) {
      int fimPar = corpo.indexOf(';', inicio);
      String par = (fimPar == -1) ? corpo.substring(inicio) : corpo.substring(inicio, fimPar);
      int igual = par.indexOf('=');

      if (igual > 0) {
        String chave = par.substring(0, igual);
        String valor = par.substring(igual + 1);

        if (chave == "LOW") {
          moistureThresholdLow = valor.toInt();
        } else if (chave == "HIGH") {
          moistureInterruptThreshold = valor.toInt();
        } else if (chave == "INTERVALO") {
          intervaloRega = (unsigned long)valor.toInt() * 60000UL;
        } else if (chave == "VOLUME") {
          volume_necessario = valor.toFloat();
        } else if (chave == "MODO") {
          modoAtual = valor;
        } else if (chave == "AUTO") {
          bool novoValor = (valor.toInt() != 0);
          if (novoValor && !regaAutomaticaAtiva) {
            // Reativou agora — recomeça a contar o intervalo do zero, em
            // vez de já disparar uma checagem na hora por causa do tempo
            // que passou desligada.
            ultimaRegaMillis = millis();
          }
          regaAutomaticaAtiva = novoValor;
        } else {
          ok = false;
        }
      }

      if (fimPar == -1) break;
      inicio = fimPar + 1;
    }

    if (ok) salvarConfigNaEEPROM();
    Serial.println(ok ? F("CFG_OK") : F("CFG_ERRO:chave_desconhecida"));
  }
  else if (linha == "GET_CFG") {
    enviarConfigAtual();
  }
  else if (linha.startsWith("TOPO:")) {
    String corpo = linha.substring(5);
    bool ok = true;
    int novoNumVasos = numVasos;
    int novoSensorParaVaso[NUM_SLOTS];
    int novoBombaParaVaso[NUM_SLOTS];
    for (int i = 0; i < NUM_SLOTS; i++) {
      novoSensorParaVaso[i] = sensorParaVaso[i];
      novoBombaParaVaso[i] = bombaParaVaso[i];
    }

    int inicio = 0;
    while (inicio < (int)corpo.length()) {
      int fimPar = corpo.indexOf(';', inicio);
      String par = (fimPar == -1) ? corpo.substring(inicio) : corpo.substring(inicio, fimPar);
      int igual = par.indexOf('=');

      if (igual > 0) {
        String chave = par.substring(0, igual);
        String valor = par.substring(igual + 1);

        if (chave == "N") {
          novoNumVasos = valor.toInt();
        } else if (chave.length() == 2 && chave.charAt(0) == 'S') {
          int slot = chave.substring(1).toInt();
          if (slot >= 0 && slot < NUM_SLOTS) {
            novoSensorParaVaso[slot] = valor.toInt() - 1; // vem 1-indexado do Python
          } else {
            ok = false;
          }
        } else if (chave.length() == 2 && chave.charAt(0) == 'B') {
          int slot = chave.substring(1).toInt();
          if (slot >= 0 && slot < NUM_SLOTS) {
            novoBombaParaVaso[slot] = valor.toInt() - 1; // vem 1-indexado do Python
          } else {
            ok = false;
          }
        } else {
          ok = false;
        }
      }

      if (fimPar == -1) break;
      inicio = fimPar + 1;
    }

    if (novoNumVasos < 1 || novoNumVasos > NUM_SLOTS) ok = false;

    if (ok) {
      numVasos = novoNumVasos;
      for (int i = 0; i < NUM_SLOTS; i++) {
        sensorParaVaso[i] = novoSensorParaVaso[i];
        bombaParaVaso[i] = novoBombaParaVaso[i];
      }
      salvarConfigNaEEPROM();
      Serial.println(F("TOPO_OK"));
    } else {
      Serial.println(F("TOPO_ERRO:invalido"));
    }
  }
  else if (linha == "GET_TOPO") {
    enviarTopologiaAtual();
  }
  else if (linha.startsWith("REGAR:")) {
    int vaso = linha.substring(6).toInt();
    if (vaso < 1 || vaso > numVasos) {
      Serial.println(F("ERRO:VASO_INVALIDO"));
    } else if (regaEmAndamento || testeBombasAtivo || bombaManualAtiva >= 0) {
      Serial.println(F("ERRO:REGA_EM_ANDAMENTO"));
    } else {
      regarManual(vaso - 1);
    }
  }
  else if (linha == "PARAR") {
    pedidoParada = true;
  }
  else if (linha == "TESTE_BOMBAS:LIGAR") {
    if (regaEmAndamento || bombaManualAtiva >= 0) {
      Serial.println(F("ERRO:REGA_EM_ANDAMENTO"));
    } else if (testeBombasAtivo) {
      Serial.println(F("ERRO:TESTE_BOMBAS_EM_ANDAMENTO"));
    } else {
      testeBombasAtivo = true;
      Serial.println(F("TESTE_BOMBAS_OK:INICIADO"));
      testarBombasSequencial();
    }
  }
  else if (linha == "TESTE_BOMBAS:DESLIGAR") {
    pedidoParada = true; // mesmo mecanismo de interrupção do "PARAR"
  }
  else if (linha.startsWith("TESTE_BOMBA:LIGAR:")) {
    int slotBomba = linha.substring(18).toInt() - 1; // vem 1-indexado do Python

    if (regaEmAndamento || testeBombasAtivo) {
      Serial.println(F("ERRO:REGA_EM_ANDAMENTO"));
    } else if (slotBomba < 0 || slotBomba >= NUM_SLOTS) {
      Serial.println(F("ERRO:BOMBA_INVALIDA"));
    } else {
      // só uma bomba manual ligada por vez — desliga a anterior primeiro
      if (bombaManualAtiva >= 0) digitalWrite(relayPins[bombaManualAtiva], HIGH);
      digitalWrite(relayPins[slotBomba], LOW);
      bombaManualAtiva = slotBomba;
      bombaManualInicioMillis = millis();
      Serial.print(F("TESTE_BOMBA_OK:LIGADA:"));
      Serial.println(slotBomba + 1);
    }
  }
  else if (linha == "TESTE_BOMBA:DESLIGAR") {
    if (bombaManualAtiva >= 0) {
      digitalWrite(relayPins[bombaManualAtiva], HIGH);
      bombaManualAtiva = -1;
    }
    Serial.println(F("TESTE_BOMBA_OK:DESLIGADA"));
  }
  else if (linha == "REINICIAR") {
    if (regaEmAndamento || testeBombasAtivo || bombaManualAtiva >= 0) {
      // Evita resetar com bomba ligada (relés ficam em estado indefinido
      // por um instante durante o reset) — peça pra parar a rega/teste antes.
      Serial.println(F("ERRO:REGA_EM_ANDAMENTO"));
    } else {
      Serial.println(F("REINICIANDO"));
      delay(50); // garante que a mensagem saia pela serial antes do reset
      wdt_enable(WDTO_15MS);
      while (true) {} // trava até o watchdog forçar o reset da placa
    }
  }
}

/**
 * @brief Drena bytes disponíveis na Serial sem bloquear o loop principal,
 *        acumulando até achar uma quebra de linha e despachando o comando.
 */
/**
 * @brief Espera em passos curtos, chamando lerComandosSerial() a cada
 *        passo — em vez de um delay() único e cego. Usado no setup() (nas
 *        telas de splash/status, ~15s de espera ao todo): sem isso, o
 *        Arduino não drena o buffer serial (só 64 bytes) enquanto mostra
 *        essas telas, e o Python já manda CFG:+TOPO: ~2s depois de
 *        conectar — os dois comandos juntos passam de 64 bytes e o que
 *        vier depois (a topologia) se perde ou chega corrompido.
 */
void aguardarDrenandoSerial(unsigned long duracaoMs) {
  unsigned long decorrido = 0;
  while (decorrido < duracaoMs) {
    delay(50);
    decorrido += 50;
    lerComandosSerial();
  }
}

void lerComandosSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      processarComando(bufferSerial);
      bufferSerial = "";
    } else if (c != '\r') {
      bufferSerial += c;
      if (bufferSerial.length() > 120) {
        // Buffer de comando maior que o esperado — descarta pra não travar
        bufferSerial = "";
      }
    }
  }
}

// --------------------------------------------------
// 6) Setup
// --------------------------------------------------
void setup() {
  wdt_disable(); // em alguns bootloaders o watchdog continua ativo após o
                 // reset provocado pelo comando "REINICIAR" — desliga logo
                 // de cara pra não cair num loop de resets.

  // Lê a config/topologia da EEPROM ANTES de qualquer tela — assim o LCD
  // já nasce mostrando o modo/vasos corretos, sem depender do Python
  // conseguir reenviar "CFG:"/"TOPO:" a tempo.
  carregarConfigDaEEPROM();

  Serial.begin(9600);
  lcd.init();
  lcd.backlight();
  lcd.clear();
  dht.begin();

  // Configura pinos de relé (todos os 4 slots físicos, mesmo que a
  // topologia atual não use algum deles)
  for (int i = 0; i < NUM_SLOTS; i++) {
    pinMode(relayPins[i], OUTPUT);
    digitalWrite(relayPins[i], HIGH);
  }

  // Tela inicial
  lcd.clear();
  telaApresentacao();
  aguardarDrenandoSerial(5000);

  // Leitura inicial do DHT
  lastTemp  = dht.readTemperature();
  lastHumid = dht.readHumidity();
  lastDHTReadMillis = millis();

  // Cronômetro de rega começa a contar do zero a partir do boot — NÃO
  // checa/rega imediatamente ao ligar/reiniciar. A primeira checagem só
  // acontece depois de "intervaloRega" decorrido (ver loop()), igual a
  // qualquer outro ciclo normal.
  ultimaRegaMillis = millis();

  // Exibe status inicial
  lcd.clear();
  verificarUmidadeComMensagem();
  aguardarDrenandoSerial(10000);
}

// --------------------------------------------------
// 7) Loop principal
// --------------------------------------------------
void loop() {
  unsigned long agora = millis();

  // Processa comandos vindos do Python (CFG/TOPO/GET_CFG/GET_TOPO/REGAR/
  // PARAR/REINICIAR/TESTE_BOMBAS). "REGAR:" roda a rega manual de forma
  // bloqueante aqui dentro (pode levar bastante tempo) — recalcula "agora"
  // depois, senão os testes de tempo abaixo (bomba manual, checagem
  // automática) comparam contra um timestamp desatualizado e "estouram"
  // (unsigned long: agora_antigo - ultimaRegaMillis_novo vira um número
  // gigante), disparando a rega automática na hora, logo após qualquer
  // rega manual — foi exatamente o bug reportado ("pedi só um vaso e ele
  // regou outro em seguida").
  lerComandosSerial();
  agora = millis();

  // Segurança: desliga sozinha a bomba manual depois de
  // BOMBA_MANUAL_TIMEOUT_MS, mesmo se ninguém mandar "TESTE_BOMBA:DESLIGAR".
  if (bombaManualAtiva >= 0 && (agora - bombaManualInicioMillis >= BOMBA_MANUAL_TIMEOUT_MS)) {
    digitalWrite(relayPins[bombaManualAtiva], HIGH);
    bombaManualAtiva = -1;
    Serial.println(F("TESTE_BOMBA_TIMEOUT"));
  }

  // Leitura do DHT a cada 2s
  if (agora - lastDHTReadMillis >= dhtInterval) {
    float t = dht.readTemperature();
    float h = dht.readHumidity();

    // Se t ou h forem inválidos (NAN), avisa e não atualiza lastTemp/lastHumid
    if (isnan(t) || isnan(h)) {
      Serial.println("Falha ao ler DHT! Mantendo valores anteriores de temperatura/umidade.");
    } else {
      // Se ambos são válidos, atualiza as variáveis globais
      lastTemp  = t;
      lastHumid = h;
    }

    lastDHTReadMillis = agora;
  }

  // Envia dados serial a cada 1s
  enviarDadosSerial();
  delay(1000);

  // Alterna telas no LCD a cada 10s: tela 0 = temp/umidade/VPD, tela 1 =
  // vasos 1-2, tela 2 = vasos 3-4 (só entra no rodízio com mais de 2 vasos
  // configurados).
  if (agora - ultimoDisplayMillis >= intervaloDisplay) {
    int totalTelas = (numVasos > 2) ? 3 : 2;
    telaAtual = (telaAtual + 1) % totalTelas;
    ultimoDisplayMillis = agora;
    lcd.clear();

    if (telaAtual == 0) {
      verificarUmidadeComMensagem();
      delay(2000);
    } else if (telaAtual == 1) {
      mostrarTelaVasos(0);
    } else {
      mostrarTelaVasos(2);
    }
  }

  // Verifica se é hora de regar — só quando a rega automática está ligada
  // (toggle "Rega automática" em Ajustes). Desligada, só a rega manual
  // (botões/REGAR:) funciona; o Arduino nunca rega sozinho.
  if (regaAutomaticaAtiva && (agora - ultimaRegaMillis >= intervaloRega)) {
    bool precisaRega = false;
    for (int v = 0; v < numVasos; v++) {
      int moistureAvg = calcularUmidadeMedia(v);
      // -1 = sem sensor confiável nesse vaso agora — nunca conta como
      // "precisa de rega" (mesma regra de dentro de verificarUmidadeERega).
      if (moistureAvg >= 0 && moistureAvg < moistureThresholdLow) {
        precisaRega = true;
        break;
      }
    }
    if (precisaRega) {
      lcd.clear();
      verificarUmidadeERega();
    } else {
      // Mesmo sem rega, atualiza tempo
      ultimaRegaMillis = agora;
    }
  }
}
