# GreenHouse — monitoramento e rega automática

Dashboard web (Flask + SocketIO) que lê dados de um Arduino conectado via USB
(sensores de umidade do solo, DHT22, controle de bombas) e exibe em tempo real,
com histórico em SQLite.

- Domínio: `greenhouse.gvtserver.online`
- Porta interna: `4022`
- Pasta no servidor: `/pendriver/greenhouse`
- Dados persistentes: `/pendriver/greenhouse_hist` (SQLite)

## Arduino

O firmware atual é `arduino/GreenHouseApp/GreenHouseApp.ino` — um sketch único que substitui
os dois sketches antigos (Vega/Flora eram quase idênticos). O modo de cultivo,
limiares de umidade, intervalo entre checagens e volume alvo agora são
configuráveis pela página **Ajustes** do dashboard, sem precisar reflashar o
Arduino. Basta plugar o Arduino via USB no servidor Ubuntu — o app detecta a
porta serial sozinho e envia a configuração atual assim que conecta.

**Importante**: este `.ino` não foi compilado neste ambiente (sem
`arduino-cli`/toolchain disponível) — revisão manual cuidadosa, mas compile e
teste no Arduino IDE / hardware real antes de confiar em produção.

### Protocolo serial (Python ⇄ Arduino)

Python → Arduino (uma linha, terminada em `\n`):
- `CFG:LOW=45;HIGH=65;INTERVALO=120;VOLUME=1000;MODO=VEGA` — aplica config completa (Arduino responde `CFG_OK`/`CFG_ERRO:...`).
- `GET_CFG` — Arduino responde com a config atual, mesmo formato.
- `REGAR:<vaso>` — dispara rega manual daquele vaso (1 até o número de vasos configurado, veja "Vasos e sensores flexíveis" abaixo).
- `PARAR` — interrompe qualquer rega em andamento na hora.

Arduino → Python (telemetria):
- `DADOS:VASOS=3;U1=62;U2=58;U3=71;ULTIMA=3;PROXIMA=45;TEMP=24.1;UMI=55.3;VPD=1.12;VPDC=Flora IN`
  — formato chave=valor, tamanho variável conforme o número de vasos
  configurado (substitui o antigo `Vaso1:x,Vaso2:x` fixo em 2 vasos).
- `REGA_INICIO:<vaso>:<motivo>` — motivo é `AUTO` ou `MANUAL`.
- `REGA_FIM:<vaso>:<motivo>:<duracao_s>:<umidade_final>:<causa>` — causa é
  `LIMITE_UMIDADE`, `VOLUME_ATINGIDO` ou `PARADA_MANUAL`. O Python persiste
  cada evento na tabela `rega_eventos` (substitui o antigo contador em
  memória, que zerava a cada restart do container).

### Vasos e sensores flexíveis (slots)

O hardware tem **4 sensores de umidade fixos** (A0-A3) e **4 relés/bombas
fixos** (pinos 8-11) — isso não muda. O que é configurável é a quais vasos
esses sensores e bombas pertencem: de 1 até 4 vasos, cada um com uma
quantidade diferente de sensores/bombas.

**Modo simples (padrão)**: cada slot físico (sensor+bomba juntos) pertence
a um único vaso — mesmo comportamento de sempre. Um slot pode ficar
**desativado** (opção "Não usar" na UI) — útil quando o número de vasos
não usa todos os 4 slots físicos (ex: 3 vasos com 1 sensor cada, sobrando
1 sensor/bomba sem uso).

**Modo avançado** (toggle "Modo avançado: sensor e bomba separados" em
Ajustes): o sensor e a bomba de um mesmo slot físico podem pertencer a
vasos **diferentes**, ou até um vaso ter só sensor (monitoramento, sem
rega automática/manual) ou só bomba (rega sem leitura de umidade
própria — usa a leitura de outro vaso, ou nenhuma). A página detecta
sozinha se a topologia salva já usa esse modo (sensor ≠ bomba em algum
slot) e abre com o toggle já ligado.

