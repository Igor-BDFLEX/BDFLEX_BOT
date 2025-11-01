# -*- coding: utf-8 -*-
# os_bot.py - Bot para Gestão de Ordens de Serviço (OS) via Telegram

# --- Imports e Setup ---

import logging
import json
import time
import os
import re
import uuid
import io # Para manipulação de arquivos em memória
from datetime import datetime, timedelta
import asyncio
import aiohttp

# --- Imports para PDF (necessitam de instalação via pip) ---
try:
    import fitz # PyMuPDF
    import pandas as pd
    PDF_PROCESSOR_AVAILABLE = True
except ImportError:
    logging.warning("Módulos 'fitz' (PyMuPDF) e/ou 'pandas' não encontrados. O recurso Enviar PDF não funcionará.")
    PDF_PROCESSOR_AVAILABLE = False
    class MockDataFrame:
        def __init__(self, *args, **kwargs): pass
    pd = MockDataFrame()

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app
from google.cloud.firestore_v1.base_query import FieldFilter # Import para filtros complexos

# Python Telegram Bot
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    JobQueue,
)
from telegram.constants import ParseMode, ChatAction
from dotenv import load_dotenv

load_dotenv()

# --- Configuração ---

# Habilita o logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Token e Configurações de Webhook (Ajuste conforme o seu ambiente)
TOKEN = os.getenv("TELEGRAM_TOKEN")
# Para desenvolvimento local, use `bot.py` com run_polling.
# Para produção/servidores, use `os_bot.py` (ou `bot_webhook.py`) com run_webhook.
# Se estiver usando este arquivo em um ambiente de hosting, certifique-se de que as variáveis de ambiente
# WEBHOOK_URL e PORT estejam definidas.
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

# Imagem do Menu (Recomendado: subir para o Telegram e usar o file_id ou usar uma URL pública)
# Substitua pela URL da sua imagem. Como a do Google Drive não é pública, usei um placeholder.
MENU_IMAGE_URL = "https://placehold.co/800x250/000000/FFFFFF/png?text=GESTAO+DE+ORDENS+DE+SERVICO"
# Se você já tiver o file_id da imagem no Telegram, use:
# MENU_IMAGE_FILE_ID = "SEU_FILE_ID_AQUI"

# Estados para o ConversationHandler
MENU, INCLUSAO_NUMERO, INCLUSAO_PREFIXO, INCLUSAO_CHAMADO, INCLUSAO_DISTANCIA, INCLUSAO_DESCRICAO, INCLUSAO_CRITICIDADE, INCLUSAO_TIPO, INCLUSAO_PRAZO, INCLUSAO_SITUACAO, INCLUSAO_TECNICO, INCLUSAO_AGENDAMENTO, INCLUSAO_RESUMO, ATUALIZACAO_NUMERO, ATUALIZACAO_CAMPO_SELECIONADO, DELETAR_NUMERO, DELETAR_CONFIRMACAO, LISTAR_TIPO, LISTAR_SITUACAO, LEMBRETE_NUMERO, LEMBRETE_DATA_HORA, LEMBRETE_MENSAGEM, RECEBER_PDF_FLOW, INCLUSAO_TECNICO_PROMPT = range(24)

# Campos de uma O.S. (usados para resumo e atualização)
OS_FIELDS = [
    ("Número", "numero_os"),
    ("Chamado", "chamado"),
    ("Prefixo/Dependência", "prefixo_dependencia"),
    ("Distância", "distancia"),
    ("Descrição", "descricao"),
    ("Criticidade", "criticidade"),
    ("Tipo", "tipo"),
    ("Prazo", "prazo"),
    ("Situação", "situacao"),
    ("Técnico", "tecnico"),
    ("Agendamento", "agendamento"),
    ("Lembrete", "lembrete_manual"), # Campo opcional para lembrete manual
]

# --- Firebase Init ---

try:
    # Tenta carregar as credenciais de um arquivo (recomendado para segurança)
    if os.path.exists("app.json"):
        cred = credentials.Certificate("app.json")
    # Tenta carregar as credenciais da variável de ambiente (comum em ambientes de hosting)
    elif os.getenv("FIREBASE_CREDENTIALS_JSON"):
        cred_json = json.loads(os.getenv("FIREBASE_CREDENTIALS_JSON"))
        cred = credentials.Certificate(cred_json)
    else:
        logger.error("Credenciais do Firebase não encontradas.")
        exit()

    initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase inicializado com sucesso.")
except Exception as e:
    logger.error(f"Erro ao inicializar Firebase: {e}")
    exit()

# --- Funções de Utilitários de Dados ---

def get_os_ref():
    """Retorna a referência da coleção de Ordens de Serviço (OS)."""
    return db.collection("ordens_servico")

def get_lembretes_ref():
    """Retorna a referência da coleção de Lembretes."""
    return db.collection("lembretes_os")

def format_os_summary(os_data: dict, include_key: bool = False) -> str:
    """Formata os dados de uma O.S. para exibição no resumo."""
    texto = "📋 *RESUMO DA O.S.* \n\n"
    for label, key in OS_FIELDS:
        # Usa um placeholder para campos vazios e formata a exibição
        value = os_data.get(key, "N/A")
        if key == "lembrete_manual" and value:
             value = f"{value}"
        elif key == "lembrete_manual" and not value:
            value = "Nenhum definido"

        texto += f"*{label}*: {value}\n"

    if include_key and 'doc_id' in os_data:
        texto += f"\n_ID Interno: {os_data['doc_id']}_"

    return texto

def create_os_buttons(os_data: dict, action_prefix: str) -> InlineKeyboardMarkup:
    """Cria botões inline para atualização a partir do resumo da OS."""
    keyboard = []
    for label, key in OS_FIELDS:
        if key not in ['numero_os', 'agendamento', 'lembrete_manual']: # Não permite alterar o número ou agendamento/lembrete diretamente
            keyboard.append([
                InlineKeyboardButton(f"✏️ {label}: {os_data.get(key, 'N/A')}", callback_data=f"{action_prefix}:{key}")
            ])

    # Botão para voltar para a etapa anterior (confirmação ou menu)
    keyboard.append([
        InlineKeyboardButton("↩️ Voltar ao Resumo", callback_data="atualizacao_finalizada")
    ])

    return InlineKeyboardMarkup(keyboard)

def format_os_list_item(os_data: dict) -> str:
    """Formata uma linha para a lista de O.S."""
    return (
        f"OS *{os_data.get('numero_os', 'N/A')}* | Tipo: *{os_data.get('tipo', 'N/A')}* | Situação: *{os_data.get('situacao', 'N/A')}* \n"
        f"  Prefixo: {os_data.get('prefixo_dependencia', 'N/A')} | Prazo: {os_data.get('prazo', 'N/A')}\n"
        f"  Técnico: {os_data.get('tecnico', 'N/A')}\n"
    )

