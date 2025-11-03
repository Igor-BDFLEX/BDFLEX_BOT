# bot_os_telegram.py - Bot para Gestão de Ordens de Serviço (OS) via Telegram (WEBHOOK MODE)
#
# Este bot permite:
# 1. Criação, visualização, atualização e eliminação de Ordens de Serviço (OS).
# 2. Gestão de alertas (lembretes) associados a uma OS específica.
# 3. Agendamento de lembretes manuais.
# 4. Exportação do estado atual das OS para PDF (requer PyMuPDF e pandas).
# 5. Utiliza Firebase Firestore para persistência de dados.

# --- Imports e Setup ---

import logging
import json
import time
import os
import re # Para manipulação de texto e validação de formatos
import uuid # Para IDs únicos
from datetime import datetime, timedelta
import asyncio # Adicionado para tarefas assíncronas
import aiohttp # Para requisições HTTP (Manter o bot ativo)
import io # Para manipulação de arquivos em memória

# --- Imports para .env (secrets) ---
from dotenv import load_dotenv

# --- Imports para PDF (necessitam de instalação via pip: PyMuPDF e pandas) ---
try:
    # A importação desses módulos garante que o recurso de PDF está disponível.
    import fitz # PyMuPDF
    import pandas as pd
    PDF_PROCESSOR_AVAILABLE = True
except ImportError:
    # Se PyMuPDF ou Pandas não estiverem disponíveis, o recurso Enviar PDF será desativado
    logging.warning("Módulos 'fitz' (PyMuPDF) e/ou 'pandas' não encontrados. O recurso Enviar PDF não funcionará.")
    PDF_PROCESSOR_AVAILABLE = False
    class MockDataFrame: # Placeholder para evitar erros
        def __init__(self, *args, **kwargs): pass
        def to_csv(self, *args, **kwargs): pass
        def from_dict(self, *args, **kwargs): return self
    pd = MockDataFrame()

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app

# Python Telegram Bot
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
)
from telegram.constants import ParseMode

# --- Configuração ---

# Habilita o logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Define níveis de log mais altos para bibliotecas que usam muito log
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Carrega variáveis de ambiente (útil para desenvolvimento local)
load_dotenv()

# --- Variáveis de Ambiente ---
# A Render define a porta (PORT) e pode definir a URL pública (RENDER_EXTERNAL_URL)
PORT = int(os.environ.get("PORT", "8080")) 
# Prioriza WEBHOOK_URL (se definido), senão usa RENDER_EXTERNAL_URL (padrão Render)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_PATH = "/" + TOKEN if TOKEN else "/webhook" # O caminho usa o token para segurança

# --- Firebase Init ---

DB = None
FIREBASE_INITIALIZED = False

# Função para inicializar o Firebase
def firebase_init():
    global DB, FIREBASE_INITIALIZED
    
    # Se já inicializado, apenas retorna
    if FIREBASE_INITIALIZED:
        return
        
    try:
        # 1. Tenta carregar as credenciais da variável de ambiente FIREBASE_CREDENTIALS (Render)
        credentials_json_str = os.getenv("FIREBASE_CREDENTIALS")

        if credentials_json_str:
            cred_dict = json.loads(credentials_json_str)
            
            # CORREÇÃO CRÍTICA PARA O ERRO PEM: 
            # Assegura que a chave privada contém quebras de linha reais (\n) 
            # em vez de strings literais ('\\n'), o que é comum em variáveis de ambiente.
            if 'private_key' in cred_dict and '\\n' in cred_dict['private_key']:
                cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
            
            cred = credentials.Certificate(cred_dict)
            
        else:
            logger.error("Variável de ambiente 'FIREBASE_CREDENTIALS' não encontrada.")
            raise ValueError("Credenciais de Firebase ausentes.")

        # Inicializa o Firebase
        initialize_app(cred)
        DB = firestore.client()
        FIREBASE_INITIALIZED = True
        logger.info("Firebase inicializado com sucesso usando credenciais de ambiente.")

    except Exception as e:
        logger.error(f"Falha ao inicializar o Firebase: {e}")
        logger.warning("Usando banco de dados mock. Funcionalidade de persistência está desativada.")
        DB = None
        FIREBASE_INITIALIZED = False

# Chamada de inicialização
firebase_init()

# --- Helpers de Persistência (Firestore ou Mock) ---

def get_db():
    """Retorna o objeto DB ou levanta um erro se não estiver inicializado."""
    if not FIREBASE_INITIALIZED or DB is None:
        return None
    return DB

def get_os_ref(os_id: str):
    """Retorna a referência de um documento OS."""
    db = get_db()
    if db:
        return db.collection("ordens_servico").document(os_id)
    return None

def get_alerts_ref():
    """Retorna a referência da coleção de alertas."""
    db = get_db()
    if db:
        return db.collection("alertas")
    return None

async def fetch_all_os() -> dict:
    """Busca todas as Ordens de Serviço."""
    db = get_db()
    if db is None:
        return {} # Mock
    try:
        docs = db.collection("ordens_servico").stream()
        os_list = {doc.id: doc.to_dict() for doc in docs}
        return os_list
    except Exception as e:
        logger.error(f"Erro ao buscar todas as OS: {e}")
        return {}

async def save_os(os_data: dict) -> str:
    """Salva ou atualiza uma Ordem de Serviço."""
    db = get_db()
    if db is None:
        return str(uuid.uuid4()) # Mock ID
    
    os_id = os_data.get("id")
    if not os_id:
        os_id = str(uuid.uuid4())
        os_data["id"] = os_id
        os_data["data_criacao"] = datetime.now().isoformat()
    
    try:
        ref = db.collection("ordens_servico").document(os_id)
        await asyncio.to_thread(ref.set, os_data)
        return os_id
    except Exception as e:
        logger.error(f"Erro ao salvar OS {os_id}: {e}")
        return os_id