Configuração pela página **Ajustes** → seção "Vasos e sensores": escolher
quantos vasos (1-4) e, pra cada um dos 4 slots físicos, marcar a qual vaso
o sensor pertence (e, no modo avançado, a qual vaso a bomba pertence,
independentemente). Depois de salvar, recarregue a página pra os controles
manuais de rega refletirem os vasos novos.

Protocolo serial (mesmo padrão do `CFG:`/`GET_CFG`):
- `TOPO:N=3;S0=1;S1=1;S2=2;S3=3;B0=1;B1=1;B2=2;B3=0` — aplica a topologia
  (vaso 1-indexado, slot 0-indexado). `S<slot>` = sensor, `B<slot>` = bomba,
  atribuições independentes. Arduino responde `TOPO_OK`/`TOPO_ERRO:...`.
- `GET_TOPO` — Arduino responde com a topologia atual, mesmo formato.

A topologia (junto com a config de umidade/modo) é gravada na EEPROM assim
que aplicada, e lida de lá logo no boot — ver "Persistência em EEPROM"
abaixo. O SQLite (`topologia_vasos`, colunas `sensores`/`bombas`) continua
sendo a referência do lado Python. O firmware recalcula o tempo de rega
necessário por vaso conforme o número de bombas daquele vaso (mais bombas
= enche mais rápido) e tira a média de umidade entre os sensores
atribuídos a ele (1 a 4) — os dois independentes um do outro.

No LCD, a tela de vasos (última/próxima rega + barra de cada vaso) mostra
sempre até 2 vasos por vez. Com mais de 2 vasos configurados, o LCD entra
num rodízio de 3 telas em vez de 2 (temp/umidade/VPD → vasos 1-2 → vasos
3-4), trocando a cada 10s — em vez de espremer todos os vasos numa tela só.

## Rega bloqueia o Arduino inteiro — cuidados

Enquanto uma rega (manual ou automática) está em andamento, o firmware
fica preso dentro de `iniciarRega()`/`aguardarComChecagem()` — é assim que
o controle por pulsos (liga 20s, espera 30s de absorção, repete) funciona.
Durante esse tempo, **nenhum outro comando é processado e a telemetria
(`DADOS:`) só volta depois que a rega termina** — isso já foi corrigido
pra mandar `DADOS:` a cada segundo mesmo durante a rega (antes não
mandava nada, o que parecia o Arduino "travado" no dashboard).

Quanto tempo uma rega pode durar, na pior hipótese (sensor nunca atinge a
umidade de corte — ex: ficou instável durante a rega): o tempo é
`volume_ml / (15,63 × bombas do vaso)` segundos de bomba **ativa**,
distribuído em ciclos de 20s ligada + 30s de absorção — ou seja, o tempo
real de bloqueio é ~2,5× esse valor. Com o preset padrão (1000ml, 1
bomba), isso dá uns 3 minutos; com um volume configurado muito mais alto,
pode passar de dezenas de minutos. Por segurança, existe um **teto de 5
minutos de bomba ativa** (`TEMPO_MAXIMO_REGA_MS`) — nenhuma rega, manual
ou automática, bloqueia além disso, mesmo que o volume configurado seja
exagerado ou a umidade nunca suba. Se isso acontecer, o log da rega
(`REGA_FIM:...:TEMPO_MAXIMO_ATINGIDO`) deixa explícito que foi o teto (não
o volume) que encerrou o ciclo — um sinal de que o volume configurado está
alto demais pra vazão das bombas.

