from flask import Flask, render_template, jsonify, request, redirect, url_for, session, Response, send_from_directory, stream_with_context
from flask_socketio import SocketIO, emit
from collections import deque
from datetime import datetime, timedelta
import serial
import serial.tools.list_ports
import threading
import time
import sqlite3
import os
import csv
import io
import requests

# -----------------------------------------
# Configuração da porta serial (auto-detecção)
# -----------------------------------------
BAUD_RATE = 9600

# VID:PID de chips USB-serial comuns em placas Arduino/clones
VID_PID_CONHECIDOS = {
    (0x1a86, 0x7523),  # CH340 (clones)
    (0x0403, 0x6001),  # FTDI
    (0x10c4, 0xea60),  # CP210x
    (0x2341, 0x0043),  # Arduino Uno (genuíno)
    (0x2341, 0x0001),  # Arduino Uno (genuíno, variante)
    (0x2341, 0x0010),  # Arduino Mega 2560
    (0x2341, 0x0042),  # Arduino Mega 2560 (variante)
}


def detectar_porta_arduino():
    """Retorna o path da porta serial do Arduino.

    Prioridade: GREENHOUSE_SERIAL_PORT (override manual) > match por VID:PID
    conhecido > primeira porta ttyUSB*/ttyACM* disponível.
    """
    override = os.environ.get("GREENHOUSE_SERIAL_PORT")
    if override:
        return override

    portas = list(serial.tools.list_ports.comports())

    for porta in portas:
        if porta.vid is not None and porta.pid is not None:
            if (porta.vid, porta.pid) in VID_PID_CONHECIDOS:
                return porta.device

    for porta in portas:
        if "ttyUSB" in porta.device or "ttyACM" in porta.device:
            return porta.device

    return None


def conectar_arduino():
    while True:
        porta = detectar_porta_arduino()
        if not porta:
            print("Nenhum Arduino detectado. Tentando novamente em 5 segundos...")
            time.sleep(5)
            continue

        try:
            ser = serial.Serial(porta, BAUD_RATE, timeout=1)
            time.sleep(2)
            print(f"Conectado ao Arduino na porta {porta}")
            emitir_status_arduino(True)
            return ser
        except serial.SerialException as e:
            print(f"Erro ao conectar ao Arduino em {porta}: {e}")
            print("Tentando reconexão em 5 segundos...")
            time.sleep(5)


# Não conecta aqui: conectar_arduino() bloqueia em loop até achar o device,
# o que impediria o Flask/SocketIO de subir e escutar a porta enquanto o
# Arduino não estivesse plugado. A conexão acontece dentro de leitura_serial(),
# em background, sem travar a subida do servidor web.
arduino = None

# Protege as escritas na serial: leitura_serial() roda numa thread e as
# rotas Flask (comandos manuais/config) rodam em threads de requisição.
arduino_lock = threading.Lock()

# Espelha do lado Python se o Arduino está "ocupado" (rega em andamento,
# teste sequencial de bombas, ou uma bomba manual ligada) — atualizado a
# partir das próprias mensagens que o Arduino já manda por engano
# (REGA_INICIO/FIM, TESTE_BOMBAS_OK, TESTE_BOMBA_OK). Sem isso, as rotas
# /api/comando/* só confirmam que ESCREVERAM o comando na serial, sem saber
# se o Arduino de fato aceitou — um clique duplo (ou dois vasos diferentes
# em sequência) retornava "sucesso" pros dois, mesmo o Arduino rejeitando
# silenciosamente o segundo (ERRO:REGA_EM_ANDAMENTO só aparece no log do
# servidor, nunca chega no navegador).
arduino_ocupado = {'regando': False, 'teste_bombas': False, 'bomba_manual': False}


def arduino_esta_ocupado():
    return any(arduino_ocupado.values())


def enviar_comando_arduino(linha):
    """Escreve uma linha de comando na serial do Arduino, com lock.

    Retorna True se conseguiu escrever, False se o Arduino não está conectado.
    """
    global arduino
    with arduino_lock:
        if not arduino or not arduino.is_open:
            return False
        try:
            arduino.write((linha + "\n").encode("utf-8"))
            return True
        except serial.SerialException:
            return False


def montar_comando_cfg(config):
    return (
        f"CFG:LOW={config['umidade_low']};"
        f"HIGH={config['umidade_high']};"
        f"INTERVALO={config['intervalo_min']};"
        f"VOLUME={config['volume_ml']};"
        f"MODO={config['modo'].upper()};"
        f"AUTO={1 if config.get('rega_automatica', True) else 0}"
    )


# Limite físico: 4 sensores (A0-A3) + 4 relés (pinos 8-11) cabeados hoje.
# Cada "slot" (0-3) é um par sensor+bomba que anda sempre junto — a
# topologia é só sobre quais slots pertencem a qual vaso.
NUM_SLOTS = 4

# Câmera do DVR (RTSP) restreamada via go2rtc (container "go2rtc" na mesma
# rede docker) — o Flask só faz proxy do MJPEG já convertido, protegido
# pelo mesmo login do resto do app (ver rota /camera/stream).
GO2RTC_URL = os.environ.get('GREENHOUSE_GO2RTC_URL', 'http://go2rtc:1984')
CAMERA_STREAM_NAME = os.environ.get('GREENHOUSE_CAMERA_STREAM', 'estufa')


def montar_comando_topo(vasos):
    """vasos: lista de {'vaso_index': int, 'sensores': [int, ...], 'bombas': [int, ...]}.
    Sensor e bomba de um mesmo slot físico podem pertencer a vasos
    diferentes (modo avançado) — por isso duas atribuições independentes,
    não mais uma única "slots"."""
    sensor_para_vaso = {}
    bomba_para_vaso = {}
    for v in vasos:
        for slot in v['sensores']:
            sensor_para_vaso[slot] = v['vaso_index']
        for slot in v['bombas']:
            bomba_para_vaso[slot] = v['vaso_index']

    partes = [f"N={len(vasos)}"]
    for slot in range(NUM_SLOTS):
        # Slot sem dono (sensor/bomba fora de uso) — 0 é o valor reservado
        # pro firmware entender "nenhum vaso" (não confundir com vaso 1).
        partes.append(f"S{slot}={sensor_para_vaso.get(slot, 0)}")
    for slot in range(NUM_SLOTS):
        partes.append(f"B{slot}={bomba_para_vaso.get(slot, 0)}")
    return "TOPO:" + ";".join(partes)