async def check_duplicate_os(update: Update, context: ContextTypes.DEFAULT_TYPE, numero_os: str) -> tuple:
    """Verifica se uma OS já existe no banco de dados."""
    os_query = get_os_ref().where(filter=FieldFilter("numero_os", "==", numero_os)).limit(1)
    docs = await os_query.get()
    
    if docs:
        os_data = docs[0].to_dict()
        os_data['doc_id'] = docs[0].id
        
        texto = f"⚠️ O.S. de número *{numero_os}* já está cadastrada! \n\n"
        texto += format_os_summary(os_data)
        texto += "\nO que deseja fazer?"

        keyboard = [
            [InlineKeyboardButton("🔄 Atualizar Informações", callback_data="incluir_atualizar")],
            [InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        context.user_data['os_data'] = os_data # Salva os dados para possível atualização
        return True, INCLUSAO_RESUMO # Retorna True e o estado de Resumo para a decisão
    
    return False, None

# --- Funções de Manipulação de PDF ---

# Adaptação das funções de utilitários do PDF do usuário
def limpar_valor_bruto(v):
    if v is None: return None
    v = v.strip()
    if re.fullmatch(r'[\(\-\s]*\)?', v) or v in ('()', '-', '—', ''): return None
    return v

def tratar_texto(valor, linha_unica=False):
    if not valor: return None
    valor = valor.replace('\r', '\n').strip()
    if '\n' in valor: partes = re.split(r'\n+', valor)
    else: partes = re.split(r'(?<=[.;:])\s+', valor)
    partes = [re.sub(r'\s+', ' ', p).strip() for p in partes if p and p.strip()]
    if not partes: return None
    if linha_unica: return " ".join(partes)
    return "\n\n".join(partes)

def extrair_dados_pdf_in_memory(pdf_bytes: bytes) -> dict:
    """Extrai dados de um PDF em memória (bytes)."""
    if not PDF_PROCESSOR_AVAILABLE:
        return {"error": "Módulos PyMuPDF/Pandas não disponíveis. Contate o administrador."}

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        texto = "".join(pagina.get_text("text") for pagina in doc)
    except Exception as e:
        logger.error(f"Erro ao abrir/ler PDF: {e}")
        return {"error": f"Erro ao processar PDF: {e}"}

    dados = {}
    padroes = {
        "Número da O.S.": r"Número da O\.S\.\s*([\d]+)",
        "Chamado": r"Chamado:\s*([A-Z0-9\-]+)",
        "Prefixo/Dependência": r"Dependência:\s*(.+?)(?=\s*Endereço:)",
        "Distância": r"Distância:\s*(.+?)(?=\s*Ambiente:)",
        "Descrição": r"Descrição:\s*(.+?)(?=\s*(?:Sinistro:|Criticidade:|Tipo:|$))",
        "Criticidade": r"Criticidade:\s*(.+?)(?=\s*(?:Tipo:|Prazo:|Solicitante:|$))",
        "Tipo": r"Tipo:\s*(.+?)(?=\s*(?:Prazo:|Solicitante:|Matrícula:|$))",
        "Prazo": r"Prazo:\s*(.+?)(?=\s*(?:Solicitante:|Matrícula:|Telefone:|$))"
    }
    
    # Mapeamento para as chaves do Firestore
    chave_map = {
        "Número da O.S.": "numero_os",
        "Chamado": "chamado",
        "Prefixo/Dependência": "prefixo_dependencia",
        "Distância": "distancia",
        "Descrição": "descricao",
        "Criticidade": "criticidade",
        "Tipo": "tipo",
        "Prazo": "prazo",
    }

    for campo, regex in padroes.items():
        m = re.search(regex, texto, re.DOTALL)
        valor = None
        if m:
            valor = m.group(1).strip()
            valor = limpar_valor_bruto(valor)

        if campo == "Número da O.S." and valor is not None:
            try: valor = str(int(valor)) # Garante string numérica
            except ValueError: valor = None

        if valor is not None:
            if campo.lower() == "descrição":
                valor = tratar_texto(valor, linha_unica=True)
            elif campo.lower() in ["criticidade", "tipo", "prazo"]:
                valor = tratar_texto(valor)

        if valor is not None and isinstance(valor, str) and not valor.strip():
            valor = None
        
        if valor is not None and campo in chave_map:
            dados[chave_map[campo]] = valor.strip()

    return dados

# --- Handlers de Comando ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a conversa e exibe o menu principal."""
    logger.info(f"Usuário {update.effective_user.id} iniciou o bot.")
    if update.message:
        await menu_principal(update, context)
        return MENU
    return MENU

async def menu_principal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exibe o menu principal com a imagem e botões."""
    
    # Limpa dados temporários
    context.user_data.clear()

    keyboard = [
        [
            InlineKeyboardButton("➕ Incluir O.S.", callback_data="incluir"),
            InlineKeyboardButton("🔄 Atualizar O.S.", callback_data="atualizar")
        ],
        [
            InlineKeyboardButton("🗑️ Deletar O.S.", callback_data="deletar"),
            InlineKeyboardButton("📋 Listar O.S.", callback_data="listar")
        ],
        [
            InlineKeyboardButton("📄 Enviar PDF", callback_data="enviar_pdf"),
            InlineKeyboardButton("⏰ Lembrete", callback_data="lembrete_menu")
        ],
        [InlineKeyboardButton("❓ Ajuda Geral", callback_data="ajuda")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Verifica se deve editar a mensagem anterior ou enviar uma nova
    if update.callback_query:
        try:
            # Envia a foto com a legenda no lugar de editar o texto para garantir que a foto sempre esteja presente
            await update.callback_query.message.delete()
            await update.callback_query.message.reply_photo(
                photo=MENU_IMAGE_URL,
                caption="*Menu Principal* \nSelecione uma opção abaixo:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.warning(f"Erro ao tentar editar/enviar menu com foto: {e}. Enviando apenas texto.")
            await update.callback_query.message.reply_text(
                "*Menu Principal* \nSelecione uma opção abaixo:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
    else: # Mensagem inicial /start
        await update.message.reply_photo(
            photo=MENU_IMAGE_URL,
            caption="*Menu Principal* \nSelecione uma opção abaixo:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    return MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela o fluxo atual e volta ao menu principal."""
    if update.message:
        await update.message.reply_text("❌ Fluxo cancelado. Voltando ao menu principal...")
    elif update.callback_query:
        await update.callback_query.message.edit_text("❌ Fluxo cancelado. Voltando ao menu principal...")
    
    return await menu_principal(update, context)

# --- Fluxo de Inclusão de O.S. ---

async def prompt_os_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de inclusão/atualização e solicita o número da O.S.."""
    
    context.user_data['flow'] = update.callback_query.data # 'incluir' ou 'atualizar'
    
    # Inicializa o dicionário de dados da OS
    context.user_data['os_data'] = {
        'situação': 'Pendente', # Valor padrão
        'tecnico': 'Não Definido', # Valor padrão
        'lembrete_manual': 'Nenhum definido',
        'chat_id': update.effective_chat.id # Para notificação
    }

    texto = "Digite o *Número da O.S.* (apenas números):"
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_caption(caption=texto, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)
    
    return INCLUSAO_NUMERO

async def receive_os_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o número da O.S. e verifica duplicidade."""
    numero_os = update.message.text.strip()

    if not re.match(r'^\d+$', numero_os):
        await update.message.reply_text("Por favor, digite um número de O.S. válido (apenas números).")
        return INCLUSAO_NUMERO

    context.user_data['os_data']['numero_os'] = numero_os

    # 1. Verifica duplicidade (apenas para o fluxo de inclusão)
    if context.user_data['flow'] == 'incluir':
        is_duplicate, next_state = await check_duplicate_os(update, context, numero_os)
        if is_duplicate:
            return next_state # Vai para o resumo para decidir atualizar ou cancelar
    
    # 2. Se não for duplicata ou se for fluxo de atualização, avança para o próximo campo
    return await prompt_os_prefixo(update, context)

async def prompt_os_prefixo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o Prefixo/Dependência."""
    # Se veio do check_duplicate_os (em caso de atualização a partir de inclusão)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text("Digite o *Prefixo/Dependência*:", parse_mode=ParseMode.MARKDOWN)
        return INCLUSAO_PREFIXO
    
    # Se veio do receive_os_number
    await update.message.reply_text("Digite o *Prefixo/Dependência*:", parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_PREFIXO

async def receive_os_prefixo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Prefixo/Dependência e solicita o Chamado."""
    context.user_data['os_data']['prefixo_dependencia'] = update.message.text.strip()
    await update.message.reply_text("Digite o *Número do Chamado*:", parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_CHAMADO

async def receive_os_chamado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Chamado e solicita a Distância."""
    context.user_data['os_data']['chamado'] = update.message.text.strip()
    await update.message.reply_text("Digite a *Distância* em Km (apenas números):", parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_DISTANCIA

async def receive_os_distancia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Distância e solicita a Descrição."""
    distancia = update.message.text.strip()
    if not re.match(r'^[\d\s,.]+$', distancia):
        await update.message.reply_text("Por favor, digite a distância em Km válida (apenas números).")
        return INCLUSAO_DISTANCIA

    context.user_data['os_data']['distancia'] = distancia
    await update.message.reply_text("Digite a *Descrição* do serviço:", parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_DESCRICAO

async def receive_os_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Descrição e solicita a Criticidade."""
    context.user_data['os_data']['descricao'] = update.message.text.strip()

    texto = "Selecione a *Criticidade*:"
    keyboard = [
        [
            InlineKeyboardButton("🚨 Emergencial", callback_data="criticidade:Emergencial"),
            InlineKeyboardButton("⚠️ Urgente", callback_data="criticidade:Urgente")
        ],
        [
            InlineKeyboardButton("🟢 Normal", callback_data="criticidade:Normal"),
            InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_CRITICIDADE

async def receive_os_criticidade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Criticidade e solicita o Tipo."""
    query = update.callback_query
    await query.answer()
    
    criticidade = query.data.split(":")[1]
    context.user_data['os_data']['criticidade'] = criticidade

    texto = "Selecione o *Tipo* de serviço:"
    keyboard = [
        [
            InlineKeyboardButton("🔧 Corretiva", callback_data="tipo:Corretiva"),
            InlineKeyboardButton("🧹 Preventiva", callback_data="tipo:Preventiva")
        ],
        [InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_TIPO

async def receive_os_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Tipo e solicita o Prazo."""
    query = update.callback_query
    await query.answer()
    
    tipo = query.data.split(":")[1]
    context.user_data['os_data']['tipo'] = tipo

    texto = "Digite o *Prazo* para conclusão (ex: DD/MM/AAAA):"
    await query.edit_message_text(texto, parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_PRAZO

async def receive_os_prazo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o Prazo e solicita a Situação."""
    prazo = update.message.text.strip()
    # Tenta validar o formato de data
    try:
        datetime.strptime(prazo, '%d/%m/%Y')
        context.user_data['os_data']['prazo'] = prazo
    except ValueError:
        await update.message.reply_text("Formato de prazo inválido. Por favor, use DD/MM/AAAA (ex: 25/10/2025):")
        return INCLUSAO_PRAZO

    texto = "Selecione a *Situação* atual:"
    keyboard = [
        [
            InlineKeyboardButton("Pendente", callback_data="situacao:Pendente"),
            InlineKeyboardButton("Aguardando Agendamento", callback_data="situacao:Aguardando agendamento")
        ],
        [
            InlineKeyboardButton("Agendado", callback_data="situacao:agendado"),
            InlineKeyboardButton("Concluído", callback_data="situacao:Concluído")
        ],
        [InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_SITUACAO

async def receive_os_situacao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a Situação e solicita a definição do Técnico."""
    query = update.callback_query
    await query.answer()
    
    situacao = query.data.split(":")[1]
    context.user_data['os_data']['situacao'] = situacao

    texto = "O *Técnico* responsável está definido?"
    keyboard = [
        [
            InlineKeyboardButton("👷 DEFINIDO", callback_data="tecnico_definido"),
            InlineKeyboardButton("🚫 NÃO DEFINIDO", callback_data="tecnico_nao_definido")
        ],
        [InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_TECNICO

async def prompt_os_tecnico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Decisão sobre o Técnico (definido ou não)."""
    query = update.callback_query
    await query.answer()

    if query.data == "tecnico_nao_definido":
        context.user_data['os_data']['tecnico'] = "Não Definido"
        # Pula a pergunta do nome do técnico e vai direto para o agendamento
        return await prompt_os_agendamento(update, context)
    
    # Se for "DEFINIDO", pede o nome
    texto = "Qual é o nome do *Técnico* responsável?"
    await query.edit_message_text(texto, parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_TECNICO_PROMPT

async def receive_os_tecnico_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o nome do Técnico e solicita o Agendamento."""
    context.user_data['os_data']['tecnico'] = update.message.text.strip()
    
    return await prompt_os_agendamento(update, context)

async def prompt_os_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a data de Agendamento."""
    
    # Se veio de um callback, edita a mensagem
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text("Digite a data do *Agendamento* (ex: DD/MM/AAAA) ou 'N/A' se não agendado:", parse_mode=ParseMode.MARKDOWN)
        return INCLUSAO_AGENDAMENTO
    
    # Se veio de um MessageHandler (após receber o nome do técnico)
    await update.message.reply_text("Digite a data do *Agendamento* (ex: DD/MM/AAAA) ou 'N/A' se não agendado:", parse_mode=ParseMode.MARKDOWN)
    return INCLUSAO_AGENDAMENTO

async def receive_os_agendamento(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data de Agendamento e mostra o resumo final."""
    agendamento = update.message.text.strip()

    if agendamento.upper() == 'N/A':
        context.user_data['os_data']['agendamento'] = 'Não Agendado'
    else:
        try:
            datetime.strptime(agendamento, '%d/%m/%Y')
            context.user_data['os_data']['agendamento'] = agendamento
        except ValueError:
            await update.message.reply_text("Formato de agendamento inválido. Por favor, use DD/MM/AAAA ou 'N/A':")
            return INCLUSAO_AGENDAMENTO

    return await show_final_summary(update, context)

async def show_final_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exibe o resumo da O.S. com opções de confirmação, edição ou cancelamento."""
    os_data = context.user_data.get('os_data', {})
    
    texto = format_os_summary(os_data)
    texto += "\n\nConfirma a inclusão/atualização da O.S.?"

    keyboard = [
        [InlineKeyboardButton("✅ Confirmar Inclusão", callback_data="confirmar_inclusao")],
        [InlineKeyboardButton("✏️ Editar Informações", callback_data="editar_inclusao")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    return INCLUSAO_RESUMO

async def finalize_os_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva ou atualiza a O.S. no Firebase."""
    os_data = context.user_data['os_data']
    doc_id = os_data.pop('doc_id', None) # Remove o doc_id para não salvar como campo

    try:
        if doc_id:
            # Atualiza
            await get_os_ref().document(doc_id).update(os_data)
            msg = f"✅ O.S. *{os_data['numero_os']}* atualizada com sucesso!"
        else:
            # Salva
            await get_os_ref().add(os_data)
            msg = f"✅ O.S. *{os_data['numero_os']}* incluída com sucesso!"
    except Exception as e:
        logger.error(f"Erro ao salvar/atualizar OS: {e}")
        msg = "❌ Ocorreu um erro ao salvar a O.S. no banco de dados. Tente novamente."

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    return await menu_principal(update, context)

# --- Fluxo de Edição a partir do Resumo Final (Incluir/Atualizar) ---

async def start_edit_from_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a edição de campos a partir do resumo final."""
    query = update.callback_query
    await query.answer()
    os_data = context.user_data.get('os_data', {})

    if not os_data:
        await query.edit_message_text("❌ Dados da O.S. não encontrados para edição.", parse_mode=ParseMode.MARKDOWN)
        return await menu_principal(update, context)

    texto = "Selecione a informação que deseja *ATUALIZAR*:\n\n"
    texto += format_os_summary(os_data)

    # Cria o teclado com botões para cada campo editável
    reply_markup = create_os_buttons(os_data, action_prefix="update_field")

    await query.edit_message_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return ATUALIZACAO_CAMPO_SELECIONADO

async def prompt_update_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o novo valor para o campo selecionado."""
    query = update.callback_query
    await query.answer()

    # update_field:prefixo_dependencia
    field_key = query.data.split(":")[1]
    
    # Mapeia a chave para o nome amigável e estado de retorno
    field_label = next(label for label, key in OS_FIELDS if key == field_key)
    
    context.user_data['current_field_key'] = field_key

    # Se for um campo de botão (Criticidade, Tipo, Situação), usa botões novamente
    if field_key == 'criticidade':
        texto = f"Selecione a nova *{field_label}*:"
        keyboard = [
            [InlineKeyboardButton("🚨 Emergencial", callback_data="update_value:Emergencial"), InlineKeyboardButton("⚠️ Urgente", callback_data="update_value:Urgente")],
            [InlineKeyboardButton("🟢 Normal", callback_data="update_value:Normal")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return ATUALIZACAO_CAMPO_SELECIONADO
    
    elif field_key == 'tipo':
        texto = f"Selecione o novo *{field_label}*:"
        keyboard = [
            [InlineKeyboardButton("🔧 Corretiva", callback_data="update_value:Corretiva"), InlineKeyboardButton("🧹 Preventiva", callback_data="update_value:Preventiva")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return ATUALIZACAO_CAMPO_SELECIONADO
        
    elif field_key == 'situacao':
        texto = f"Selecione a nova *{field_label}*:"
        keyboard = [
            [InlineKeyboardButton("Pendente", callback_data="update_value:Pendente"), InlineKeyboardButton("Aguardando agendamento", callback_data="update_value:Aguardando agendamento")],
            [InlineKeyboardButton("Agendado", callback_data="update_value:agendado"), InlineKeyboardButton("Concluído", callback_data="update_value:Concluído")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return ATUALIZACAO_CAMPO_SELECIONADO
        
    elif field_key == 'tecnico':
        texto = "Qual é o novo nome do *Técnico* responsável?"
        await query.edit_message_text(texto, parse_mode=ParseMode.MARKDOWN)
        return ATUALIZACAO_CAMPO_SELECIONADO # Espera texto

    # Campos de texto (Padrão)
    texto = f"Digite o novo valor para *{field_label}* (Atual valor: {context.user_data['os_data'].get(field_key, 'N/A')}):"
    await query.edit_message_text(texto, parse_mode=ParseMode.MARKDOWN)
    return ATUALIZACAO_CAMPO_SELECIONADO

async def receive_update_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o novo valor (texto ou callback) e retorna ao resumo para mais edições/confirmação."""
    os_data = context.user_data['os_data']
    field_key = context.user_data.get('current_field_key')

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        # Valor de um botão
        new_value = query.data.split(":")[1]
        
        os_data[field_key] = new_value
        await query.edit_message_text(f"✅ {field_key} atualizado para *{new_value}*. Voltando ao resumo...", parse_mode=ParseMode.MARKDOWN)
    
    elif update.message:
        # Valor digitado (texto)
        new_value = update.message.text.strip()

        # Validação simples para Prazo/Agendamento
        if field_key in ['prazo', 'agendamento'] and new_value.upper() != 'N/A':
            try:
                datetime.strptime(new_value, '%d/%m/%Y')
            except ValueError:
                await update.message.reply_text(f"Formato de data inválido para {field_key}. Por favor, use DD/MM/AAAA ou 'N/A':")
                return ATUALIZACAO_CAMPO_SELECIONADO

        os_data[field_key] = new_value
        await update.message.reply_text(f"✅ {field_key} atualizado para *{new_value}*.", parse_mode=ParseMode.MARKDOWN)
        
    else:
        # Se não for nem callback nem mensagem, algo deu errado
        return await start_edit_from_summary(update, context)

    context.user_data['os_data'] = os_data # Salva o dado atualizado

    # Volta para o menu de edição/resumo
    return await start_edit_from_summary(update, context)

async def return_to_summary_or_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Retorna ao resumo final após a edição de um campo."""
    query = update.callback_query
    await query.answer()
    
    # Se o usuário clicou no botão "Voltar ao Resumo" no menu de edição
    if query.data == "atualizacao_finalizada":
        return await show_final_summary(update, context)

    # Se a atualização foi feita no resumo inicial (duplicidade)
    if context.user_data.get('flow') == 'incluir_atualizar':
        # Volta ao resumo para decidir se confirma ou edita mais
        return await show_final_summary(update, context)

    # Se a atualização foi feita no fluxo de 'Atualizar O.S.'
    # O doc_id é obrigatório neste caso
    doc_id = context.user_data['os_data'].get('doc_id')
    if not doc_id:
        await query.edit_message_text("❌ Erro interno: ID do documento não encontrado.", parse_mode=ParseMode.MARKDOWN)
        return await menu_principal(update, context)

    # Salva no banco de dados após a edição
    os_data = context.user_data['os_data'].copy()
    os_data.pop('doc_id') # Remove o id antes de salvar
    
    try:
        await get_os_ref().document(doc_id).update(os_data)
        await query.edit_message_text(f"✅ O.S. *{os_data['numero_os']}* atualizada com sucesso! Voltando ao menu...", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Erro ao salvar atualização de OS: {e}")
        await query.edit_message_text("❌ Erro ao salvar a atualização.", parse_mode=ParseMode.MARKDOWN)

    return await menu_principal(update, context)

# --- Fluxo de Atualização de O.S. (Start) ---

async def prompt_update_os_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o número da O.S. a ser atualizada."""
    context.user_data['flow'] = 'atualizar_os'
    texto = "Digite o *Número da O.S.* que deseja *atualizar*:"

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_caption(caption=texto, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)

    return ATUALIZACAO_NUMERO

async def receive_update_os_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o número da O.S. e exibe o resumo para edição."""
    numero_os = update.message.text.strip()
    
    if not re.match(r'^\d+$', numero_os):
        await update.message.reply_text("Por favor, digite um número de O.S. válido (apenas números).")
        return ATUALIZACAO_NUMERO
    
    os_query = get_os_ref().where(filter=FieldFilter("numero_os", "==", numero_os)).limit(1)
    docs = await os_query.get()

    if not docs:
        await update.message.reply_text(f"❌ O.S. de número *{numero_os}* não encontrada. Tente novamente ou volte ao menu.", parse_mode=ParseMode.MARKDOWN)
        return ATUALIZACAO_NUMERO
    
    os_data = docs[0].to_dict()
    os_data['doc_id'] = docs[0].id
    context.user_data['os_data'] = os_data

    texto = "📋 *RESUMO DA O.S.* (Selecione um campo para editar):\n\n"
    texto += format_os_summary(os_data)

    # Cria o teclado de edição, prefixo 'update_field_external'
    reply_markup = create_os_buttons(os_data, action_prefix="update_field_external")
    
    # Adiciona botão para confirmar e finalizar
    keyboard_rows = reply_markup.inline_keyboard
    keyboard_rows.append([
        InlineKeyboardButton("✅ Concluir Atualização", callback_data="atualizacao_finalizada"),
        InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")
    ])
    reply_markup = InlineKeyboardMarkup(keyboard_rows)

    await update.message.reply_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    
    # O estado é o mesmo do fluxo de inclusão (ATUALIZACAO_CAMPO_SELECIONADO) para usar a mesma lógica
    return ATUALIZACAO_CAMPO_SELECIONADO

# --- Fluxo de Deletar O.S. ---

async def prompt_delete_os_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o número da O.S. a ser deletada."""
    context.user_data.clear()
    texto = "Digite o *Número da O.S.* que deseja *deletar*:"

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_caption(caption=texto, parse_mode=ParseMode.MARKDOWN)
    
    return DELETAR_NUMERO

async def receive_delete_os_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o número da O.S., exibe o resumo e pede confirmação."""
    numero_os = update.message.text.strip()
    
    if not re.match(r'^\d+$', numero_os):
        await update.message.reply_text("Por favor, digite um número de O.S. válido (apenas números).")
        return DELETAR_NUMERO

    os_query = get_os_ref().where(filter=FieldFilter("numero_os", "==", numero_os)).limit(1)
    docs = await os_query.get()

    if not docs:
        await update.message.reply_text(f"❌ O.S. de número *{numero_os}* não encontrada. Tente novamente ou volte ao menu.", parse_mode=ParseMode.MARKDOWN)
        return DELETAR_NUMERO
    
    os_data = docs[0].to_dict()
    os_data['doc_id'] = docs[0].id
    context.user_data['os_data'] = os_data

    texto = format_os_summary(os_data)
    texto += "\n\n*ATENÇÃO:* Confirma a *EXCLUSÃO* desta O.S.?"

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmar Exclusão", callback_data="confirmar_exclusao"),
            InlineKeyboardButton("❌ Cancelar", callback_data="menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return DELETAR_CONFIRMACAO

async def confirm_delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Deleta a O.S. no Firebase após confirmação."""
    query = update.callback_query
    await query.answer()
    
    os_data = context.user_data.get('os_data')
    doc_id = os_data.get('doc_id')

    if not doc_id:
        await query.edit_message_text("❌ Erro interno: ID do documento não encontrado.", parse_mode=ParseMode.MARKDOWN)
        return await menu_principal(update, context)

    try:
        await get_os_ref().document(doc_id).delete()
        # Remove também quaisquer lembretes associados
        lembretes_query = get_lembretes_ref().where(filter=FieldFilter("numero_os", "==", os_data['numero_os']))
        lembretes_docs = await lembretes_query.get()
        for doc in lembretes_docs:
            await doc.reference.delete()
        
        msg = f"✅ O.S. *{os_data['numero_os']}* e lembretes associados *deletados* com sucesso!"
    except Exception as e:
        logger.error(f"Erro ao deletar OS: {e}")
        msg = "❌ Ocorreu um erro ao deletar a O.S. no banco de dados. Tente novamente."

    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    return await menu_principal(update, context)

# --- Fluxo de Listar O.S. ---

async def prompt_list_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o tipo de O.S. para listagem."""
    context.user_data.clear()
    texto = "Selecione o *Tipo* de O.S. que deseja listar:"
    keyboard = [
        [
            InlineKeyboardButton("🔧 Corretiva", callback_data="list_type:Corretiva"),
            InlineKeyboardButton("🧹 Preventiva", callback_data="list_type:Preventiva")
        ],
        [
            InlineKeyboardButton("Todas", callback_data="list_type:Todas"),
            InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_caption(caption=texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    return LISTAR_TIPO

async def prompt_list_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a situação da O.S. para listagem."""
    query = update.callback_query
    await query.answer()

    # list_type:Corretiva
    tipo_os = query.data.split(":")[1]
    context.user_data['list_tipo'] = tipo_os

    texto = f"Tipo selecionado: *{tipo_os}*. Agora, selecione a *Situação*:"
    keyboard = [
        [
            InlineKeyboardButton("Pendente", callback_data="list_status:Pendente"),
            InlineKeyboardButton("Aguardando Agendamento", callback_data="list_status:Aguardando agendamento")
        ],
        [
            InlineKeyboardButton("Agendado", callback_data="list_status:agendado"),
            InlineKeyboardButton("Concluído", callback_data="list_status:Concluído")
        ],
        [
            InlineKeyboardButton("Todas", callback_data="list_status:Todas"),
            InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return LISTAR_SITUACAO

async def execute_list_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executa a consulta e exibe a lista de O.S.."""
    query = update.callback_query
    await query.answer()

    # list_status:Pendente
    situacao_os = query.data.split(":")[1]
    tipo_os = context.user_data.get('list_tipo')

    if not tipo_os:
        await query.edit_message_text("❌ Erro interno: Tipo de O.S. não definido.", parse_mode=ParseMode.MARKDOWN)
        return await menu_principal(update, context)

    # Constrói a query
    os_query = get_os_ref()
    
    if tipo_os != 'Todas':
        os_query = os_query.where(filter=FieldFilter("tipo", "==", tipo_os))
    
    if situacao_os != 'Todas':
        os_query = os_query.where(filter=FieldFilter("situacao", "==", situacao_os))
    
    await query.edit_message_text(f"⏳ Buscando O.S. de Tipo: *{tipo_os}* e Situação: *{situacao_os}*...", parse_mode=ParseMode.MARKDOWN)
    
    docs = await os_query.get()

    if not docs:
        texto_resultado = f"✅ Nenhuma O.S. encontrada com os critérios: Tipo *{tipo_os}*, Situação *{situacao_os}*."
    else:
        lista_os = [doc.to_dict() for doc in docs]
        
        # Opcional: ordenar por Prazo
        try:
            lista_os.sort(key=lambda x: datetime.strptime(x.get('prazo', '01/01/3000'), '%d/%m/%Y'))
        except:
            pass # Ignora se houver erro de formatação de data
        
        texto_resultado = f"📋 *LISTA DE O.S.* (Total: {len(lista_os)}):\n\n"
        for os_data in lista_os:
            texto_resultado += format_os_list_item(os_data)
        
        texto_resultado += "\n\n✅ Lista completa exibida."

    keyboard = [[InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Divide a mensagem se for muito longa
    MAX_MESSAGE_LENGTH = 4096
    if len(texto_resultado) > MAX_MESSAGE_LENGTH:
        parts = []
        current_part = ""
        for line in texto_resultado.split('\n'):
            if len(current_part) + len(line) + 1 > MAX_MESSAGE_LENGTH:
                parts.append(current_part)
                current_part = line
            else:
                current_part += "\n" + line
        parts.append(current_part)
        
        for i, part in enumerate(parts):
            if i == 0:
                 await query.edit_message_text(part, parse_mode=ParseMode.MARKDOWN)
            else:
                 await query.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)
        
        # Envia a última mensagem com o botão
        await query.message.reply_text("Fim da lista.", reply_markup=reply_markup)

    else:
        await query.edit_message_text(texto_resultado, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    return MENU

# --- Fluxo de Enviar PDF ---

async def prompt_receive_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita que o usuário envie o arquivo PDF."""
    context.user_data.clear()
    texto = "Por favor, envie o arquivo *PDF* com as informações da O.S. para que eu possa extrair os dados."
    
    if not PDF_PROCESSOR_AVAILABLE:
         texto = "❌ O recurso de *Enviar PDF* não está disponível, pois as bibliotecas PyMuPDF e/ou Pandas não foram instaladas. Instale-as para usar este recurso."
         keyboard = [[InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")]]
         reply_markup = InlineKeyboardMarkup(keyboard)
         if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.edit_caption(caption=texto, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
         return MENU

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_caption(caption=texto, parse_mode=ParseMode.MARKDOWN)
    
    return RECEBER_PDF_FLOW

async def receive_pdf_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o arquivo PDF, processa e salva/atualiza no Firebase."""
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    
    if not update.message.document or not update.message.document.mime_type == 'application/pdf':
        await update.message.reply_text("Por favor, envie um arquivo válido no formato *PDF*.", parse_mode=ParseMode.MARKDOWN)
        return RECEBER_PDF_FLOW

    pdf_file = await update.message.document.get_file()
    pdf_bytes = io.BytesIO()
    await pdf_file.download_to_memory(pdf_bytes)
    pdf_bytes.seek(0)
    
    await update.message.reply_text("⏳ Recebi o PDF. Aguarde enquanto extraio os dados...", parse_mode=ParseMode.MARKDOWN)
    
    # Extração
    dados_extraidos = extrair_dados_pdf_in_memory(pdf_bytes.read())

    if "error" in dados_extraidos:
        await update.message.reply_text(f"❌ Erro na extração: {dados_extraidos['error']}. Por favor, tente incluir a O.S. manualmente.", parse_mode=ParseMode.MARKDOWN)
        return await menu_principal(update, context)

    numero_os = dados_extraidos.get('numero_os')
    if not numero_os:
        await update.message.reply_text("❌ Não foi possível extrair o *Número da O.S.*. Verifique o formato do PDF e tente novamente.", parse_mode=ParseMode.MARKDOWN)
        return await menu_principal(update, context)

    # Garante os campos que não são extraídos do PDF
    dados_extraidos['situacao'] = dados_extraidos.get('situacao', 'Pendente')
    dados_extraidos['tecnico'] = dados_extraidos.get('tecnico', 'Não Definido')
    dados_extraidos['agendamento'] = dados_extraidos.get('agendamento', 'Não Agendado')
    dados_extraidos['lembrete_manual'] = dados_extraidos.get('lembrete_manual', 'Nenhum definido')
    dados_extraidos['chat_id'] = update.effective_chat.id


    # Verifica se já existe para atualizar
    os_query = get_os_ref().where(filter=FieldFilter("numero_os", "==", numero_os)).limit(1)
    docs = await os_query.get()
    
    msg_status = ""
    try:
        if docs:
            # Atualiza
            doc_id = docs[0].id
            await get_os_ref().document(doc_id).update(dados_extraidos)
            msg_status = f"✅ O.S. *{numero_os}* atualizada com sucesso via PDF!"
        else:
            # Salva
            await get_os_ref().add(dados_extraidos)
            msg_status = f"✅ O.S. *{numero_os}* incluída com sucesso via PDF!"
    except Exception as e:
        logger.error(f"Erro ao salvar/atualizar OS via PDF: {e}")
        msg_status = "❌ Ocorreu um erro ao salvar a O.S. no banco de dados após a extração. Tente novamente."

    await update.message.reply_text(msg_status, parse_mode=ParseMode.MARKDOWN)

    # Exibe o resumo final
    texto_resumo = format_os_summary(dados_extraidos)
    await update.message.reply_text(texto_resumo, parse_mode=ParseMode.MARKDOWN)

    return await menu_principal(update, context)

# --- Fluxo de Lembrete Manual ---

async def prompt_lembrete_os_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de lembrete e solicita o número da O.S.."""
    context.user_data.clear()
    texto = "Para qual *Número da O.S.* você deseja configurar um lembrete?"

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_caption(caption=texto, parse_mode=ParseMode.MARKDOWN)

    return LEMBRETE_NUMERO

async def receive_lembrete_os_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Verifica se a OS existe e solicita a data/hora do lembrete."""
    numero_os = update.message.text.strip()
    
    if not re.match(r'^\d+$', numero_os):
        await update.message.reply_text("Por favor, digite um número de O.S. válido (apenas números).")
        return LEMBRETE_NUMERO

    os_query = get_os_ref().where(filter=FieldFilter("numero_os", "==", numero_os)).limit(1)
    docs = await os_query.get()

    if not docs:
        await update.message.reply_text(f"❌ O.S. de número *{numero_os}* não encontrada. Tente novamente.", parse_mode=ParseMode.MARKDOWN)
        return LEMBRETE_NUMERO

    os_data = docs[0].to_dict()
    os_data['doc_id'] = docs[0].id
    context.user_data['lembrete_os_data'] = os_data

    context.user_data['lembrete_data'] = {
        'numero_os': numero_os,
        'chat_id': update.effective_chat.id
    }

    texto = f"O.S. *{numero_os}* encontrada. Digite a *data e hora* do lembrete (ex: DD/MM/AAAA HH:MM):"
    await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)

    return LEMBRETE_DATA_HORA

async def receive_lembrete_data_hora(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data/hora do lembrete e solicita a mensagem."""
    data_hora_str = update.message.text.strip()
    
    try:
        lembrete_datetime = datetime.strptime(data_hora_str, '%d/%m/%Y %H:%M')
        
        if lembrete_datetime < datetime.now():
            await update.message.reply_text("A data e hora do lembrete devem ser no futuro. Tente novamente (ex: DD/MM/AAAA HH:MM):")
            return LEMBRETE_DATA_HORA

        context.user_data['lembrete_data']['data_hora_notificacao'] = data_hora_str
        context.user_data['lembrete_data']['timestamp_notificacao'] = int(lembrete_datetime.timestamp())

        texto = "Digite a *mensagem personalizada* do lembrete:"
        await update.message.reply_text(texto, parse_mode=ParseMode.MARKDOWN)
        return LEMBRETE_MENSAGEM

    except ValueError:
        await update.message.reply_text("Formato inválido. Use DD/MM/AAAA HH:MM (ex: 25/10/2025 10:30):")
        return LEMBRETE_DATA_HORA

async def save_lembrete_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva o lembrete manual no Firebase."""
    mensagem = update.message.text.strip()
    lembrete_data = context.user_data['lembrete_data']
    lembrete_data['mensagem'] = mensagem
    lembrete_data['status'] = 'pendente'
    lembrete_data['tipo_alerta'] = 'manual' # Diferencia de alertas de prazo

    # Adiciona a descrição do lembrete na O.S. (Lembrete Manual)
    os_data = context.user_data['lembrete_os_data']
    os_doc_id = os_data['doc_id']
    lembrete_text = f"Lembrete Agendado em {lembrete_data['data_hora_notificacao']}: {mensagem}"
    
    # Atualiza o campo lembrete_manual da OS
    try:
        await get_os_ref().document(os_doc_id).update({"lembrete_manual": lembrete_text})
        await get_lembretes_ref().add(lembrete_data)
        
        msg = f"✅ Lembrete manual para O.S. *{lembrete_data['numero_os']}* salvo com sucesso!\nNotificação agendada para *{lembrete_data['data_hora_notificacao']}*."
    except Exception as e:
        logger.error(f"Erro ao salvar lembrete: {e}")
        msg = "❌ Ocorreu um erro ao salvar o lembrete. Tente novamente."

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return await menu_principal(update, context)

# --- Fluxo de Ajuda Geral ---

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exibe a tela de Ajuda Geral."""
    query = update.callback_query
    await query.answer()

    texto_ajuda = (
        "*Ajuda Geral - Gerenciador de O.S.* 🤖\n\n"
        "Este bot foi criado para gerenciar o ciclo de vida das suas Ordens de Serviço (O.S.).\n\n"
        "**Comandos Principais:**\n"
        "➕ *Incluir O.S.*: Inicia um fluxo passo a passo para cadastrar uma nova O.S., com validação de duplicidade.\n"
        "🔄 *Atualizar O.S.*: Permite buscar uma O.S. pelo número e editar *qualquer* campo de forma interativa.\n"
        "🗑️ *Deletar O.S.*: Busca e permite a exclusão definitiva de uma O.S. mediante confirmação.\n"
        "📋 *Listar O.S.*: Permite filtrar e listar O.S. por Tipo (Corretiva/Preventiva) e Situação (Pendente/Concluído, etc.).\n"
        "📄 *Enviar PDF*: Processa um arquivo PDF enviado no chat, extrai as informações principais e salva/atualiza a O.S. automaticamente.\n"
        "⏰ *Lembrete*: Cria um alerta manual, com data e hora específicas, atrelado a uma O.S. e com mensagem customizada.\n\n"
        "**Notificações Automáticas:**\n"
        "O bot notifica diariamente sobre O.S. que estão *Vencidas* ou que vencem em *1 e 2 dias*, desde que não estejam na Situação *'Concluído'*.\n"
        "Use o comando `/start` ou o botão de retorno para voltar ao menu principal a qualquer momento."
    )

    keyboard = [[InlineKeyboardButton("↩️ Voltar ao Menu Principal", callback_data="menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_caption(caption=texto_ajuda, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return MENU

# --- Job Queue para Notificações Automáticas ---

async def notify_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str):
    """Envia uma mensagem de notificação para o chat especificado."""
    try:
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Não foi possível enviar notificação para {chat_id}: {e}")

async def check_deadlines_job(context: ContextTypes.DEFAULT_TYPE):
    """Verifica O.S. vencidas e próximas do prazo (1 e 2 dias)."""
    
    # 1. Notificações de Prazo (automáticas)
    today = datetime.now().date()
    two_days_from_now = (today + timedelta(days=2))
    one_day_from_now = (today + timedelta(days=1))
    
    # Busca todas as O.S. que não estão 'Concluído'
    os_query = get_os_ref().where(filter=FieldFilter("situacao", "!=", "Concluído"))
    docs = await os_query.get()
    
    notifications = {} # {chat_id: [mensagens]}

    for doc in docs:
        os_data = doc.to_dict()
        prazo_str = os_data.get('prazo')
        numero_os = os_data.get('numero_os', 'N/A')
        chat_id = os_data.get('chat_id')
        
        if not prazo_str or not chat_id:
            continue

        try:
            prazo_date = datetime.strptime(prazo_str, '%d/%m/%Y').date()
            
            status_notif = None
            if prazo_date < today:
                status_notif = "🚨 *VENCIDA* 🚨"
            elif prazo_date == one_day_from_now:
                status_notif = "⚠️ *VENCE AMANHÃ* ⚠️"
            elif prazo_date == two_days_from_now:
                status_notif = "🔔 *VENCE EM 2 DIAS* 🔔"
                
            if status_notif:
                msg = f"O.S. *{numero_os}* - {status_notif}\nPrazo: _{prazo_str}_ | Situação: _{os_data.get('situacao')}_"
                
                if chat_id not in notifications:
                    notifications[chat_id] = []
                notifications[chat_id].append(msg)
                
        except ValueError:
            logger.warning(f"OS {numero_os}: Prazo inválido ({prazo_str}) ignorado na verificação.")
            continue
            
    # Envia as notificações de prazo
    for chat_id, msgs in notifications.items():
        full_msg = "⭐ *ALERTA DE PRAZO DE O.S.* ⭐\n\n" + "\n---\n".join(msgs)
        await notify_user(context, chat_id, full_msg)

    # 2. Notificações de Lembrete (manuais)
    current_timestamp = int(datetime.now().timestamp())
    
    # Busca lembretes pendentes que já atingiram o timestamp
    lembretes_query = get_lembretes_ref().where(filter=FieldFilter("status", "==", "pendente")).where(filter=FieldFilter("timestamp_notificacao", "<=", current_timestamp))
    lembretes_docs = await lembretes_query.get()
    
    for doc in lembretes_docs:
        lembrete_data = doc.to_dict()
        lembrete_id = doc.id
        
        msg = (
            f"🔔 *LEMBRETE MANUAL AGENDADO* 🔔\n\n"
            f"O.S. Nº *{lembrete_data.get('numero_os')}* \n"
            f"Data/Hora: *{lembrete_data.get('data_hora_notificacao')}*\n"
            f"Mensagem: _{lembrete_data.get('mensagem')}_"
        )
        
        await notify_user(context, lembrete_data['chat_id'], msg)
        
        # Marca como concluído para não notificar novamente
        await get_lembretes_ref().document(lembrete_id).update({"status": "notificado"})
        
    logger.info("Verificação de prazos e lembretes concluída.")


# --- Handler de Callback (Geral) ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gerencia callbacks de botões para navegação principal."""
    query = update.callback_query
    data = query.data
    
    # Navegação do menu principal
    if data == "menu":
        await query.answer()
        return await menu_principal(update, context)
    
    if data == "incluir":
        return await prompt_os_number(update, context)
        
    if data == "atualizar":
        return await prompt_update_os_number(update, context)
        
    if data == "deletar":
        return await prompt_delete_os_number(update, context)
        
    if data == "listar":
        return await prompt_list_type(update, context)

    if data == "enviar_pdf":
        return await prompt_receive_pdf(update, context)
        
    if data == "lembrete_menu":
        return await prompt_lembrete_os_number(update, context)
        
    if data == "ajuda":
        return await show_help(update, context)

    # Decisões no resumo de inclusão/duplicidade
    if data == "incluir_atualizar":
        # Continua o fluxo de inclusão/atualização (pulando campos já preenchidos)
        return await prompt_os_prefixo(update, context)
        
    if data == "confirmar_inclusao":
        return await finalize_os_save(update, context)
        
    if data == "editar_inclusao":
        return await start_edit_from_summary(update, context)
    
    # Confirmação de exclusão
    if data == "confirmar_exclusao":
        return await confirm_delete_os(update, context)

    # Navegação/Ações dentro dos fluxos (tratadas nos Conversation States específicos)
    if data.startswith("update_field_external") or data.startswith("update_field"):
        return await prompt_update_field(update, context)
    
    if data.startswith("update_value"):
        return await receive_update_value(update, context)

    if data == "atualizacao_finalizada":
        # Se veio do fluxo de Atualização de O.S. externo, finaliza
        if context.user_data.get('flow') == 'atualizar_os':
            return await return_to_summary_or_finish(update, context)
        # Se veio do resumo de inclusão/duplicidade, vai para a confirmação final
        else:
             return await show_final_summary(update, context)

    return MENU # Volta ao menu se o callback não for reconhecido

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Trata comandos não reconhecidos."""
    await update.message.reply_text("Comando não reconhecido. Use /start para ir ao menu principal.")
    return MENU

# --- Main ---

def main():
    """Inicializa e executa o bot no modo webhook."""
    if not TOKEN:
        logger.error("O token do Telegram não foi encontrado. Defina a variável de ambiente TELEGRAM_TOKEN.")
        return

    application = Application.builder().token(TOKEN).build()
    
    # Adiciona a fila de jobs para a verificação de prazos
    job_queue: JobQueue = application.job_queue
    # Agenda a verificação de prazos para rodar diariamente às 08:00 (ajuste o horário conforme a necessidade)
    job_queue.run_daily(check_deadlines_job, time=datetime.strptime('08:00', '%H:%M').time(), name="deadline_checker")
    
    # ----------------------------------------------------
    # Conversation Handler - Define o fluxo de conversação
    # ----------------------------------------------------
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        
        states={
            MENU: [
                CallbackQueryHandler(callback_handler, pattern="^(incluir|atualizar|deletar|listar|enviar_pdf|lembrete_menu|ajuda)$"),
            ],
            
            # ----------------------------------------------------
            # Fluxo de Inclusão de O.S. (Passo a Passo)
            # ----------------------------------------------------
            INCLUSAO_NUMERO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_number),
            ],
            INCLUSAO_PREFIXO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_prefixo),
            ],
            INCLUSAO_CHAMADO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_chamado),
            ],
            INCLUSAO_DISTANCIA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_distancia),
            ],
            INCLUSAO_DESCRICAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_descricao),
            ],
            INCLUSAO_CRITICIDADE: [
                CallbackQueryHandler(receive_os_criticidade, pattern="^criticidade:"),
            ],
            INCLUSAO_TIPO: [
                CallbackQueryHandler(receive_os_tipo, pattern="^tipo:"),
            ],
            INCLUSAO_PRAZO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_prazo),
            ],
            INCLUSAO_SITUACAO: [
                CallbackQueryHandler(receive_os_situacao, pattern="^situacao:"),
            ],
            INCLUSAO_TECNICO: [
                CallbackQueryHandler(prompt_os_tecnico, pattern="^(tecnico_definido|tecnico_nao_definido)$"),
            ],
            INCLUSAO_TECNICO_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_tecnico_name),
            ],
            INCLUSAO_AGENDAMENTO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_agendamento),
            ],
            INCLUSAO_RESUMO: [
                CallbackQueryHandler(callback_handler, pattern="^(confirmar_inclusao|editar_inclusao|menu|incluir_atualizar)$"),
                CallbackQueryHandler(return_to_summary_or_finish, pattern="^atualizacao_finalizada$"),
            ],
            
            # ----------------------------------------------------
            # Fluxo de Atualização/Edição de Campo
            # ----------------------------------------------------
            ATUALIZACAO_NUMERO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_update_os_number),
            ],
            ATUALIZACAO_CAMPO_SELECIONADO: [
                # Recebe o novo valor (texto)
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_update_value),
                # Recebe a seleção de campo para editar (callback)
                CallbackQueryHandler(prompt_update_field, pattern="^update_field"),
                # Recebe o novo valor (callback para botões de Criticidade/Tipo/Situação)
                CallbackQueryHandler(receive_update_value, pattern="^update_value:"),
                # Botão "Concluir Atualização" / "Voltar ao Resumo"
                CallbackQueryHandler(return_to_summary_or_finish, pattern="^atualizacao_finalizada$"),
            ],
            
            # ----------------------------------------------------
            # Fluxo de Deletar O.S.
            # ----------------------------------------------------
            DELETAR_NUMERO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_delete_os_number),
            ],
            DELETAR_CONFIRMACAO: [
                CallbackQueryHandler(callback_handler, pattern="^(confirmar_exclusao|menu)$"),
            ],
            
            # ----------------------------------------------------
            # Fluxo de Listar O.S.
            # ----------------------------------------------------
            LISTAR_TIPO: [
                CallbackQueryHandler(prompt_list_status, pattern="^list_type:"),
            ],
            LISTAR_SITUACAO: [
                CallbackQueryHandler(execute_list_os, pattern="^list_status:"),
            ],
            
            # ----------------------------------------------------
            # Fluxo de Enviar PDF
            # ----------------------------------------------------
            RECEBER_PDF_FLOW: [
                MessageHandler(filters.Document.PDF, receive_pdf_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_receive_pdf), # Trata se enviar texto
            ],
            
            # ----------------------------------------------------
            # Fluxo de Lembrete Manual
            # ----------------------------------------------------
            LEMBRETE_NUMERO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_lembrete_os_number),
            ],
            LEMBRETE_DATA_HORA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_lembrete_data_hora),
            ],
            LEMBRETE_MENSAGEM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_lembrete_manual),
            ],
        },
        
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(callback_handler, pattern='^menu$'), # Última chance para voltar ao menu
            MessageHandler(filters.COMMAND, fallback_command), # Comandos não reconhecidos
        ],
        allow_reentry=True # Permite reentrar na conversa (ex: com /start)
    )

    # Adiciona o ConversationHandler e o handler de comandos de fallback
    application.add_handler(conv_handler)
    
    # 4. Configuração do Webhook
    # Esta é a configuração para rodar em ambientes de hosting com Webhook
    if WEBHOOK_URL and PORT:
        try:
            WEBHOOK_PATH = "/" + TOKEN # Usamos o token como path para maior segurança
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=WEBHOOK_PATH,
                webhook_url=WEBHOOK_URL + WEBHOOK_PATH,
                secret_token=uuid.uuid4().hex # Adiciona um secret token para maior segurança
            )
            logger.info(f"Servidor Webhook iniciado e escutando na porta {PORT}.")
            logger.info(f"Webhook URL configurada no Telegram: {WEBHOOK_URL + WEBHOOK_PATH}")
        except Exception as e:
            logger.error(f"Falha ao iniciar Webhook: {e}")
            
    # Se não estiver configurado para webhook, roda em modo polling (para teste local)
    else:
        logger.info("Executando em modo Polling (ambiente local).")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