O app Python também espelha se o Arduino está "ocupado" (rega, teste de
bombas, ou bomba manual ligada) a partir das próprias mensagens que o
Arduino manda (`REGA_INICIO`/`REGA_FIM`, `TESTE_BOMBAS_OK:...`,
`TESTE_BOMBA_OK:...`) — as rotas `/api/comando/regar`,
`/api/comando/teste_bombas` e `/api/comando/teste_bomba` checam isso
**antes** de mandar o comando, e respondem com erro 409 em vez de um falso
"sucesso" se algo já estiver em andamento (bug real encontrado e corrigido:
antes, um clique duplo ou dois vasos em sequência rápida recebiam
"sucesso" dos dois, mesmo o Arduino rejeitando o segundo silenciosamente).

## Detecção de sensor desconectado/instável

Um pino analógico sem sensor nenhum ligado (flutuando) capta ruído
elétrico e varia MUITO entre leituras tiradas com poucos ms de diferença —
um sensor de verdade não muda de umidade nesse intervalo tão curto. O
firmware usa isso pra diferenciar "sem sensor" de uma leitura real:

- Cada leitura já era uma mediana de 7 amostras (`lerMedianaSensor`); agora
  também mede a diferença entre a maior e a menor amostra (`spread`). Um
  `spread` acima de `LIMITE_SPREAD_INSTAVEL` (150, calibrável no `.ino`)
  conta como "instável" naquela rodada.
- Depois de `LIMIAR_DEBOUNCE` (3) rodadas seguidas instáveis, o slot é
  marcado como desconectado — evita marcar/desmarcar à toa por causa de um
  pico isolado de ruído.
- `calcularUmidadeMedia()` ignora slots desconectados na média do vaso, e
  retorna `-1` (sentinela "sem sensor") se o vaso não tiver nenhum sensor
  confiável no momento — nunca `0%`, pra não ser confundido com "solo bem
  molhado" nem disparar rega automática à toa.
- Sensores conectados (não desconectados) passam por uma suavização extra
  (média móvel exponencial, fator 0.3) além da mediana — reduz o "pulo"
  de leitura em leitura mesmo com o sensor bem conectado, útil contra
  ruído de rede elétrica ou do chaveamento do relé da bomba.

Isso aparece em três lugares:
- **LCD**: a linha do vaso mostra "Sem sensor" no lugar da barra/%.
- **Dashboard web**: o card do vaso mostra "Sem sensor" e um aviso
  amarelo, e o gráfico simplesmente pula esse ponto (sem linha reta em
  0% nem pico maluco) — o Python nunca insere `-1` no histórico; grava
  `null` na série em tempo real e não grava linha nenhuma pro vaso naquele
  snapshot no SQLite.
- **Rega automática**: um vaso com `-1` nunca é considerado "seco" — não
  dispara rega sem uma leitura de verdade.

## Câmera ao vivo (DVR via go2rtc)

O dashboard mostra o vídeo ao vivo de uma câmera ligada a um DVR na rede
local (testado com Intelbras, DVR HDCVI, mas qualquer DVR com RTSP serve).

Arquitetura: `go2rtc` (container `alexxit/go2rtc`, ver `docker-compose.yml`)
puxa o stream **RTSP** do DVR e reencaminha como **MJPEG** via HTTP. O
Flask faz o proxy desse MJPEG na rota `/camera/stream` — protegida pelo
mesmo login do resto do app. O `go2rtc` **não** tem router/labels do
Traefik: só é alcançável pelo container `greenhouse` dentro da rede docker
interna, nunca fica exposto direto na internet.

Configuração:
1. Copiar `go2rtc.yaml.example` para `go2rtc.yaml` (gitignored — nunca
   commitar com credenciais reais) e trocar a URL RTSP pela do seu DVR:
   `rtsp://usuario:senha@IP:554/cam/realmonitor?channel=N&subtype=1`
   (`subtype=1` = substream, mais leve pra visualização web; `subtype=0` =
   principal, mais pesado). Teste essa URL primeiro no VLC (Mídia → Abrir
   Fluxo de Rede) pra confirmar usuário/senha/porta/canal antes de plugar
   no app.