def parse_pares_chave_valor(corpo):
    """Parseia 'A=1;B=2;C=3' num dict {'A':'1','B':'2','C':'3'} — mesmo
    estilo usado pelo firmware nas linhas CFG:/TOPO:/DADOS:."""
    pares = {}
    for par in corpo.split(';'):
        if '=' in par:
            chave, valor = par.split('=', 1)
            pares[chave] = valor
    return pares

# -----------------------------------------
# Configuração do Banco de Dados SQLite
# -----------------------------------------
DB_DIR = os.environ.get("GREENHOUSE_HIST_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_NAME = os.path.join(DB_DIR, "monitor_leituras.db")


def init_db():
    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leituras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            vaso1_umidade INTEGER,
            vaso2_umidade INTEGER,
            ultima_rega INTEGER,
            proxima_rega INTEGER,
            temp REAL,
            umi REAL,
            vpd REAL,
            classificacao_vpd TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rega_eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vaso INTEGER,
            motivo TEXT,
            causa TEXT,
            inicio TIMESTAMP,
            fim TIMESTAMP,
            duracao_segundos INTEGER,
            umidade_final INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS configuracoes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            modo TEXT,
            umidade_low INTEGER,
            umidade_high INTEGER,
            intervalo_min INTEGER,
            volume_ml INTEGER,
            atualizado_em TIMESTAMP
        )
    """)

    # Migração aditiva: coluna nova numa tabela que já pode existir em
    # produção (nunca ALTER destrutivo — só adiciona se ainda não tiver).
    cursor.execute("PRAGMA table_info(configuracoes)")
    colunas_config = [row[1] for row in cursor.fetchall()]
    if 'rega_automatica' not in colunas_config:
        cursor.execute("ALTER TABLE configuracoes ADD COLUMN rega_automatica INTEGER DEFAULT 1")

    cursor.execute("""
        INSERT OR IGNORE INTO configuracoes
        (id, modo, umidade_low, umidade_high, intervalo_min, volume_ml, rega_automatica, atualizado_em)
        VALUES (1, 'vega', 50, 70, 120, 1000, 1, CURRENT_TIMESTAMP)
    """)

    # Topologia: quantos vasos existem e quais slots físicos (0-3) cada um
    # usa. Guardado como tabela (não singleton) pra suportar 1-4 vasos.
    # "sensores" e "bombas" são independentes (modo avançado permite um
    # slot ter o sensor num vaso e a bomba noutro) — "slots" é mantida só
    # por compatibilidade com bancos antigos, migrada uma vez abaixo.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS topologia_vasos (
            vaso_index INTEGER PRIMARY KEY,
            nome TEXT,
            slots TEXT,
            sensores TEXT,
            bombas TEXT
        )
    """)

    cursor.execute("PRAGMA table_info(topologia_vasos)")
    colunas_topo = [row[1] for row in cursor.fetchall()]
    if 'sensores' not in colunas_topo:
        cursor.execute("ALTER TABLE topologia_vasos ADD COLUMN sensores TEXT")
    if 'bombas' not in colunas_topo:
        cursor.execute("ALTER TABLE topologia_vasos ADD COLUMN bombas TEXT")

    cursor.execute("SELECT COUNT(*) FROM topologia_vasos")
    if cursor.fetchone()[0] == 0:
        # Default de fábrica: 2 vasos de 2 slots cada, sensor e bomba
        # juntos (modo simples), replica o comportamento original do app.
        cursor.execute(
            "INSERT INTO topologia_vasos (vaso_index, nome, slots, sensores, bombas) VALUES (1, 'Vaso 1', '0,1', '0,1', '0,1')"
        )
        cursor.execute(
            "INSERT INTO topologia_vasos (vaso_index, nome, slots, sensores, bombas) VALUES (2, 'Vaso 2', '2,3', '2,3', '2,3')"
        )
    else:
        # Migração única: bancos existentes têm "slots" mas não
        # "sensores"/"bombas" ainda — replica slots pros dois (equivalente
        # ao comportamento anterior: sensor e bomba sempre juntos).
        cursor.execute("SELECT vaso_index, slots FROM topologia_vasos WHERE sensores IS NULL")
        for vaso_index, slots_csv in cursor.fetchall():
            cursor.execute(
                "UPDATE topologia_vasos SET sensores = ?, bombas = ? WHERE vaso_index = ?",
                (slots_csv, slots_csv, vaso_index)
            )

    # Leituras por vaso — substitui as colunas fixas vaso1_umidade/
    # vaso2_umidade (que continuam existindo na tabela "leituras" só por
    # compatibilidade com dados antigos, mas não são mais preenchidas).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leituras_vasos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            leitura_id INTEGER,
            vaso_index INTEGER,
            umidade INTEGER
        )
    """)

    # Migração única: se já existem leituras antigas (com vaso1_umidade/
    # vaso2_umidade preenchidos) e ainda não migramos nada, copia pro
    # formato novo — assim o histórico anterior à topologia flexível não
    # se perde.
    cursor.execute("SELECT COUNT(*) FROM leituras_vasos")
    if cursor.fetchone()[0] == 0:
        cursor.execute("SELECT COUNT(*) FROM leituras WHERE vaso1_umidade IS NOT NULL")
        if cursor.fetchone()[0] > 0:
            cursor.execute("""
                INSERT INTO leituras_vasos (leitura_id, vaso_index, umidade)
                SELECT id, 1, vaso1_umidade FROM leituras WHERE vaso1_umidade IS NOT NULL
            """)
            cursor.execute("""
                INSERT INTO leituras_vasos (leitura_id, vaso_index, umidade)
                SELECT id, 2, vaso2_umidade FROM leituras WHERE vaso2_umidade IS NOT NULL
            """)
            print("Migração: histórico antigo (vaso1/vaso2) copiado para leituras_vasos.")

    conexao.commit()
    conexao.close()


init_db()

# Presets de referência para os modos de cultivo. Ponto de partida editável,
# não são valores agronômicos fixos — o usuário pode ajustar tudo depois
# pela página de Ajustes.
PRESETS_MODO = {
    'vega':  {'umidade_low': 50, 'umidade_high': 70, 'intervalo_min': 120, 'volume_ml': 1000},
    'flora': {'umidade_low': 40, 'umidade_high': 60, 'intervalo_min': 180, 'volume_ml': 1000},
}


def obter_configuracao():
    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()
    cursor.execute("""
        SELECT modo, umidade_low, umidade_high, intervalo_min, volume_ml, rega_automatica
        FROM configuracoes WHERE id = 1
    """)
    row = cursor.fetchone()
    conexao.close()
    if not row:
        preset = PRESETS_MODO['vega']
        return {'modo': 'vega', 'rega_automatica': True, **preset}
    return {
        'modo': row[0],
        'umidade_low': row[1],
        'umidade_high': row[2],
        'intervalo_min': row[3],
        'volume_ml': row[4],
        'rega_automatica': bool(row[5]) if row[5] is not None else True,
    }


def salvar_configuracao(modo, umidade_low, umidade_high, intervalo_min, volume_ml, rega_automatica):
    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()
    cursor.execute("""
        INSERT INTO configuracoes (id, modo, umidade_low, umidade_high, intervalo_min, volume_ml, rega_automatica, atualizado_em)
        VALUES (1, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            modo = excluded.modo,
            umidade_low = excluded.umidade_low,
            umidade_high = excluded.umidade_high,
            intervalo_min = excluded.intervalo_min,
            volume_ml = excluded.volume_ml,
            rega_automatica = excluded.rega_automatica,
            atualizado_em = excluded.atualizado_em
    """, (modo, umidade_low, umidade_high, intervalo_min, volume_ml, int(rega_automatica)))
    conexao.commit()
    conexao.close()


def _csv_para_lista(csv_texto):
    return [int(s) for s in (csv_texto or '').split(',') if s != '']


def obter_topologia():
    """Retorna a topologia atual: lista de {'vaso_index','nome','sensores','bombas'}.
    'slots' também é incluída (união de sensores+bombas) por conveniência
    pra código que só precisa saber "esse vaso usa esse slot de alguma forma"."""
    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()
    cursor.execute("SELECT vaso_index, nome, sensores, bombas FROM topologia_vasos ORDER BY vaso_index")
    rows = cursor.fetchall()
    conexao.close()

    vasos = []
    for vaso_index, nome, sensores_csv, bombas_csv in rows:
        sensores = _csv_para_lista(sensores_csv)
        bombas = _csv_para_lista(bombas_csv)
        vasos.append({
            'vaso_index': vaso_index,
            'nome': nome,
            'sensores': sensores,
            'bombas': bombas,
            'slots': sorted(set(sensores) | set(bombas)),
        })
    return vasos


def salvar_topologia(vasos):
    """vasos: lista de {'vaso_index','nome','sensores','bombas'} — substitui tudo."""
    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()
    cursor.execute("DELETE FROM topologia_vasos")
    for v in vasos:
        sensores_csv = ",".join(str(s) for s in v['sensores'])
        bombas_csv = ",".join(str(s) for s in v['bombas'])
        slots_csv = ",".join(str(s) for s in sorted(set(v['sensores']) | set(v['bombas'])))
        cursor.execute(
            "INSERT INTO topologia_vasos (vaso_index, nome, slots, sensores, bombas) VALUES (?, ?, ?, ?, ?)",
            (v['vaso_index'], v['nome'], slots_csv, sensores_csv, bombas_csv)
        )
    conexao.commit()
    conexao.close()


def salvar_leitura(leituras_vasos_dict, ultima, proxima, temp, umi, vpd, classificacao_vpd):
    """Insere uma nova linha de leitura (ambiente) + uma linha por vaso em
    leituras_vasos. Horário local, sem ajustes de fuso."""

    dt_local = datetime.now()
    data_hora = dt_local.strftime("%Y-%m-%d %H:%M:%S")

    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()

    cursor.execute("""
        INSERT INTO leituras
        (data_hora, ultima_rega, proxima_rega, temp, umi, vpd, classificacao_vpd)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (data_hora, ultima, proxima, temp, umi, vpd, classificacao_vpd))
    leitura_id = cursor.lastrowid

    for vaso_index, umidade in leituras_vasos_dict.items():
        cursor.execute("""
            INSERT INTO leituras_vasos (leitura_id, vaso_index, umidade)
            VALUES (?, ?, ?)
        """, (leitura_id, vaso_index, umidade))

    conexao.commit()
    conexao.close()