async def delete_os(os_id: str) -> bool:
    """Deleta uma Ordem de Serviço e seus alertas associados."""
    db = get_db()
    if db is None:
        return True # Mock Success

    try:
        # 1. Deletar a OS
        os_ref = db.collection("ordens_servico").document(os_id)
        await asyncio.to_thread(os_ref.delete)
        
        # 2. Deletar alertas associados (opcional, mas recomendado)
        alerts_ref = db.collection("alertas")
        q = alerts_ref.where("os_id", "==", os_id).stream()
        
        for doc in q:
            await asyncio.to_thread(doc.reference.delete)
            
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar OS {os_id} e/ou alertas: {e}")
        return False

# --- Helpers de Alerta (Lembrete) ---

async def schedule_alert(os_id: str, chat_id: int, due_date: datetime, message: str) -> str:
    """Agenda um novo alerta no Firestore."""
    db = get_db()
    alert_id = str(uuid.uuid4())
    alert_data = {
        "id": alert_id,
        "os_id": os_id,
        "chat_id": chat_id,
        "due_date": due_date.isoformat(),
        "message": message,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    
    if db is None:
        return alert_id # Mock
    
    try:
        ref = db.collection("alertas").document(alert_id)
        await asyncio.to_thread(ref.set, alert_data)
        return alert_id
    except Exception as e:
        logger.error(f"Erro ao agendar alerta: {e}")
        return alert_id
        
async def fetch_alert(alert_id: str) -> dict | None:
    """Busca um alerta específico."""
    db = get_db()
    if db is None:
        return None
    try:
        ref = db.collection("alertas").document(alert_id)
        doc = await asyncio.to_thread(ref.get)
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        logger.error(f"Erro ao buscar alerta {alert_id}: {e}")
        return None

async def delete_alert(alert_id: str) -> bool:
    """Deleta um alerta específico."""
    db = get_db()
    if db is None:
        return True # Mock
    try:
        ref = db.collection("alertas").document(alert_id)
        await asyncio.to_thread(ref.delete)
        return True
    except Exception as e:
        logger.error(f"Erro ao deletar alerta {alert_id}: {e}")
        return False

# --- Estados para o ConversationHandler ---
MENU, PROMPT_OS, PROMPT_DESCRICAO, PROMPT_TIPO, PROMPT_STATUS, PROMPT_ATUALIZACAO, PROMPT_ALERTA, PROMPT_INCLUSAO, PROMPT_ID_ALERTA, PROMPT_TIPO_INCLUSAO, LEMBRETE_MENU, PROMPT_ID_LEMBRETE, PROMPT_LEMBRETE_DATA, PROMPT_LEMBRETE_MSG = range(14)

# --- Funções de Formatação ---

def format_os_message(os_data: dict) -> str:
    """Formata os dados da OS para exibição no Telegram."""
    if not os_data:
        return "OS não encontrada."

    id_os = os_data.get("id", "N/A")
    descricao = os_data.get("descricao", "N/A")
    tipo = os_data.get("tipo", "N/A")
    status = os_data.get("status", "N/A")
    criacao = os_data.get("data_criacao", "N/A")
    atualizacao = os_data.get("ultima_atualizacao", "N/A")
    alerta = os_data.get("alerta", "N/A")
    
    # Formata a data de criação
    try:
        data_criacao = datetime.fromisoformat(criacao).strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        data_criacao = criacao
        
    # Formata a última atualização
    try:
        data_atualizacao = datetime.fromisoformat(atualizacao).strftime("%d/%m/%Y %H:%M") if atualizacao != "N/A" else "N/A"
    except (ValueError, TypeError):
        data_atualizacao = atualizacao

    
    message = (
        f"🛠️ *Detalhes da Ordem de Serviço (OS)*\n"
        f"--- \n"
        f"*ID:* `{id_os}`\n"
        f"*Descrição:* {descricao}\n"
        f"*Tipo:* {tipo}\n"
        f"*Status:* {status}\n"
        f"--- \n"
        f"*Data de Criação:* {data_criacao}\n"
        f"*Última Atualização:* {data_atualizacao}\n"
        f"*Alerta Agendado:* {alerta}\n"
    )
    return message

def get_main_menu_keyboard(update: Update) -> InlineKeyboardMarkup:
    """Cria o teclado do menu principal."""
    keyboard = [
        [
            InlineKeyboardButton("➕ Nova OS", callback_data='menu|nova_os'),
            InlineKeyboardButton("🔎 Ver OS", callback_data='menu|ver_os'),
        ],
        [
            InlineKeyboardButton("🔄 Atualizar OS", callback_data='menu|atualizar_os'),
            InlineKeyboardButton("❌ Deletar OS", callback_data='menu|deletar_os'),
        ],
        [
            InlineKeyboardButton("🔔 Gestão de Alertas", callback_data='menu|alerta_menu'),
            InlineKeyboardButton("PDF 📄", callback_data='menu|exportar_pdf'),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Handlers Principais ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a conversa e exibe o menu principal."""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text="Bem-vindo ao Gerenciador de OS!\nEscolha uma opção:",
            reply_markup=get_main_menu_keyboard(update),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "Olá! Eu sou o seu bot para gestão de Ordens de Serviço (OS).",
            parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(
            "Escolha uma opção no menu:",
            reply_markup=get_main_menu_keyboard(update),
            parse_mode=ParseMode.MARKDOWN
        )
    
    return MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a conversa atual e volta ao menu."""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "Operação cancelada. Voltando ao menu principal.",
            reply_markup=get_main_menu_keyboard(update),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "Operação cancelada. Voltando ao menu principal.",
            reply_markup=get_main_menu_keyboard(update),
            parse_mode=ParseMode.MARKDOWN
        )
    return MENU

# --- Funções de Criação de OS ---

async def prompt_os_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede a descrição da nova OS."""
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "Por favor, digite a *descrição* detalhada da nova Ordem de Serviço:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_DESCRICAO

async def receive_os_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição e pede o tipo."""
    context.user_data["os_temp"] = {"descricao": update.text}
    
    keyboard = [
        [InlineKeyboardButton("Manutenção", callback_data='os_tipo|Manutenção')],
        [InlineKeyboardButton("Instalação", callback_data='os_tipo|Instalação')],
        [InlineKeyboardButton("Suporte", callback_data='os_tipo|Suporte')],
        [InlineKeyboardButton("Outro", callback_data='os_tipo|Outro')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')],
    ]
    
    await update.message.reply_text(
        "Qual é o *tipo* desta OS?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_TIPO

async def receive_os_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o tipo e salva a OS com status inicial 'Pendente'."""
    query = update.callback_query
    await query.answer()
    
    # Extrai o tipo
    # A estrutura do callback_data é 'os_tipo|TipoEscolhido'
    tipo = query.data.split('|')[1]
    
    os_data = context.user_data["os_temp"]
    os_data["tipo"] = tipo
    os_data["status"] = "Pendente"
    os_data["ultima_atualizacao"] = datetime.now().isoformat()
    os_data["alerta"] = "Nenhum"
    os_data["user_id"] = query.from_user.id
    
    # Salva no Firestore
    os_id = await save_os(os_data)
    
    # Limpa dados temporários
    context.user_data.pop("os_temp")
    
    message_text = (
        f"✅ *OS Criada com Sucesso!*\n"
        f"--- \n"
        f"*ID:* `{os_id}`\n"
        f"*Descrição:* {os_data['descricao'][:50]}...\n"
        f"*Tipo:* {tipo}\n"
        f"*Status:* Pendente\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("🔔 Gerenciar Alerta", callback_data=f'alerta_set|{os_id}')],
        [InlineKeyboardButton("⬅️ Menu Principal", callback_data='menu')]
    ]
    
    await query.edit_message_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return MENU

# --- Funções de Visualização e Busca de OS ---

async def prompt_os_id(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> int:
    """Pede o ID da OS para uma ação específica."""
    query = update.callback_query
    await query.answer()
    
    context.user_data["action_os"] = action
    
    if action == 'ver_os':
        prompt = "Por favor, digite o *ID* da OS que você deseja *visualizar*."
        all_os = await fetch_all_os()
        if all_os:
            os_list_text = "\n".join([f"`{os_id}`: {data['descricao'][:30]}..." for os_id, data in all_os.items()])
            prompt += f"\n\n*OS Ativas:*\n{os_list_text}"
    elif action == 'atualizar_os':
        prompt = "Por favor, digite o *ID* da OS que você deseja *atualizar*."
    elif action == 'deletar_os':
        prompt = "Por favor, digite o *ID* da OS que você deseja *deletar*."
        
    await query.edit_message_text(
        prompt,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS

async def process_os_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processa o ID recebido e encaminha para a ação correta."""
    os_id = update.text.strip()
    action = context.user_data.get("action_os")
    
    os_data = await asyncio.to_thread(get_os_ref(os_id).get) if get_os_ref(os_id) else None
    if os_data and os_data.exists:
        os_data = os_data.to_dict()
        os_data["id"] = os_id # Garante que o ID está no dict

        if action == 'ver_os':
            return await show_os_details(update, context, os_data)
        elif action == 'atualizar_os':
            context.user_data["os_temp"] = os_data
            return await prompt_update_field(update, context)
        elif action == 'deletar_os':
            return await confirm_delete_os(update, context, os_id)
        
    else:
        await update.message.reply_text(
            f"❌ OS com ID `{os_id}` não encontrada.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='menu')]]),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_OS # Permanece no estado para nova tentativa

async def show_os_details(update: Update, context: ContextTypes.DEFAULT_TYPE, os_data: dict) -> int:
    """Exibe os detalhes de uma OS."""
    message = format_os_message(os_data)
    
    keyboard = [
        [InlineKeyboardButton("🔄 Atualizar", callback_data=f'update_start|{os_data["id"]}')],
        [InlineKeyboardButton("🔔 Gerenciar Alerta", callback_data=f'alerta_set|{os_data["id"]}')],
        [InlineKeyboardButton("⬅️ Menu Principal", callback_data='menu')]
    ]
    
    await update.message.reply_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data.pop("action_os", None)
    return MENU

# --- Funções de Atualização de OS ---

async def prompt_update_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede ao usuário qual campo da OS deseja atualizar."""
    os_id = context.user_data["os_temp"]["id"]
    
    keyboard = [
        [InlineKeyboardButton("Descrição", callback_data='update_field|descricao')],
        [InlineKeyboardButton("Tipo", callback_data='update_field|tipo')],
        [InlineKeyboardButton("Status", callback_data='update_field|status')],
        [InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]
    ]
    
    await update.message.reply_text(
        f"OS `{os_id}` selecionada. Qual campo você deseja *atualizar*?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ATUALIZACAO

async def prompt_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede o novo valor para o campo selecionado."""
    query = update.callback_query
    await query.answer()
    
    field = query.data.split('|')[1]
    context.user_data["update_field"] = field
    
    if field == 'status':
        keyboard = [
            [InlineKeyboardButton("Pendente", callback_data='update_value|Pendente')],
            [InlineKeyboardButton("Em Andamento", callback_data='update_value|Em Andamento')],
            [InlineKeyboardButton("Concluída", callback_data='update_value|Concluída')],
            [InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]
        ]
        await query.edit_message_text(
            "Selecione o novo *Status*:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        # Permanece no estado PROMPT_ATUALIZACAO, a próxima ação (update_value) fará a transição
        return PROMPT_ATUALIZACAO
    
    elif field == 'tipo':
        keyboard = [
            [InlineKeyboardButton("Manutenção", callback_data='update_value|Manutenção')],
            [InlineKeyboardButton("Instalação", callback_data='update_value|Instalação')],
            [InlineKeyboardButton("Suporte", callback_data='update_value|Suporte')],
            [InlineKeyboardButton("Outro", callback_data='update_value|Outro')],
            [InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]
        ]
        await query.edit_message_text(
            "Selecione o novo *Tipo*:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ATUALIZACAO
        
    else: # Descrição
        await query.edit_message_text(
            f"Digite o novo valor para *{field.capitalize()}*:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]]),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ATUALIZACAO

async def receive_and_save_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o novo valor (texto) e salva a atualização."""
    # Este handler é chamado APENAS se for uma MessageHandler (ou seja, descrição)
    
    new_value = update.text
    field = context.user_data.get("update_field")
    
    return await save_update(update, context, new_value, field)

async def handle_callback_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o novo valor (callback) e salva a atualização."""
    # Este handler é chamado se o valor for via CallbackQuery (status ou tipo)
    query = update.callback_query
    await query.answer()
    
    # O callback data é 'update_value|NovoValor'
    new_value = query.data.split('|')[1]
    field = context.user_data.get("update_field")

    # Chama a função de salvamento com a Update de CallbackQuery
    return await save_update(update, context, new_value, field)
    
async def save_update(update: Update, context: ContextTypes.DEFAULT_TYPE, new_value: str, field: str) -> int:
    """Função central para aplicar e salvar a atualização no Firestore."""
    
    os_data = context.user_data["os_temp"]
    os_id = os_data["id"]

    os_data[field] = new_value
    os_data["ultima_atualizacao"] = datetime.now().isoformat()
    
    await save_os(os_data)
    
    # Limpa dados temporários
    context.user_data.pop("os_temp")
    context.user_data.pop("update_field")
    context.user_data.pop("action_os", None)

    message = (
        f"✅ *OS `{os_id}` Atualizada!*\n"
        f"Campo *{field.capitalize()}* atualizado para: _{new_value}_."
    )
    
    keyboard = [
        [InlineKeyboardButton("🔎 Ver Detalhes", callback_data=f'ver_os_id|{os_id}')],
        [InlineKeyboardButton("⬅️ Menu Principal", callback_data='menu')]
    ]
    
    # Usa edit_message_text se for CallbackQuery, senão usa reply_text
    if update.callback_query:
        await update.callback_query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        
    return MENU

# --- Funções de Deleção de OS ---

async def confirm_delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str) -> int:
    """Solicita confirmação para deletar a OS."""
    
    keyboard = [
        [InlineKeyboardButton("🚨 CONFIRMAR DELEÇÃO 🚨", callback_data=f'confirm_delete_{os_id}')],
        [InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]
    ]
    
    await update.message.reply_text(
        f"⚠️ *Atenção!* Você está prestes a deletar a OS com ID `{os_id}`.\n"
        f"Esta ação não pode ser desfeita. Confirma a deleção?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    # Define o estado da conversa para aguardar a confirmação de deleção
    return MENU 

async def finalize_delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executa a deleção da OS após a confirmação."""
    query = update.callback_query
    await query.answer()
    
    # Extrai o ID da OS: 'confirm_delete_OS_ID'
    os_id = query.data.split('_')[-1]
    
    success = await delete_os(os_id)
    
    if success:
        message = f"✅ *OS `{os_id}` e alertas associados deletados com sucesso!*"
    else:
        message = f"❌ *Falha ao deletar a OS `{os_id}`*. Tente novamente."
        
    await query.edit_message_text(
        message,
        reply_markup=get_main_menu_keyboard(update),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data.pop("action_os", None)
    return MENU

# --- Funções de Alerta (Lembrete) ---

async def alerta_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exibe o menu de gestão de alertas."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("➕ Incluir Alerta em OS", callback_data='alerta_os_prompt')],
        [InlineKeyboardButton("❌ Remover Alerta de OS", callback_data='remover_alerta_prompt')],
        [InlineKeyboardButton("🕰️ Agendar Lembrete Manual", callback_data='lembrete_manual_start')],
        [InlineKeyboardButton("⬅️ Menu Principal", callback_data='menu')],
    ]
    
    await query.edit_message_text(
        "🔔 *Gestão de Alertas e Lembretes*\nEscolha uma opção:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ALERTA
    
async def prompt_os_id_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede o ID da OS para incluir/remover alerta."""
    query = update.callback_query
    await query.answer()
    
    action = query.data.split('|')[0] # 'alerta_os_prompt' ou 'remover_alerta_prompt'
    context.user_data["alerta_action"] = action
    
    prompt = "Por favor, digite o *ID* da OS para gerenciar o alerta:"
    
    await query.edit_message_text(
        prompt,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data='alerta_menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ID_ALERTA
    
async def prompt_alerta_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o ID da OS e pede o prazo do alerta."""
    os_id = update.text.strip()
    action = context.user_data.get("alerta_action")
    
    os_ref = get_os_ref(os_id)
    os_data = await asyncio.to_thread(os_ref.get) if os_ref else None

    if not os_data or not os_data.exists:
        await update.message.reply_text(
            f"❌ OS com ID `{os_id}` não encontrada.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data='alerta_menu')]]),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ID_ALERTA # Permanece no estado para nova tentativa
        
    context.user_data["os_id_alerta"] = os_id
    
    if action == 'remover_alerta_prompt':
        return await remove_alerta_confirm(update, context, os_data.to_dict())

    # Continua para inclusão
    await update.message.reply_text(
        f"OS `{os_id}` encontrada. Digite a *data e hora* do alerta (Ex: 01/12/2025 10:00):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_INCLUSAO

async def receive_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data/hora e pede a mensagem do alerta."""
    
    # 1. Valida a data
    date_str = update.text.strip()
    try:
        due_date = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
        if due_date <= datetime.now():
             await update.message.reply_text(
                "❌ A data do alerta deve ser *futura*. Tente novamente (Ex: 01/12/2025 10:00):",
                parse_mode=ParseMode.MARKDOWN
            )
             return PROMPT_INCLUSAO
             
        context.user_data["alerta_due_date"] = due_date
    except ValueError:
        await update.message.reply_text(
            "❌ Formato de data/hora inválido. Use o formato *DD/MM/AAAA HH:MM* (Ex: 01/12/2025 10:00):",
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_INCLUSAO

    # 2. Pede a mensagem
    await update.message.reply_text(
        "Digite a *mensagem* para o alerta/lembrete (Ex: Ligar para o cliente):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_TIPO_INCLUSAO

async def save_os_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva o alerta no Firestore e atualiza a OS."""
    
    alert_message = update.text.strip()
    os_id = context.user_data.get("os_id_alerta")
    due_date = context.user_data.get("alerta_due_date")
    chat_id = update.effective_chat.id
    
    # 1. Agenda o alerta
    alert_id = await schedule_alert(os_id, chat_id, due_date, alert_message)
    
    # 2. Atualiza a OS para refletir o alerta
    os_ref = get_os_ref(os_id)
    if os_ref:
        await asyncio.to_thread(os_ref.update, {
            "alerta": f"Agendado para {due_date.strftime('%d/%m/%Y %H:%M')}",
            "alerta_id": alert_id,
        })

    # 3. Confirmação
    message = (
        f"🔔 *Alerta Agendado!* \n"
        f"OS ID: `{os_id}`\n"
        f"Para: {due_date.strftime('%d/%m/%Y %H:%M')}\n"
        f"Mensagem: {alert_message}\n"
    )

    await update.message.reply_text(
        message,
        reply_markup=get_main_menu_keyboard(update),
        parse_mode=ParseMode.MARKDOWN
    )
    
    context.user_data.pop("os_id_alerta", None)
    context.user_data.pop("alerta_due_date", None)
    context.user_data.pop("alerta_action", None)
    return MENU

async def remove_alerta_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, os_data: dict) -> int:
    """Pede confirmação para remover alerta."""
    query_target = update.callback_query or update.message
    os_id = os_data.get("id")
    alerta_info = os_data.get("alerta")
    alerta_id = os_data.get("alerta_id")
    
    if alerta_info == "Nenhum" or not alerta_id:
        message = f"❌ A OS `{os_id}` não possui um alerta agendado."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu Alerta", callback_data='alerta_menu')]])
    else:
        context.user_data["alerta_id_remover"] = alerta_id
        message = (
            f"⚠️ Confirma a remoção do alerta agendado:\n"
            f"*OS ID:* `{os_id}`\n"
            f"*Detalhe:* {alerta_info}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar Remoção", callback_data=f'confirm_remove_alerta')],
            [InlineKeyboardButton("⬅️ Cancelar", callback_data='alerta_menu')]
        ])

    if update.callback_query:
        await query_target.edit_message_text(message, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    else:
        await query_target.reply_text(message, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

    # Não muda de estado, o callback 'confirm_remove_alerta' fará a transição
    return PROMPT_ID_ALERTA 

async def finalize_remove_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Executa a remoção do alerta."""
    query = update.callback_query
    await query.answer()

    alerta_id = context.user_data.get("alerta_id_remover")
    os_id = context.user_data.get("os_id_alerta")

    if not alerta_id or not os_id:
        await query.edit_message_text("❌ Erro: Não foi possível identificar o alerta.", reply_markup=get_main_menu_keyboard(update))
        return MENU
        
    # 1. Deleta o alerta
    await delete_alert(alerta_id)
    
    # 2. Atualiza a OS
    os_ref = get_os_ref(os_id)
    if os_ref:
        await asyncio.to_thread(os_ref.update, {
            "alerta": "Nenhum",
            "alerta_id": firestore.DELETE_FIELD, # Remove o campo alerta_id
        })
        
    message = f"✅ *Alerta da OS `{os_id}` removido com sucesso!*"
    
    await query.edit_message_text(
        message,
        reply_markup=get_main_menu_keyboard(update),
        parse_mode=ParseMode.MARKDOWN
    )

    context.user_data.pop("alerta_id_remover", None)
    context.user_data.pop("os_id_alerta", None)
    context.user_data.pop("alerta_action", None)
    return MENU

# --- Funções de Lembrete Manual (Agendamento avulso) ---

async def start_lembrete_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de agendamento de lembrete manual."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "⏰ *Agendamento de Lembrete Manual*\n"
        "Por favor, digite a *data e hora* para o lembrete (Ex: 01/12/2025 10:00):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_LEMBRETE_DATA

async def prompt_lembrete_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data/hora e pede a mensagem do lembrete manual."""
    
    # 1. Valida a data
    date_str = update.text.strip()
    try:
        due_date = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
        if due_date <= datetime.now():
             await update.message.reply_text(
                "❌ A data do lembrete deve ser *futura*. Tente novamente (Ex: 01/12/2025 10:00):",
                parse_mode=ParseMode.MARKDOWN
            )
             return PROMPT_LEMBRETE_DATA
             
        context.user_data["lembrete_due_date"] = due_date
    except ValueError:
        await update.message.reply_text(
            "❌ Formato de data/hora inválido. Use o formato *DD/MM/AAAA HH:MM* (Ex: 01/12/2025 10:00):",
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_LEMBRETE_DATA

    # 2. Pede a mensagem
    await update.message.reply_text(
        "Digite a *mensagem* para o lembrete (Ex: Pagar a conta de luz):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_LEMBRETE_MSG

async def save_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva o lembrete manual no Firestore."""
    
    lembrete_message = update.text.strip()
    due_date = context.user_data.get("lembrete_due_date")
    chat_id = update.effective_chat.id
    
    # Agendamos o alerta com um OS ID mock 'MANUAL'
    alert_id = await schedule_alert("MANUAL", chat_id, due_date, lembrete_message)
    
    message = (
        f"✅ *Lembrete Manual Agendado!* \n"
        f"Para: {due_date.strftime('%d/%m/%Y %H:%M')}\n"
        f"Mensagem: {lembrete_message}\n"
    )

    await update.message.reply_text(
        message,
        reply_markup=get_main_menu_keyboard(update),
        parse_mode=ParseMode.MARKDOWN
    )
    
    context.user_data.pop("lembrete_due_date", None)
    return MENU

# --- Exportação para PDF ---

async def exportar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Busca todas as OS e gera um PDF para envio."""
    query = update.callback_query
    await query.answer("Gerando PDF... Aguarde um momento.")

    if not PDF_PROCESSOR_AVAILABLE:
        await query.edit_message_text(
            "❌ *Recurso Indisponível.* Os módulos 'PyMuPDF' e/ou 'pandas' não estão instalados neste ambiente. Por favor, instale-os para usar esta função.",
            reply_markup=get_main_menu_keyboard(update),
            parse_mode=ParseMode.MARKDOWN
        )
        return MENU

    try:
        os_data = await fetch_all_os()
        if not os_data:
            await query.edit_message_text(
                "⚠️ Não há Ordens de Serviço (OS) cadastradas para exportar.",
                reply_markup=get_main_menu_keyboard(update),
                parse_mode=ParseMode.MARKDOWN
            )
            return MENU

        # Cria a lista de dicionários para o DataFrame
        data_for_df = []
        for os_id, data in os_data.items():
            data_for_df.append({
                'ID': os_id,
                'Descrição': data.get('descricao', 'N/A'),
                'Tipo': data.get('tipo', 'N/A'),
                'Status': data.get('status', 'N/A'),
                'Data Criação': data.get('data_criacao', 'N/A').split('T')[0], # Simplifica data
                'Alerta': data.get('alerta', 'Nenhum'),
            })

        df = pd.DataFrame(data_for_df)
        
        # --- Geração do PDF usando PyMuPDF (fitz) ---
        
        # 1. Configuração do Documento
        doc = fitz.open()
        page = doc.new_page(width=595, height=842) # A4 size
        
        # 2. Título
        page.insert_text((50, 50), "Relatório de Ordens de Serviço", fontsize=18, color=(0, 0, 0))
        page.insert_text((50, 75), f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}", fontsize=10, color=(0.5, 0.5, 0.5))
        
        # 3. Tabela (usando CSV temporário para layout simples)
        csv_buffer = io.StringIO()
        # Formata o CSV para que a descrição não tenha quebras de linha
        df.to_csv(csv_buffer, index=False, sep=';') 
        csv_text = csv_buffer.getvalue()
        
        text_y = 100
        text_x = 50
        
        # Desenha as linhas da tabela (simplificado, aqui apenas texto)
        header = csv_text.split('\n')[0].split(';')
        
        # Desenha cabeçalho
        col_widths = [100, 200, 80, 80, 80, 50]
        current_x = text_x
        for i, h in enumerate(header):
            page.insert_text((current_x, text_y), h, fontsize=9, color=(0, 0, 0), fontname="helv-bold")
            current_x += col_widths[i]

        text_y += 15
        
        # Desenha linhas de dados
        for line in csv_text.split('\n')[1:]:
            if not line.strip(): continue
            cols = line.split(';')
            current_x = text_x
            
            # Nova página se o texto exceder o limite
            if text_y > 800:
                page = doc.new_page(width=595, height=842)
                text_y = 50
                # Redesenha o cabeçalho na nova página
                current_x = text_x
                for i, h in enumerate(header):
                    page.insert_text((current_x, text_y), h, fontsize=9, color=(0, 0, 0), fontname="helv-bold")
                    current_x += col_widths[i]
                text_y += 15
                current_x = text_x
            
            for i, col_data in enumerate(cols):
                # Limita o tamanho do texto para caber na coluna
                display_text = col_data.replace('"', '').strip()
                if len(display_text) > col_widths[i] // 5: # Estimativa simples
                    display_text = display_text[:col_widths[i] // 5 - 3] + "..."
                    
                page.insert_text((current_x, text_y), display_text, fontsize=8, color=(0, 0, 0))
                current_x += col_widths[i]
            
            text_y += 15
        
        # 4. Salva o PDF no buffer
        pdf_buffer = io.BytesIO(doc.tobytes())
        pdf_buffer.seek(0)
        doc.close()

        # 5. Envia o PDF
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=InputFile(pdf_buffer, filename=f"relatorio_os_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"),
            caption="✅ Relatório de Ordens de Serviço exportado com sucesso."
        )
        
        await query.delete_message()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Escolha outra opção:",
            reply_markup=get_main_menu_keyboard(update)
        )

    except Exception as e:
        logger.error(f"Erro ao gerar ou enviar PDF: {e}")
        await query.edit_message_text(
            f"❌ Ocorreu um erro ao gerar o PDF. Detalhes: {e}",
            reply_markup=get_main_menu_keyboard(update)
        )

    return MENU

# --- Callbacks Genéricos ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gerencia callbacks que fazem transições de estado ou chamam funções auxiliares."""
    query = update.callback_query
    await query.answer()
    
    # Extrai o comando
    data = query.data
    
    if data == 'menu':
        return await start(update, context)

    # Fluxo de OS
    elif data == 'menu|nova_os':
        return await prompt_os_description(update, context)
    elif data == 'menu|ver_os':
        return await prompt_os_id(update, context, 'ver_os')
    elif data.startswith('ver_os_id|'):
        os_id = data.split('|')[1]
        os_ref = get_os_ref(os_id)
        if os_ref:
            os_data = await asyncio.to_thread(os_ref.get)
            if os_data.exists:
                os_data_dict = os_data.to_dict()
                os_data_dict["id"] = os_id
                return await show_os_details(Update(update_id=update.update_id, callback_query=query), context, os_data_dict)
        
        await query.edit_message_text(f"❌ OS com ID `{os_id}` não encontrada.", reply_markup=get_main_menu_keyboard(update), parse_mode=ParseMode.MARKDOWN)
        return MENU
        
    elif data == 'menu|atualizar_os':
        return await prompt_os_id(update, context, 'atualizar_os')
    elif data.startswith('update_start|'):
        os_id = data.split('|')[1]
        os_ref = get_os_ref(os_id)
        if os_ref:
            os_data = await asyncio.to_thread(os_ref.get)
            if os_data.exists:
                os_data_dict = os_data.to_dict()
                os_data_dict["id"] = os_id
                context.user_data["os_temp"] = os_data_dict
                return await prompt_update_field(Update(update_id=update.update_id, callback_query=query), context)
        
        await query.edit_message_text(f"❌ OS com ID `{os_id}` não encontrada.", reply_markup=get_main_menu_keyboard(update), parse_mode=ParseMode.MARKDOWN)
        return MENU
        
    elif data.startswith('update_field|'):
        return await prompt_new_value(update, context)
    elif data.startswith('update_value|'):
        return await handle_callback_update(update, context)

    elif data == 'menu|deletar_os':
        return await prompt_os_id(update, context, 'deletar_os')
    elif data.startswith('confirm_delete_'):
        return await finalize_delete_os(update, context)
        
    # Fluxo de Alerta
    elif data == 'menu|alerta_menu':
        return await alerta_menu(update, context)
    elif data == 'alerta_os_prompt' or data == 'remover_alerta_prompt':
        return await prompt_os_id_alerta(update, context)
    elif data.startswith('alerta_set|'):
        os_id = data.split('|')[1]
        os_ref = get_os_ref(os_id)
        if os_ref:
            os_data = await asyncio.to_thread(os_ref.get)
            if os_data.exists:
                context.user_data["os_id_alerta"] = os_id
                context.user_data["alerta_action"] = 'alerta_os_prompt'
                # Simula a transição como se o ID tivesse sido digitado
                return await update.callback_query.edit_message_text(
                    f"OS `{os_id}` encontrada. Digite a *data e hora* do alerta (Ex: 01/12/2025 10:00):",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancelar", callback_data='menu')]]),
                    parse_mode=ParseMode.MARKDOWN
                )
        
        await query.edit_message_text(f"❌ OS com ID `{os_id}` não encontrada.", reply_markup=get_main_menu_keyboard(update), parse_mode=ParseMode.MARKDOWN)
        return MENU
        
    elif data == 'confirm_remove_alerta':
        return await finalize_remove_alerta(update, context)

    # Fluxo de Lembrete Manual
    elif data == 'lembrete_manual_start':
        return await start_lembrete_manual(update, context)
        
    # Exportação
    elif data == 'menu|exportar_pdf':
        return await exportar_pdf(update, context)

    # Catch-all
    else:
        logger.warning(f"Callback não tratado: {data}")
        await query.edit_message_text("Opção inválida. Voltando ao menu principal.", reply_markup=get_main_menu_keyboard(update))
        return MENU

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Responde a comandos não reconhecidos em estados de conversa."""
    await update.message.reply_text(
        "Comando não reconhecido. Use /cancel ou escolha uma opção do menu.",
        reply_markup=get_main_menu_keyboard(update)
    )
    return MENU

# --- Tarefa de Agendamento (Job Queue) ---

async def alert_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verifica e dispara alertas pendentes do Firestore."""
    
    db = get_db()
    if db is None:
        # Se o Firebase falhou, apenas retorna
        return

    # Busca alertas pendentes com data de vencimento até o momento atual
    now_iso = datetime.now().isoformat()
    
    try:
        # Nota: O Firestore não permite consultas "menor ou igual a" em campos de data/hora
        # sem um índice, mas a busca por um grande intervalo é geralmente suficiente
        # Usamos uma consulta simples para buscar todos os 'pending' e filtramos em memória para precisão
        
        alerts_ref = db.collection("alertas")
        query = alerts_ref.where("status", "==", "pending").stream()

        for doc in query:
            alert_data = doc.to_dict()
            try:
                due_date = datetime.fromisoformat(alert_data["due_date"])
                
                if due_date <= datetime.now():
                    # Disparar o Alerta
                    chat_id = alert_data["chat_id"]
                    os_id = alert_data["os_id"]
                    message = alert_data["message"]
                    
                    if os_id == "MANUAL":
                        alert_text = f"⏰ *Lembrete Manual:* {message}"
                    else:
                        alert_text = (
                            f"🔔 *ALERTA OS:* Lembrete para a OS `{os_id}`.\n"
                            f"Mensagem: {message}"
                        )

                    await context.bot.send_message(
                        chat_id=chat_id, 
                        text=alert_text, 
                        parse_mode=ParseMode.MARKDOWN
                    )

                    # Atualizar o status para 'sent' no Firestore
                    await asyncio.to_thread(doc.reference.update, {"status": "sent", "sent_at": now_iso})
                    
            except Exception as e:
                logger.error(f"Erro ao processar alerta {doc.id}: {e}")
                
    except Exception as e:
        logger.error(f"Erro geral no job de alerta: {e}")

# --- Função de Manutenção (Keep Alive) ---

async def keep_alive_ping(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia um ping periódico para manter o serviço Render ativo."""
    
    # Use a URL externa para o ping
    ping_url = WEBHOOK_URL
    if not ping_url:
        logger.warning("WEBHOOK_URL/RENDER_EXTERNAL_URL não definido. Não é possível enviar ping de keep-alive.")
        return

    try:
        # Usamos aiohttp para uma requisição assíncrona leve
        async with aiohttp.ClientSession() as session:
            # Pingamos a URL base (Render)
            async with session.get(ping_url) as response:
                if response.status not in (200, 404): # 404 é normal se a rota base não existir
                    logger.warning(f"Keep-Alive: Ping para {ping_url} retornou status {response.status}")
                # else: Sucesso silencioso
    except Exception as e:
        logger.error(f"Keep-Alive: Falha ao enviar ping para {ping_url}: {e}")

# --- Função Principal ---

def run_bot():
    """Configura e executa o bot em modo Webhook ou Long Polling."""

    if not TOKEN:
        logger.error("TOKEN do Telegram não encontrado. Defina a variável TELEGRAM_TOKEN.")
        return
        
    # 1. Cria a Aplicação
    application = Application.builder().token(TOKEN).build()
    
    # 2. Configuração do Job Queue (Agendamento)
    # Executa o job de alerta a cada 1 minuto
    application.job_queue.run_repeating(alert_job, interval=timedelta(minutes=1), first=1) 
    
    # Executa o job de Keep Alive a cada 10 minutos (pode ser ajustado)
    application.job_queue.run_repeating(keep_alive_ping, interval=timedelta(minutes=10), first=1)

    # 3. Configuração do Conversation Handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(callback_handler, pattern='^menu|'),
                CallbackQueryHandler(callback_handler, pattern='^lembrete_manual_start$'),
            ],
            PROMPT_DESCRICAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_description),
            ],
            PROMPT_TIPO: [
                CallbackQueryHandler(callback_handler, pattern='^os_tipo|'),
            ],
            PROMPT_OS: [ # Para ver, atualizar, deletar
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_os_id),
            ],
            PROMPT_ATUALIZACAO: [
                CallbackQueryHandler(callback_handler, pattern='^update_field|'), # Pede novo valor
                CallbackQueryHandler(callback_handler, pattern='^update_value|'), # Recebe valor (Status/Tipo)
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_and_save_update), # Recebe valor (Descrição)
            ],
            PROMPT_ALERTA: [
                CallbackQueryHandler(callback_handler, pattern='^alerta_os_prompt$'),
                CallbackQueryHandler(callback_handler, pattern='^remover_alerta_prompt$'),
                CallbackQueryHandler(callback_handler, pattern='^lembrete_manual_start$'), # Volta para o fluxo manual
            ],
            PROMPT_ID_ALERTA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_alerta_data),
                CallbackQueryHandler(callback_handler, pattern='^confirm_remove_alerta$'), # Confirma remoção
                CallbackQueryHandler(callback_handler, pattern='^alerta_menu$'),
            ],
            PROMPT_INCLUSAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_descricao), # Recebe Data/Hora
            ],
            PROMPT_TIPO_INCLUSAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_os_alerta), # Recebe Mensagem
            ],
            # Fluxo de Lembrete Manual
            PROMPT_LEMBRETE_DATA: [ # Recebe a data
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_msg),
            ],
            PROMPT_LEMBRETE_MSG: [ # Recebe a mensagem
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_lembrete),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.COMMAND, fallback_command),
            CallbackQueryHandler(callback_handler, pattern='^menu$'), # Última chance para voltar ao menu
        ],
    )

    # Adiciona o ConversationHandler e o start
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start)) 
    
    # 4. Configuração do Webhook
    if WEBHOOK_URL:
        # CORREÇÃO CRÍTICA PARA O WEBHOOK:
        # A URL deve ser resolúvel publicamente. Se a configuração estiver correta 
        # (WEBHOOK_URL = domínio do Render), o set_webhook funcionará.
        full_webhook_url = WEBHOOK_URL + WEBHOOK_PATH
        
        try:
            logger.info(f"A iniciar Webhook em http://0.0.0.0:{PORT}{WEBHOOK_PATH}")
            logger.info(f"URL completa do Webhook enviada ao Telegram: {full_webhook_url}")
            
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TOKEN, 
                webhook_url=full_webhook_url, 
            )
            logger.info(f"Servidor Webhook iniciado e escutando na porta {PORT}.")
            logger.info(f"Webhook URL configurada com sucesso no Telegram: {full_webhook_url}")

        except Exception as e:
            logger.error(f"Falha ao iniciar o Webhook: {e}")
            logger.info("Tentando modo Long Polling (desativar para produção em infraestrutura Webhook).")
            # Fallback para Long Polling
            application.run_polling(allowed_updates=Update.ALL_TYPES)
    else:
        # Se WEBHOOK_URL não estiver configurado, usa Long Polling
        logger.info("WEBHOOK_URL/RENDER_EXTERNAL_URL não configurado. Iniciando em modo Long Polling.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_bot()