2. No servidor, `go2rtc.yaml` fica em `/pendriver/greenhouse/go2rtc.yaml`
   (montado como volume no `docker-compose.yml`).
3. Nada a configurar no `.env` a não ser que o nome do stream no
   `go2rtc.yaml` seja diferente de `estufa` (ver `GREENHOUSE_CAMERA_STREAM`
   no `.env.example`).
4. Depois de qualquer alteração no `go2rtc.yaml`, é preciso **reiniciar o
   container** pra recarregar a config (não é hot-reload):
   `docker restart greenhouse-go2rtc`, ou redeploy pelo Coolify.

**A câmera do DVR manda vídeo em H264 (o padrão de qualquer RTSP), mas o
endpoint `/api/stream.mjpeg` do go2rtc não converte H264 → MJPEG sozinho** —
precisa apontar explicitamente pro `ffmpeg` embutido na imagem do go2rtc
fazer essa transcodificação. Por isso a fonte no `go2rtc.yaml` é
`ffmpeg:rtsp://...#video=mjpeg` (prefixo `ffmpeg:` + sufixo `#video=mjpeg`),
em vez de só a URL RTSP crua. Sem esse prefixo, o log do go2rtc mostra
`codecs not matched: video:H264 => video:JPEG, video:RAW` e o vídeo não
aparece no dashboard.

**Ajustando qualidade/desempenho**: a fonte usa `subtype=0` (stream
principal do DVR, resolução alta) em vez do substream — a substream costuma
sair "chiada"/borrada mesmo com JPEG de boa qualidade, já que a resolução
de origem é baixa. Os parâmetros depois de `#raw=` controlam qualidade e
taxa de quadros do JPEG gerado:
- `-q:v N` — qualidade JPEG, de `2` (quase sem perda, mais pesado) a `31`
  (pior, mais leve). Padrão atual: `4`.
- `-r N` — quadros por segundo entregues ao navegador. Padrão atual: `12`
  (de sobra pra monitorar a planta, sem pesar tanto na rede/CPU do servidor
  quanto os ~25-30fps nativos do DVR).

Se a rede/CPU do servidor não aguentar o stream principal em boa
qualidade, volte pra `subtype=1` (substream) e/ou suba o `-q:v` (menos
qualidade, mais leve). Qualquer mudança no `go2rtc.yaml` exige
`docker restart greenhouse-go2rtc` pra valer.

**Importante**: não precisa redirecionar porta nenhuma no roteador/modem —
o servidor já está na mesma rede local que o DVR, então o container
`go2rtc` alcança o DVR direto pelo IP local. Redirecionar a porta RTSP
(554) pra internet seria expor um protocolo com autenticação fraca sem
necessidade nenhuma.

**Não testado com hardware real** (sem DVR/rede local acessível neste
ambiente) — testei a lógica do proxy Flask com um servidor MJPEG simulado
(content-type, streaming de bytes, fallback quando a câmera está offline,
bloqueio sem login — tudo passou), mas o `go2rtc` de verdade puxando RTSP
do DVR real precisa ser validado no servidor. Se o vídeo não aparecer:
- Confira os logs do container: `docker logs greenhouse-go2rtc`.
- A UI web embutida do go2rtc (porta 1984, só interna — acesse via
  `docker exec` ou túnel SSH temporário) deixa testar o stream isolado do
  Flask, útil pra saber se o problema é no go2rtc ou no proxy.

### Persistência em EEPROM — o Arduino fica correto sozinho no boot

O Arduino grava config e topologia na EEPROM assim que recebe um `CFG:`/
`TOPO:` válido (via `salvarConfigNaEEPROM()`), e lê de lá logo no início do
`setup()` — antes de qualquer tela do LCD. Isso existe porque depender só
do Python reenviar `CFG:`/`TOPO:` bem no momento certo depois de cada boot
é uma corrida frágil (o LCD tem telas de splash/status com ~15s de espera
antes do Arduino começar a processar comandos seriais). O SQLite
(`configuracoes`/`topologia_vasos`) continua sendo a referência do lado
Python, e ainda reenvia `CFG:`/`TOPO:` a cada reconexão como sincronização
de segurança (útil se você trocar de placa Arduino) — mas o Arduino não
depende mais disso pra iniciar correto.