# Variável global para controlar o último horário de gravação
ultimaGravacaoBD = datetime.now() - timedelta(hours=1)  # Força a gravar na primeira leitura


def salvarLeituraSeNecessario(leituras_vasos_dict, ultima, proxima, temp, umi, vpd, classificacao_vpd):
    """
    Salva no BD apenas se tiver passado >= 1 hora desde a última gravação.
    """
    global ultimaGravacaoBD
    agora = datetime.now()
    tempoDecorrido = agora - ultimaGravacaoBD
    if tempoDecorrido >= timedelta(hours=1):
        salvar_leitura(leituras_vasos_dict, ultima, proxima, temp, umi, vpd, classificacao_vpd)
        ultimaGravacaoBD = agora
        print("Leitura salva no BD (intervalo >= 1h).")

# -----------------------------------------
# Flask + SocketIO
# -----------------------------------------
app = Flask(__name__)


def static_url(filename):
    """url_for('static', ...) com um '?v=<mtime>' — assim, toda vez que um
    CSS/JS/imagem muda, o navegador (e o Cloudflare) buscam a versão nova
    em vez de reaproveitar um arquivo em cache com o mesmo nome antigo."""
    caminho = os.path.join(app.static_folder, filename)
    try:
        versao = int(os.path.getmtime(caminho))
    except OSError:
        versao = 0
    return url_for('static', filename=filename) + f'?v={versao}'


