# Memória da conversa — GreenHouse

Resumo de tudo que foi discutido e implementado nas sessões de trabalho
neste projeto, pra referência futura (sua ou de quem continuar o
desenvolvimento).

## 1) Topologia flexível de vasos/sensores/bombas

- Antes: fixo em 2 vasos, 2 sensores por vaso, sensor e bomba sempre
  juntos.
- Depois: 1 a 4 vasos, cada um com uma quantidade diferente de
  sensores/bombas (dos 4 slots físicos fixos: A0-A3 + relés 8-11).
- **Modo avançado** (toggle em Ajustes): sensor e bomba do mesmo slot
  físico podem pertencer a vasos diferentes — permite um vaso só de
  monitoramento (sensor sem bomba) ou uma bomba sem sensor dedicado.
- Configurado pela página **Ajustes** → "Vasos e sensores". Protocolo
  serial: `TOPO:N=..;S0=..;S1=..;S2=..;S3=..;B0=..;B1=..;B2=..;B3=..`
  (S=sensor, B=bomba, independentes).
- Banco: tabela `topologia_vasos` com colunas `sensores`/`bombas` (CSV).

## 2) Detecção de sensor desconectado/instável

- Um pino analógico sem sensor (flutuando) varia muito entre leituras
  próximas — isso é usado pra detectar "sem sensor" (spread alto por
  `LIMIAR_DEBOUNCE` rodadas seguidas).
- Vaso sem sensor confiável retorna `-1` (nunca `0%`) e nunca dispara
  rega automática.
- Suavização (EMA) reduz ruído em leituras de sensores conectados.
- LCD mostra "Sem sensor"; painel web mostra aviso amarelo e o gráfico
  pula o ponto (sem linha reta em 0%).
- Período de carência de 8s no boot evita falso positivo por causa do
  circuito do sensor ainda assentando eletricamente.

## 3) Persistência em EEPROM (Arduino)

- Config e topologia são gravadas na EEPROM assim que aplicadas
  (`CFG:`/`TOPO:` válidos) e lidas de lá logo no início do `setup()` —
  antes de qualquer tela do LCD. Isso evita depender do timing exato do
  Python reenviar a config depois de cada boot.
- Byte "magic" (`0xA6`) versiona o layout — se o firmware mudar a
  struct, a EEPROM antiga é tratada como virgem em vez de lida errada.

## 4) Rega automática (toggle)

- Toggle "Rega automática" em Ajustes: desligado, só rega manualmente;
  ligado, funciona como sempre (checa a cada intervalo configurado).
- Reativar reinicia a contagem do intervalo do zero (não dispara na
  hora).
- Painel mostra "Desativada" em vez de uma contagem regressiva falsa
  quando está desligada.

## 5) Bug crítico: rega bloqueava o Arduino inteiro

- **Causa raiz** de "Arduino parece travado depois de regar
  manualmente": a rega (manual ou automática) bloqueia o `loop()`
  inteiro do Arduino Uno durante todo o ciclo de pulsos — nenhuma
  leitura (`DADOS:`) era enviada nesse tempo. Se a umidade nunca
  atingisse o limite de corte, o bloqueio durava o tempo **inteiro**
  calculado pelo volume configurado (podendo ser dezenas de minutos).
- **Corrigido**: telemetria agora é enviada a cada segundo mesmo durante
  a rega; teto de segurança de 5 minutos de bomba ativa
  (`TEMPO_MAXIMO_REGA_MS`) — nenhuma rega trava além disso.
- Log da rega (`REGA_FIM`) distingue `TEMPO_MAXIMO_ATINGIDO` de
  `VOLUME_ATINGIDO` de verdade.

## 6) Bug: comandos conflitantes retornavam "sucesso" falso

- O backend Python só confirmava que **escreveu** o comando na serial,
  nunca lia a resposta do Arduino — um clique duplo (ou dois vasos em
  sequência rápida) recebia "sucesso" dos dois, mesmo o Arduino
  rejeitando o segundo silenciosamente.
- **Corrigido**: `app.py` agora espelha o estado "ocupado" do Arduino
  (`arduino_ocupado`, atualizado via `REGA_INICIO`/`REGA_FIM`/
  `TESTE_BOMBAS_OK`/`TESTE_BOMBA_OK`) e rejeita com erro 409 **antes**
  de mandar o comando, se algo já estiver em andamento.
- Testado com um Arduino simulado (porta serial virtual via `socat`) —
  login, dashboard, todos os botões, histórico, relatórios, conflitos de
  comando — sem erros.

## 7) Controles manuais de bomba

- **Teste de bombas sequencial**: liga cada uma das 4 bombas por 10s,
  uma de cada vez (nunca duas juntas) — botão em Ajustes → Manutenção.
- **Bomba manual individual**: escolher e ligar 1 bomba específica na
  hora (desliga sozinha em 2 min por segurança).
- Ambos ficam dentro do card "Manutenção" (junto com "Reiniciar
  Arduino").

## 8) Câmera ao vivo (DVR Intelbras via go2rtc)

- Serviço `go2rtc` no `docker-compose.yml`, convertendo RTSP do DVR em
  MJPEG. Flask faz proxy em `/camera/stream`, protegido pelo login —
  `go2rtc` nunca fica exposto direto na internet.
- Config em `go2rtc.yaml` (gitignored): fonte
  `ffmpeg:rtsp://...#video=mjpeg#raw=-q:v 4 -r 12` (stream principal,
  qualidade JPEG alta, 12fps — ajustável se pesar demais no servidor).
- Painel web tem card "Câmera" com auto-retry se a conexão cair.

## 9) Sensores de umidade — troca futura

- Sensor resistivo com hastes "resistentes à corrosão" (LM393 +
  potenciômetro) é compatível eletricamente (mesmo `analogRead()`), só
  precisa recalibrar `valor_seco`/`valor_umido` por slot.
- Continua sendo resistivo (corrente DC nas hastes) — ainda sofre
  corrosão eletrolítica com o tempo, só mais devagar que o sensor atual.
  Sensor capacitivo seria imune a isso, mas é outra faixa de calibração.

## 10) Projetos de hardware gerados (pastas separadas)

- **`greenhouse-esp32/`** — proposta de próxima geração: ESP32 com WiFi
  (sem cabo USB até o servidor), PCB organizada, módulos de relé
  plugáveis, DevKit em soquete. Firmware completo com arquitetura de 2
  tarefas FreeRTOS (WiFi/HTTP nunca trava esperando a rega terminar —
  resolve de raiz o bug do item 5). Não testado em hardware real (sem
  toolchain ESP32 disponível pra compilar aqui). Inclui BOM, esquemático
  descrito, guia de leiaute de PCB, e exemplo de integração com o Flask.
- **`greenhouse-arduino-atual/`** — documentação da configuração ATUAL
  (Arduino Uno), pra caso precise religar algo. Pinout exato (vem do
  firmware real, 100% confiável) + guia de fiação com planilha pra você
  preencher (cor do fio ↔ função), já que não dá pra confirmar isso só
  pela foto do sistema montado.

## Pendências / próximos passos conhecidos

- Regravar o `.ino` do Arduino Uno com todas as correções mais recentes,
  se ainda não tiver feito.
- Preencher a planilha de fiação em `greenhouse-arduino-atual/hardware/guia-fiacao.md`
  enquanto o sistema ainda está funcionando.
- Se for adiante com o projeto ESP32: montar em protoboard primeiro,
  recalibrar sensores (`GET /debug/adc`), validar tudo antes de mandar
  fabricar PCB.
- Confirmar tensão/corrente real das bombas e completar o BOM do sistema
  atual.