### Rega automática (toggle)

A página **Ajustes** tem um toggle "Rega automática". Desligado, o Arduino
só rega quando você mandar manualmente (Controles manuais / API) — nunca
sozinho por tempo/umidade. Ligado, funciona como sempre (checa a cada
`intervalo_min` e rega se algum vaso estiver seco). Reativar o toggle
reinicia a contagem do intervalo do zero, não dispara uma checagem na
hora. Protocolo serial: `AUTO=1`/`AUTO=0` dentro do `CFG:`/`GET_CFG`
(mesmo padrão dos outros campos).

## Auto-detecção da porta serial

Em vez de uma porta fixa (`COM7` como era no Windows), o `app.py` varre as
portas seriais disponíveis (`serial.tools.list_ports`) e escolhe a que bate
com VID:PID conhecidos de chips USB-serial de Arduino (CH340, FTDI, CP210x,
Arduino genuíno). Se nada bater, usa a primeira `/dev/ttyUSB*` ou `/dev/ttyACM*`
encontrada.

- **Hot-plug**: se o Arduino for plugado depois do container já estar
  rodando, o loop de reconexão (tenta a cada 5s) encontra ele na próxima
  varredura — não precisa reiniciar o container.
- **Conferir qual porta foi detectada**: `docker logs greenhouse` mostra
  `Conectado ao Arduino na porta /dev/ttyUSBx` (ou `Nenhum Arduino detectado...`
  se ainda não estiver plugado).
- **Forçar manualmente**: se por algum motivo a auto-detecção pegar o device
  errado (ex: outro periférico serial no mesmo servidor), defina
  `GREENHOUSE_SERIAL_PORT=/dev/ttyUSBx` no `.env` do Coolify.

## Por que o container roda `privileged: true` + monta `/dev` inteiro

O Docker isola devices por padrão — sem isso, o container não enxergaria o
Arduino mesmo com ele plugado no host. Essa é a opção mais simples e robusta
a hot-plug (qualquer novo device aparece automaticamente dentro do container).
O trade-off é um relaxamento do isolamento do container; aceitável aqui por
ser servidor pessoal de uso doméstico. Se quiser reforçar isolamento no
futuro, dá pra trocar por uma regra `udev` que cria um symlink fixo (ex:
`/dev/greenhouse-arduino`) e mapear só esse device específico no compose.

## Variáveis de ambiente (`.env` no Coolify)

Ver `.env.example`. Nunca commitar o `.env` real (já está no `.gitignore`).

| Variável | Uso |
|---|---|
| `PORT` | Porta HTTP interna (padrão `4022`) |
| `GREENHOUSE_USERNAME` / `GREENHOUSE_PASSWORD` | Login do dashboard |
| `GREENHOUSE_SESSION_SECRET` | Chave de sessão Flask (gerar com `openssl rand -hex 32`) |
| `GREENHOUSE_HIST_DIR` | Pasta onde o SQLite é gravado dentro do container (mapear para volume persistente) |
| `GREENHOUSE_SERIAL_PORT` | Override manual da porta serial (opcional) |

## Páginas

- **Painel** (`/`) — dois painéis separados (um por vaso), cada um com seu
  próprio gráfico; lado a lado no desktop, carrossel com swipe no mobile.
  Badge Online/Offline do Arduino (agora fixo na sidebar/rodapé mobile,
  visível em qualquer página), aviso de sensor sem variação por vaso.
- **Histórico** (`/historico`) — leituras brutas por período.
- **Relatórios** (`/relatorios`) — resumo agregado do período (total de
  regas, VPD/temp/umidade médios, regas por dia, distribuição por
  classificação de VPD) + botão de exportar CSV.