app.jinja_env.globals['static_url'] = static_url

app.secret_key = os.environ.get("GREENHOUSE_SESSION_SECRET") or os.urandom(24)
if not os.environ.get("GREENHOUSE_SESSION_SECRET"):
    print("AVISO: GREENHOUSE_SESSION_SECRET não definido. Sessões serão invalidadas a cada restart.")

GREENHOUSE_USERNAME = os.environ.get("GREENHOUSE_USERNAME", "admin")
GREENHOUSE_PASSWORD = os.environ.get("GREENHOUSE_PASSWORD", "changeme")
if GREENHOUSE_PASSWORD == "changeme":
    print("AVISO: usando GREENHOUSE_PASSWORD padrão (changeme). Defina uma senha forte em produção.")

# async_mode="threading" é necessário: leitura_serial() roda numa
# threading.Thread comum (não greenlet). Se o eventlet estiver instalado,
# o Flask-SocketIO auto-seleciona async_mode="eventlet" e os emits vindos
# dessa thread real são descartados silenciosamente (não cruzam pro hub
# do eventlet sem eventlet.monkey_patch()). "threading" evita isso de vez.
socketio = SocketIO(app, async_mode="threading")

# Estado de conexão do Arduino, refletido no badge "Online/Offline" do front-end
arduino_conectado = False


def emitir_status_arduino(conectado):
    global arduino_conectado
    if conectado != arduino_conectado:
        arduino_conectado = conectado
        socketio.emit('status_arduino', {'conectado': arduino_conectado})


@socketio.on('connect')
def ao_conectar_cliente():
    # Envia o estado atual assim que um cliente abre o dashboard,
    # sem precisar esperar a próxima mudança de status.
    emit('status_arduino', {'conectado': arduino_conectado})


# Estruturas de dados
tempo = deque(maxlen=50)
vasos_umidade = {}  # {vaso_index: deque(maxlen=50)} — reconstruído a partir da topologia atual


def preparar_deques_vasos(vasos):
    """(Re)inicializa as deques de histórico em memória conforme a
    topologia atual — chamado no boot e sempre que a topologia é salva."""
    global vasos_umidade
    vasos_umidade = {v['vaso_index']: deque(maxlen=50) for v in vasos}


preparar_deques_vasos(obter_topologia())

# Tempo mínimo SEM NENHUMA variação pra suspeitar que um sensor está
# travado/desconectado (só vira um aviso na tela, sem alerta externo).
# Baseado em tempo real decorrido, não em quantidade de leituras — a
# umidade do solo realmente não muda em poucos segundos, então contar só
# "últimas N leituras" (janela curta, usada pro gráfico) gerava aviso falso
# o tempo todo mesmo com o sensor lendo certo.
LIMIAR_SENSOR_TRAVADO = timedelta(minutes=30)

# {vaso_index: {'valor': int, 'desde': datetime}} — quando o valor lido
# muda, reinicia a contagem; se ficar LIMIAR_SENSOR_TRAVADO sem mudar, é suspeito.
ultima_variacao_vaso = {}


def sensor_parece_travado(vaso_index, valor_atual):
    agora = datetime.now()
    anterior = ultima_variacao_vaso.get(vaso_index)
    if anterior is None or anterior['valor'] != valor_atual:
        ultima_variacao_vaso[vaso_index] = {'valor': valor_atual, 'desde': agora}
        return False
    return (agora - anterior['desde']) >= LIMIAR_SENSOR_TRAVADO


def contar_regas_24h():
    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM rega_eventos WHERE fim >= datetime('now', '-24 hours')
    """)
    total = cursor.fetchone()[0]
    conexao.close()
    return total


def registrar_rega_evento(vaso, motivo, causa, duracao_segundos, umidade_final):
    agora = datetime.now()
    inicio = agora - timedelta(seconds=duracao_segundos)
    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()
    cursor.execute("""
        INSERT INTO rega_eventos (vaso, motivo, causa, inicio, fim, duracao_segundos, umidade_final)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (vaso, motivo, causa,
          inicio.strftime("%Y-%m-%d %H:%M:%S"),
          agora.strftime("%Y-%m-%d %H:%M:%S"),
          duracao_segundos, umidade_final))
    conexao.commit()
    conexao.close()


# -----------------------------------------
# Leitura assíncrona da Serial
# -----------------------------------------
def leitura_serial():
    global arduino
    while True:
        if not arduino or not arduino.is_open:
            print("Arduino desconectado. Tentando reconexão...")
            emitir_status_arduino(False)
            arduino = conectar_arduino()
            # Assim que (re)conecta, empurra a config e a topologia atuais
            # do banco pro Arduino — cobre power-loss do Arduino, redeploy
            # do container etc.
            config_atual = obter_configuracao()
            enviar_comando_arduino(montar_comando_cfg(config_atual))
            topologia_atual = obter_topologia()
            enviar_comando_arduino(montar_comando_topo(topologia_atual))

        try:
            linha = arduino.readline().decode("utf-8").strip()
            if not linha:
                continue

            if linha.startswith("DADOS:"):
                pares = parse_pares_chave_valor(linha[len("DADOS:"):])
                try:
                    num_vasos_telemetria = int(pares.get("VASOS", 0))
                    # U{v} == -1 é a sentinela do firmware pra "sem sensor
                    # confiável nesse vaso agora" (desconectado ou instável
                    # demais) — nunca um dado real de umidade.
                    leituras_vasos_atual = {}
                    sem_sensor_atual = {}
                    for v in range(1, num_vasos_telemetria + 1):
                        valor = int(pares[f"U{v}"]) if f"U{v}" in pares else -1
                        if valor >= 0:
                            leituras_vasos_atual[v] = valor
                            sem_sensor_atual[v] = False
                        else:
                            sem_sensor_atual[v] = True

                    ultima_rega = int(pares.get("ULTIMA", 0))
                    proxima_rega = int(pares.get("PROXIMA", 0))
                    rega_automatica_ativa = pares.get("AUTO", "1") == "1"
                    temp_txt = pares.get("TEMP", "Err")
                    umi_txt = pares.get("UMI", "Err")
                    vpd_txt = pares.get("VPD", "Err")
                    temp = float(temp_txt) if temp_txt != "Err" else float("nan")
                    umi = float(umi_txt) if umi_txt != "Err" else float("nan")
                    vpd = float(vpd_txt) if vpd_txt != "Err" else float("nan")
                    classificacao_vpd = pares.get("VPDC", "Indef")
                except ValueError:
                    continue  # linha malformada, ignora e segue pra próxima

                # Atualiza deques para gráfico em tempo real. Sempre gravamos
                # uma entrada por vaso a cada ciclo (None quando sem sensor),
                # pra manter o deque de cada vaso do mesmo tamanho que
                # "tempo" — senão o eixo X e a série de um vaso que ficou sem
                # sensor por um tempo saem desalinhados no gráfico.
                t = 0 if len(tempo) == 0 else tempo[-1] + 1
                tempo.append(t)
                for v in range(1, num_vasos_telemetria + 1):
                    if v not in vasos_umidade:
                        vasos_umidade[v] = deque(maxlen=50)
                    vasos_umidade[v].append(leituras_vasos_atual.get(v))
                tempo_em_horas = [x / 3600.0 for x in tempo]

                # Só avalia "sem variação" pros vasos com leitura válida
                # agora (sem_sensor já é um aviso próprio, tratado à parte).
                sensor_suspeito = {
                    str(v): sensor_parece_travado(v, valor)
                    for v, valor in leituras_vasos_atual.items()
                }

                # Emite para front-end
                socketio.emit('update_data', {
                    'tempo': tempo_em_horas,
                    'vasos': {str(k): list(v) for k, v in vasos_umidade.items()},
                    'ultima': ultima_rega,
                    'proxima': proxima_rega,
                    'rega_automatica': rega_automatica_ativa,
                    'temp': temp,
                    'umi': umi,
                    'vpd': vpd,
                    'classificacao_vpd': classificacao_vpd,
                    'total_regas_24h': contar_regas_24h(),
                    'sensor_suspeito': sensor_suspeito,
                    'sem_sensor': {str(k): v for k, v in sem_sensor_atual.items()},
                })

                # Salva no BD apenas se tiver passado >= 1h
                salvarLeituraSeNecessario(
                    leituras_vasos_atual,
                    ultima_rega,
                    proxima_rega,
                    temp,
                    umi,
                    vpd,
                    classificacao_vpd
                )

            elif linha.startswith("TOPO_ERRO"):
                print(f"Arduino recusou topologia: {linha}")

            elif linha.startswith("REGA_INICIO:"):
                partes = linha.split(":")
                if len(partes) == 3:
                    arduino_ocupado['regando'] = True
                    socketio.emit('rega_estado', {
                        'vaso': int(partes[1]),
                        'em_andamento': True,
                        'motivo': partes[2],
                    })

            elif linha.startswith("REGA_FIM:"):
                partes = linha.split(":")
                if len(partes) == 6:
                    arduino_ocupado['regando'] = False
                    vaso = int(partes[1])
                    motivo = partes[2].lower()
                    duracao_segundos = int(partes[3])
                    umidade_final = int(partes[4])
                    causa = partes[5].lower()

                    registrar_rega_evento(vaso, motivo, causa, duracao_segundos, umidade_final)

                    socketio.emit('rega_estado', {
                        'vaso': vaso,
                        'em_andamento': False,
                        'motivo': motivo,
                    })
                    socketio.emit('update_regas', {
                        'total_regas_24h': contar_regas_24h()
                    })

            elif linha.startswith("CFG_ERRO"):
                print(f"Arduino recusou configuração: {linha}")

            elif linha == "TESTE_BOMBAS_OK:INICIADO":
                arduino_ocupado['teste_bombas'] = True

            elif linha in ("TESTE_BOMBAS_OK:CONCLUIDO", "TESTE_BOMBAS_TIMEOUT"):
                arduino_ocupado['teste_bombas'] = False

            elif linha.startswith("TESTE_BOMBA_OK:LIGADA"):
                arduino_ocupado['bomba_manual'] = True

            elif linha in ("TESTE_BOMBA_OK:DESLIGADA", "TESTE_BOMBA_TIMEOUT"):
                arduino_ocupado['bomba_manual'] = False

            elif linha.startswith("ERRO:"):
                print(f"Arduino retornou erro de comando: {linha}")

        except serial.SerialException as e:
            print(f"Erro de conexão serial: {e}")
            if arduino and arduino.is_open:
                try:
                    arduino.close()
                except Exception:
                    pass
            arduino = None
            emitir_status_arduino(False)
            # Desconectou — o Arduino reinicia do zero na próxima conexão
            # (regaEmAndamento/testeBombasAtivo/bombaManualAtiva sempre
            # nascem "livres" no boot dele), então o espelho Python também
            # não pode continuar achando que algo ainda está em andamento.
            arduino_ocupado['regando'] = False
            arduino_ocupado['teste_bombas'] = False
            arduino_ocupado['bomba_manual'] = False
            time.sleep(5)
        except Exception as e:
            print(f"Erro ao ler serial: {e}")
            time.sleep(2)

# -----------------------------------------
# Rotas Flask
# -----------------------------------------
@app.route('/health')
def health():
    return jsonify(status="ok"), 200