- **Ajustes** (`/configuracoes`) — escolher modo Vega/Flora (preset editável),
  ajustar limiares/intervalo/volume, e controles manuais de rega (com o
  mesmo limite de segurança da rega automática) — desativados na interface
  quando o Arduino está offline.

## Layout: sidebar (desktop) + menu flutuante (mobile) + PWA

Inspirado no padrão de navegação de outro app do usuário (riskTrade — sidebar
fixa com grupos no desktop, pílula flutuante no mobile), reimplementado aqui
em CSS/JS puro (o GreenHouse não usa React/build step):

- **Desktop (≥768px)**: sidebar fixa à esquerda (`templates/base.html` +
  `.sidebar` em `static/core.css`), com os 4 itens de navegação, badge de
  status e "Sair" no rodapé.
- **Mobile (<768px)**: `.sidebar` vira `display:none` e `.bottom-nav`
  aparece — uma pílula flutuante fixa embaixo, com os mesmos 4 itens (como
  são só 4 páginas, cabem direto, sem precisar de menu "mais"). É um
  elemento `position:fixed`, então o conteúdo passa por baixo dele ao rolar
  — comportamento esperado de nav flutuante, não é bug.
- Troca sidebar ↔ bottom-nav é **só CSS** (media query em 768px), sem JS.
- **Gestos**: `static/swipe.js` (`attachSwipe(el, {onSwipeLeft, onSwipeRight})`)
  implementa swipe horizontal via touch events puro (limiares: 70px de
  distância mínima, 700ms de duração máxima, dominância horizontal 1.3x —
  mesmos valores usados no riskTrade). Usado hoje só no carrossel de vasos
  do Painel; reaproveitável em outros lugares no futuro.
- **PWA**: `static/manifest.json` (servido com mimetype correto via rota
  dedicada `/manifest.json`) + `static/sw.js` (service worker mínimo, só
  pra satisfazer o requisito de instalabilidade — sem cache real ainda,
  intercepta só requests same-origin pra não interferir com o Plotly/
  Socket.IO carregados via CDN). Ícones em `static/icon-192.png` e
  `static/icon-512.png`. Corrigido o link de manifest que apontava pra um
  arquivo inexistente (`/site.webmanifest`, 404 desde o app original).

## O que mudou em relação à versão Windows original

- Removido o túnel Cloudflare (`cloudflared`) — Traefik + domínio próprio
  assumem a exposição pública.
- Porta serial fixa (`COM7`) → auto-detecção via `pyserial`.
- Login e session secret hardcoded → variáveis de ambiente.
- Porta HTTP fixa (5000) → configurável via `PORT`.
- Adicionado endpoint `/health` para o healthcheck do Docker/Traefik.
- SQLite passa a viver em volume persistente (`GREENHOUSE_HIST_DIR`) fora
  do container, sobrevivendo a rebuilds.
- Dois sketches quase idênticos (Vega/Flora) viraram um só, com o modo
  selecionável pela web (`arduino/GreenHouseApp/GreenHouseApp.ino`).
- Contador de regas 24h deixou de ser uma lista em memória (zerava a cada
  restart) e passou a ser persistido em `rega_eventos`.
- Novos controles manuais, configuração remota e relatórios (ver seção
  "Páginas" acima).

## Deploy

1. Copiar esta pasta para `/pendriver/greenhouse` no servidor.
2. Criar `/pendriver/greenhouse_hist` (histórico persistente).
3. Configurar o `.env` no Coolify com base em `.env.example`.
4. Adicionar/atualizar o bloco de `dynamic-proxy.yaml` no Traefik (file
   provider), se aplicável.
5. Subir o stack no Coolify.
6. Plugar o Arduino via USB no servidor e conferir `docker logs greenhouse`.
7. Testar `https://greenhouse.gvtserver.online` e `/health`.