@app.route('/camera/stream')
def camera_stream():
    """Proxy do vídeo MJPEG da câmera (via go2rtc), atrás do mesmo login
    do dashboard — o go2rtc não fica exposto direto na internet."""
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    try:
        upstream = requests.get(
            f"{GO2RTC_URL}/api/stream.mjpeg",
            params={'src': CAMERA_STREAM_NAME},
            stream=True,
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        return jsonify(erro=f"Câmera indisponível: {e}"), 503

    return Response(
        stream_with_context(upstream.iter_content(chunk_size=4096)),
        content_type=upstream.headers.get('Content-Type', 'multipart/x-mixed-replace'),
    )


@app.route('/sw.js')
def service_worker():
    # Servido na raiz (não em /static/) para o escopo do service worker
    # cobrir o site inteiro, não só a pasta static.
    return send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')


@app.route('/manifest.json')
def manifest():
    # Rota dedicada (em vez de servir via /static/) só pra garantir o
    # mimetype correto — o handler estático genérico do Flask serve .json
    # como application/json, que a maioria dos navegadores aceita, mas o
    # tipo correto é application/manifest+json.
    return send_from_directory(app.static_folder, 'manifest.json', mimetype='application/manifest+json')


@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    total_regas_24h = contar_regas_24h()
    vasos = obter_topologia()
    return render_template('index.html', total_regas_24h=total_regas_24h, vasos=vasos, active_page='painel')


def obter_periodo_solicitado():
    """Lê 'start'/'end' da querystring, com default dos últimos 7 dias."""
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    if not start_date or not end_date:
        hoje = datetime.now()
        start_date = (hoje - timedelta(days=7)).strftime('%Y-%m-%d')
        end_date = hoje.strftime('%Y-%m-%d')
    return start_date, end_date


def buscar_leituras_periodo(start_date, end_date):
    """Retorna as leituras do período, cada uma com um dict 'vasos'
    {vaso_index: umidade} — quantos vasos existirem, não mais fixo em 2."""
    conexao = sqlite3.connect(DB_NAME)
    cursor  = conexao.cursor()
    cursor.execute("""
        SELECT id, data_hora, ultima_rega, proxima_rega, temp, umi, vpd, classificacao_vpd
        FROM leituras
        WHERE date(data_hora) BETWEEN date(?) AND date(?)
        ORDER BY data_hora DESC
    """, (start_date, end_date))
    rows = cursor.fetchall()

    ids = [r[0] for r in rows]
    vasos_por_leitura = {}
    if ids:
        marcadores = ",".join("?" * len(ids))
        cursor.execute(f"""
            SELECT leitura_id, vaso_index, umidade
            FROM leituras_vasos
            WHERE leitura_id IN ({marcadores})
        """, ids)
        for leitura_id, vaso_index, umidade in cursor.fetchall():
            vasos_por_leitura.setdefault(leitura_id, {})[vaso_index] = umidade

    conexao.close()

    leituras = []
    for leitura_id, data_str, ultima_rega, proxima_rega, temp, umi, vpd, classificacao_vpd in rows:
        try:
            dt = datetime.strptime(data_str, "%Y-%m-%d %H:%M:%S")
            data_iso = dt.isoformat(sep='T')
            data_para_tabela = dt.strftime("%b %d, %Y %H:%M")
        except Exception as e:
            print(f"Erro ao processar a data: {e}")
            data_iso = data_str
            data_para_tabela = data_str

        leituras.append({
            'data_plotly':      data_iso,
            'data_hora':        data_para_tabela,
            'vasos':            vasos_por_leitura.get(leitura_id, {}),
            'ultima_rega':      ultima_rega,
            'proxima_rega':     proxima_rega,
            'temp':             temp,
            'umi':              umi,
            'vpd':              vpd,
            'classificacao_vpd': classificacao_vpd,
        })
    return leituras


@app.route('/historico', methods=['GET'])
def historico():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    start_date, end_date = obter_periodo_solicitado()
    leituras = buscar_leituras_periodo(start_date, end_date)
    vasos = obter_topologia()

    return render_template('historico.html',
                           leituras=leituras,
                           vasos=vasos,
                           start_date=start_date,
                           end_date=end_date,
                           active_page='historico')


@app.route('/historico/exportar.csv')
def exportar_historico_csv():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    start_date, end_date = obter_periodo_solicitado()
    leituras = buscar_leituras_periodo(start_date, end_date)
    vasos = obter_topologia()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    cabecalho = ['data_hora'] + [f"{v['nome']}_umidade" for v in vasos] + [
        'ultima_rega_min', 'proxima_rega_min', 'temp_c', 'umidade_ar', 'vpd', 'classificacao_vpd'
    ]
    writer.writerow(cabecalho)
    for l in leituras:
        linha = [l['data_hora']] + [l['vasos'].get(v['vaso_index'], '') for v in vasos] + [
            l['ultima_rega'], l['proxima_rega'], l['temp'], l['umi'],
            l['vpd'], l['classificacao_vpd']
        ]
        writer.writerow(linha)

    nome_arquivo = f"greenhouse_historico_{start_date}_a_{end_date}.csv"
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{nome_arquivo}"'}
    )


@app.route('/relatorios')
def relatorios():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    start_date, end_date = obter_periodo_solicitado()

    conexao = sqlite3.connect(DB_NAME)
    cursor = conexao.cursor()

    cursor.execute("""
        SELECT COUNT(*), AVG(vpd), AVG(temp), AVG(umi)
        FROM leituras
        WHERE date(data_hora) BETWEEN date(?) AND date(?)
    """, (start_date, end_date))
    total_leituras, vpd_medio, temp_media, umi_media = cursor.fetchone()

    cursor.execute("""
        SELECT lv.vaso_index, AVG(lv.umidade)
        FROM leituras_vasos lv
        JOIN leituras l ON l.id = lv.leitura_id
        WHERE date(l.data_hora) BETWEEN date(?) AND date(?)
        GROUP BY lv.vaso_index
        ORDER BY lv.vaso_index
    """, (start_date, end_date))
    medias_por_vaso = {row[0]: round(row[1], 1) for row in cursor.fetchall()}

    cursor.execute("""
        SELECT COUNT(*), COALESCE(SUM(duracao_segundos), 0)
        FROM rega_eventos
        WHERE date(fim) BETWEEN date(?) AND date(?)
    """, (start_date, end_date))
    total_regas, duracao_total_regas = cursor.fetchone()

    cursor.execute("""
        SELECT date(fim), COUNT(*)
        FROM rega_eventos
        WHERE date(fim) BETWEEN date(?) AND date(?)
        GROUP BY date(fim)
        ORDER BY date(fim)
    """, (start_date, end_date))
    regas_por_dia = [{'dia': r[0], 'total': r[1]} for r in cursor.fetchall()]

    cursor.execute("""
        SELECT classificacao_vpd, COUNT(*)
        FROM leituras
        WHERE date(data_hora) BETWEEN date(?) AND date(?)
        GROUP BY classificacao_vpd
    """, (start_date, end_date))
    distribuicao_vpd = {r[0]: r[1] for r in cursor.fetchall()}

    conexao.close()

    resumo = {
        'total_leituras': total_leituras or 0,
        'vpd_medio': round(vpd_medio, 2) if vpd_medio is not None else None,
        'temp_media': round(temp_media, 1) if temp_media is not None else None,
        'umi_media': round(umi_media, 1) if umi_media is not None else None,
        'total_regas': total_regas or 0,
        'duracao_total_regas_min': round((duracao_total_regas or 0) / 60, 1),
    }

    vasos = obter_topologia()
    for v in vasos:
        v['umidade_media'] = medias_por_vaso.get(v['vaso_index'])

    return render_template('relatorios.html',
                           resumo=resumo,
                           vasos=vasos,
                           regas_por_dia=regas_por_dia,
                           distribuicao_vpd=distribuicao_vpd,
                           start_date=start_date,
                           end_date=end_date,
                           active_page='relatorios')


@app.route('/configuracoes', methods=['GET', 'POST'])
def configuracoes():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    erro = None
    sucesso = None

    if request.method == 'POST':
        try:
            modo = request.form.get('modo', 'vega')
            umidade_low = int(request.form.get('umidade_low'))
            umidade_high = int(request.form.get('umidade_high'))
            intervalo_min = int(request.form.get('intervalo_min'))
            volume_ml = int(request.form.get('volume_ml'))
            rega_automatica = request.form.get('rega_automatica') == 'on'

            if modo not in PRESETS_MODO:
                raise ValueError("Modo inválido.")
            if not (0 <= umidade_low < umidade_high <= 100):
                raise ValueError("Umidade mínima precisa ser menor que o limite de corte, ambas entre 0 e 100.")
            if intervalo_min <= 0 or volume_ml <= 0:
                raise ValueError("Intervalo e volume precisam ser maiores que zero.")

            salvar_configuracao(modo, umidade_low, umidade_high, intervalo_min, volume_ml, rega_automatica)

            config_atual = obter_configuracao()
            if enviar_comando_arduino(montar_comando_cfg(config_atual)):
                sucesso = "Configuração salva e enviada ao Arduino."
            else:
                sucesso = "Configuração salva. Será enviada ao Arduino assim que ele reconectar (está offline agora)."
        except (TypeError, ValueError) as e:
            erro = str(e) or "Valores inválidos."

    config = obter_configuracao()
    topologia = obter_topologia()
    sensor_para_vaso = {slot: v['vaso_index'] for v in topologia for slot in v['sensores']}
    bomba_para_vaso = {slot: v['vaso_index'] for v in topologia for slot in v['bombas']}
    # Se algum slot já tem sensor e bomba em vasos diferentes, a topologia
    # atual só é representável no modo avançado — abre a página já nele,
    # senão o usuário veria os dois desalinhados sem entender por quê.
    modo_avancado_ativo = any(
        sensor_para_vaso.get(slot, 0) != bomba_para_vaso.get(slot, 0)
        for slot in range(NUM_SLOTS)
    )
    return render_template('configuracoes.html',
                           config=config,
                           presets=PRESETS_MODO,
                           topologia=topologia,
                           sensor_para_vaso=sensor_para_vaso,
                           bomba_para_vaso=bomba_para_vaso,
                           modo_avancado_ativo=modo_avancado_ativo,
                           num_slots=NUM_SLOTS,
                           arduino_conectado=arduino_conectado,
                           erro=erro,
                           sucesso=sucesso,
                           active_page='configuracoes')


@app.route('/api/topologia', methods=['POST'])
def api_salvar_topologia():
    if not session.get('logged_in'):
        return jsonify(ok=False, erro="Não autenticado."), 401

    dados = request.get_json(silent=True) or {}
    vasos_entrada = dados.get('vasos')
    if not isinstance(vasos_entrada, list) or not (1 <= len(vasos_entrada) <= NUM_SLOTS):
        return jsonify(ok=False, erro=f"Precisa de 1 a {NUM_SLOTS} vasos."), 400

    def validar_lista_slots(lista, rotulo, vistos):
        if not isinstance(lista, list):
            return None, f"{rotulo} inválido."
        for s in lista:
            if not isinstance(s, int) or not (0 <= s < NUM_SLOTS):
                return None, f"{rotulo} inválido: {s}."
            if s in vistos:
                return None, f"{rotulo} {s} está atribuído a mais de um vaso."
            vistos.add(s)
        return lista, None

    sensores_vistos = set()
    bombas_vistas = set()
    vasos_normalizados = []
    for i, v in enumerate(vasos_entrada, start=1):
        sensores, erro_s = validar_lista_slots(v.get('sensores'), "Sensor", sensores_vistos)
        if erro_s:
            return jsonify(ok=False, erro=erro_s), 400
        bombas, erro_b = validar_lista_slots(v.get('bombas'), "Bomba", bombas_vistas)
        if erro_b:
            return jsonify(ok=False, erro=erro_b), 400
        if len(sensores) == 0 and len(bombas) == 0:
            return jsonify(ok=False, erro=f"Vaso {i} precisa de pelo menos 1 sensor ou 1 bomba."), 400
        vasos_normalizados.append({
            'vaso_index': i,
            'nome': (v.get('nome') or f"Vaso {i}").strip() or f"Vaso {i}",
            'sensores': sensores,
            'bombas': bombas,
        })

    salvar_topologia(vasos_normalizados)
    preparar_deques_vasos(vasos_normalizados)

    if enviar_comando_arduino(montar_comando_topo(vasos_normalizados)):
        return jsonify(ok=True, mensagem="Topologia salva e enviada ao Arduino.")
    return jsonify(ok=True, mensagem="Topologia salva. Será enviada ao Arduino assim que ele reconectar (está offline agora).")


@app.route('/api/comando/regar', methods=['POST'])
def api_comando_regar():
    if not session.get('logged_in'):
        return jsonify(ok=False, erro="Não autenticado."), 401

    dados = request.get_json(silent=True) or {}
    vaso = dados.get('vaso')
    num_vasos_atual = len(obter_topologia())
    if not isinstance(vaso, int) or not (1 <= vaso <= num_vasos_atual):
        return jsonify(ok=False, erro="Vaso inválido."), 400

    # Mesma checagem que o firmware já faz (regaEmAndamento || testeBombasAtivo
    # || bombaManualAtiva >= 0) — mas feita ANTES de mandar o comando, não só
    # depois. Sem isso, um clique duplo (ou dois vasos em sequência) recebia
    # "ok:true" dos dois, mesmo o Arduino rejeitando o segundo silenciosamente
    # (o "ERRO:REGA_EM_ANDAMENTO" nunca chegava até o navegador).
    if arduino_esta_ocupado():
        return jsonify(ok=False, erro="Já tem uma rega ou teste de bomba em andamento."), 409

    if enviar_comando_arduino(f"REGAR:{vaso}"):
        return jsonify(ok=True)
    return jsonify(ok=False, erro="Arduino não está conectado."), 409


@app.route('/api/comando/parar', methods=['POST'])
def api_comando_parar():
    if not session.get('logged_in'):
        return jsonify(ok=False, erro="Não autenticado."), 401

    if enviar_comando_arduino("PARAR"):
        return jsonify(ok=True)
    return jsonify(ok=False, erro="Arduino não está conectado."), 409


@app.route('/api/comando/teste_bombas', methods=['POST'])
def api_comando_teste_bombas():
    """Liga cada bomba por 10s, uma de cada vez (nunca duas juntas), sem
    passar pela lógica normal de rega — só pra testar fiação/funcionamento.
    "ligar" inicia o ciclo (~40s no total); "desligar" interrompe na hora,
    mesmo mecanismo do "Parar rega"."""
    if not session.get('logged_in'):
        return jsonify(ok=False, erro="Não autenticado."), 401

    dados = request.get_json(silent=True) or {}
    ligar = bool(dados.get('ligar'))

    if ligar:
        # "desligar" sempre pode ser enviado (é a forma de interromper) —
        # só "ligar" precisa checar conflito antes.
        if arduino_ocupado['regando'] or arduino_ocupado['bomba_manual']:
            return jsonify(ok=False, erro="Já tem uma rega ou bomba manual em andamento."), 409
        if arduino_ocupado['teste_bombas']:
            return jsonify(ok=False, erro="O teste de bombas já está em andamento."), 409

    comando = "TESTE_BOMBAS:LIGAR" if ligar else "TESTE_BOMBAS:DESLIGAR"

    if enviar_comando_arduino(comando):
        return jsonify(ok=True)
    return jsonify(ok=False, erro="Arduino não está conectado."), 409


@app.route('/api/comando/teste_bomba', methods=['POST'])
def api_comando_teste_bomba():
    """Liga/desliga UMA bomba específica (1-4) na hora, sem passar pela
    lógica normal de rega — pra escolher e testar uma bomba manualmente,
    sem esperar o ciclo automático do "Testar bombas". Só uma fica ligada
    por vez (o firmware desliga a anterior sozinho)."""
    if not session.get('logged_in'):
        return jsonify(ok=False, erro="Não autenticado."), 401

    dados = request.get_json(silent=True) or {}
    ligar = bool(dados.get('ligar'))

    if ligar:
        slot = dados.get('slot')
        if not isinstance(slot, int) or not (1 <= slot <= NUM_SLOTS):
            return jsonify(ok=False, erro=f"Bomba inválida (1 a {NUM_SLOTS})."), 400
        # "desligar" sempre pode ser enviado — só "ligar" precisa checar
        # conflito antes (mesma regra do firmware).
        if arduino_ocupado['regando'] or arduino_ocupado['teste_bombas']:
            return jsonify(ok=False, erro="Já tem uma rega ou teste de bombas em andamento."), 409
        comando = f"TESTE_BOMBA:LIGAR:{slot}"
    else:
        comando = "TESTE_BOMBA:DESLIGAR"

    if enviar_comando_arduino(comando):
        return jsonify(ok=True)
    return jsonify(ok=False, erro="Arduino não está conectado."), 409


@app.route('/api/comando/reiniciar', methods=['POST'])
def api_comando_reiniciar():
    if not session.get('logged_in'):
        return jsonify(ok=False, erro="Não autenticado."), 401

    if arduino_esta_ocupado():
        return jsonify(ok=False, erro="Pare a rega/teste de bomba em andamento antes de reiniciar."), 409

    if not enviar_comando_arduino("REINICIAR"):
        return jsonify(ok=False, erro="Arduino não está conectado."), 409

    def reenviar_config_apos_reset():
        # O reset por watchdog reinicia só o microcontrolador — a porta USB
        # serial nunca fecha, então leitura_serial() não detecta "desconexão"
        # e não reenvia CFG:/TOPO: sozinho. Sem isso, o Arduino voltaria
        # sempre com os valores padrão do firmware (ex: MODO=VEGA), mesmo
        # com Floração salva no banco.
        time.sleep(3)
        enviar_comando_arduino(montar_comando_cfg(obter_configuracao()))
        enviar_comando_arduino(montar_comando_topo(obter_topologia()))

    threading.Thread(target=reenviar_config_apos_reset, daemon=True).start()
    return jsonify(ok=True)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form.get('username')
        pwd  = request.form.get('password')

        if user == GREENHOUSE_USERNAME and pwd == GREENHOUSE_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            return render_template('login.html', erro="Usuário ou senha inválidos!")
    else:
        return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


# Inicializa thread para leitura da Serial
threading.Thread(target=leitura_serial, daemon=True).start()

# -----------------------------------------
# Execução
# -----------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
