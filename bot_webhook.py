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
    JobQueue,
)
from telegram.constants import ParseMode, ChatAction

# --- Configuração ---

# Carrega variáveis de ambiente (do arquivo .env, se existir)
load_dotenv()

# Obtém variáveis de ambiente (o TOKEN e WEBHOOK_URL são obrigatórios)
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # Ex: https://sua-app.exemplo.com
WEBHOOK_PATH = "/" + TOKEN # Caminho do webhook
PORT = int(os.getenv("PORT", 8080)) # Porta para o webhook

if not TOKEN or not WEBHOOK_URL:
    # Se estiver rodando em ambiente local de teste, esta verificação pode ser relaxada
    # mas é crucial para o Webhook.
    logging.error("Variáveis de ambiente TELEGRAM_TOKEN e WEBHOOK_URL devem estar definidas.")

# Habilita o logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Reduz o nível de log de bibliotecas externas
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("firebase_admin").setLevel(logging.WARNING)
logging.getLogger("google.auth").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Estados para o ConversationHandler
MENU, PROMPT_OS, PROMPT_DESCRICAO, PROMPT_TIPO, PROMPT_STATUS, PROMPT_ATUALIZACAO, PROMPT_ALERTA, PROMPT_INCLUSAO, PROMPT_ID_ALERTA, PROMPT_TIPO_INCLUSAO, LEMBRETE_MENU, PROMPT_ID_LEMBRETE, PROMPT_LEMBRETE_DATA, PROMPT_LEMBRETE_MSG = range(14)

# --- Firebase Init ---

# Inicializa o Firebase (Prioriza variável de ambiente JSON ou fallback para arquivo local)
try:
    # Tenta carregar as credenciais diretamente de uma variável de ambiente JSON
    firebase_json_str = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if firebase_json_str:
        cred_dict = json.loads(firebase_json_str)
        cred = credentials.Certificate(cred_dict)
        logger.info("Credenciais do Firebase carregadas de FIREBASE_CREDENTIALS_JSON.")
    else:
        # Tenta carregar o arquivo local app.json
        cred = credentials.Certificate("app.json")
        logger.info("Credenciais do Firebase carregadas de app.json (caminho local).")
    
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase inicializado com sucesso.")

except Exception as e:
    logger.error(f"Falha ao inicializar o Firebase: {e}")
    # Cria um mock db para evitar crashes caso o Firebase falhe na inicialização
    class MockDB:
        def collection(self, *args): return self
        def document(self, *args): return self
        def get(self): return type('MockDoc', (object,), {'exists': False, 'to_dict': lambda: {}})()
        def stream(self): return []
        def where(self, *args): return self
        def order_by(self, *args): return self
        def limit(self, *args): return self
        def set(self, *args): pass
        def update(self, *args): pass
        def delete(self): pass
        def add(self, *args): return (None, type('MockDocRef', (object,), {'id': 'mock-id'})())
    db = MockDB()
    logger.warning("Usando banco de dados mock. Funcionalidade de persistência está desativada.")
    
# --- Utils Firebase ---

def get_os_ref(os_id: str):
    """Retorna a referência do documento OS no Firestore."""
    return db.collection("ordens_servico").document(os_id)

def get_alertas_ref():
    """Retorna a referência da coleção de Alertas/Lembretes no Firestore."""
    return db.collection("alertas_lembretes")

def get_all_os():
    """Retorna todas as OS ativas (status != 'Concluída')."""
    try:
        # Nota: O Firestore não permite consultas em diferentes campos em '!=', '!in' ou 'not_in'.
        docs = db.collection("ordens_servico").where("status", "!=", "Concluída").stream()
        return [doc.to_dict() | {"id": doc.id} for doc in docs]
    except Exception as e:
        logger.error(f"Erro ao buscar todas as OS: {e}")
        return []

def get_os_by_id(os_id: str):
    """Retorna uma OS pelo ID."""
    try:
        doc = get_os_ref(os_id).get()
        if doc.exists:
            return doc.to_dict() | {"id": doc.id}
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar OS {os_id}: {e}")
        return None

# --- Utils de Formatação ---

def format_os_details(os_data: dict) -> str:
    """Formata os detalhes de uma OS para exibição."""
    os_id = os_data.get('id', 'N/A')
    data_criacao = os_data.get('data_criacao', 'N/A')
    
    # Formatar timestamp para string legível
    data_str = "N/A"
    if isinstance(data_criacao, datetime):
        data_str = data_criacao.strftime("%d/%m/%Y %H:%M")
    elif data_criacao and hasattr(data_criacao, 'seconds'): # Firestore Timestamp object
        data_str = datetime.fromtimestamp(data_criacao.seconds).strftime("%d/%m/%Y %H:%M")
    else:
        data_str = str(data_criacao)
        
    alerta_info = ""
    alerta_data = os_data.get('alerta_data')
    alerta_desc = os_data.get('alerta_descricao')
    
    if alerta_data:
        alerta_dt_obj = datetime.fromtimestamp(alerta_data.seconds)
        alerta_info = f"\n🚨 *Alerta Agendado:*\n`{alerta_dt_obj.strftime('%d/%m/%Y %H:%M')}`\n*Descrição Alerta:* {alerta_desc or 'N/A'}"
        
    message = (
        f"🛠️ *Detalhes da Ordem de Serviço*\n\n"
        f"🔗 *ID:* `{os_id}`\n"
        f"📝 *Descrição:* {os_data.get('descricao', 'N/A')}\n"
        f"🏷️ *Tipo:* `{os_data.get('tipo', 'N/A')}`\n"
        f"🟢 *Status:* *{os_data.get('status', 'N/A')}*\n"
        f"📅 *Data Criação:* `{data_str}`\n"
        f"👤 *Criado Por (Chat ID):* `{os_data.get('user_id', 'N/A')}`\n"
        f"{alerta_info}"
    )
    return message

def parse_time_input(time_str: str) -> datetime | None:
    """Tenta analisar a string de data/hora (DD/MM/YYYY HH:MM ou formato relativo)."""
    time_str = time_str.strip().lower()
    now = datetime.now()

    # 1. Formato absoluto DD/MM/YYYY HH:MM
    try:
        return datetime.strptime(time_str, "%d/%m/%Y %H:%M")
    except ValueError:
        pass

    # 2. Formato relativo (e.g., "+3h", "1d", "amanha 10:00")
    if time_str.startswith('+'):
        match = re.match(r"\+(\d+)([hdm])", time_str)
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            if unit == 'h':
                return now + timedelta(hours=value)
            elif unit == 'd':
                return now + timedelta(days=value)
            elif unit == 'm':
                return now + timedelta(minutes=value)
    elif time_str.startswith('amanha'):
        try:
            time_part = time_str.split(' ')[1]
            hours, minutes = map(int, time_part.split(':'))
            tomorrow = now + timedelta(days=1)
            # Retorna a data de amanhã com a hora especificada
            return tomorrow.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        except Exception:
            pass

    return None

# --- Handlers Básicos ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a conversa, exibe o menu principal."""
    # Garante que o usuário esteja logado ou em um contexto de chat
    if update.effective_user:
        user = update.effective_user
        if update.message:
            await update.message.reply_chat_action(ChatAction.TYPING)
            await update.message.reply_text(
                f"Olá, {user.first_name}! Bem-vindo ao Gestor de Ordens de Serviço (OS).",
                reply_markup=menu_keyboard()
            )
        elif update.callback_query:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Menu Principal:",
                reply_markup=menu_keyboard()
            )
        
    return MENU

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handler para o botão 'Voltar ao Menu'."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Menu Principal:",
        reply_markup=menu_keyboard()
    )
    return MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a conversa atual (ConversationHandler)."""
    if update.message:
        await update.message.reply_text(
            'Operação cancelada. Voltando ao Menu Principal.', 
            reply_markup=menu_keyboard()
        )
    # Limpa estados temporários
    context.user_data.clear()
    return MENU

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Responde a comandos não reconhecidos."""
    if update.message:
        await update.message.reply_text(
            "Comando não reconhecido. Por favor, use um dos botões do menu ou /cancel."
        )
    return MENU

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Trata todos os CallbackQueries que não são específicos de outros handlers."""
    query = update.callback_query
    data = query.data
    
    # Lógica de Navegação
    if data == 'menu':
        await menu_callback(update, context)
        return MENU
    
    # Lógica de Ações de OS (View, Update, Delete vindo do menu de listagem)
    if data.startswith('view_os_') or data.startswith('update_os_') or data.startswith('delete_os_'):
        try:
            _, action, os_id = data.split('_')
        except ValueError:
            logger.error(f"Callback data inválido: {data}")
            await query.answer("Erro ao processar a ação.")
            return MENU

        if action == 'view':
            await view_os_details(update, context, os_id)
            return MENU
        elif action == 'update':
            await prompt_update_status(update, context, os_id)
            return PROMPT_STATUS
        elif action == 'delete':
            await delete_os_confirm(update, context, os_id)
            return MENU

    # Lógica de Submenus e Ações
    elif data == 'gerenciar_alertas_menu':
        await gerenciar_alertas_menu(update, context)
        return PROMPT_ALERTA
    elif data == 'remover_alerta_menu':
        await remover_alerta_menu_start(update, context)
        return PROMPT_ID_ALERTA
    elif data == 'alerta_existente':
        await query.answer("Você deve digitar um ID de OS válido ou voltar ao menu.")
        return None
    elif data == 'lembrete_menu':
        await lembrete_menu(update, context)
        return LEMBRETE_MENU
    elif data == 'lembrete_manual_start':
        # O fluxo de lembrete manual agora começa pedindo a data (PROMPT_LEMBRETE_DATA)
        await prompt_lembrete_os_id(update, context)
        return PROMPT_LEMBRETE_DATA
    elif data == 'exportar_pdf':
        await exportar_os_para_pdf(update, context)
        return MENU
    elif data.startswith('confirm_delete_'):
        await confirm_delete_os(update, context)
        return MENU
        
    return None

# --- Teclados ---

def menu_keyboard() -> InlineKeyboardMarkup:
    """Teclado do menu principal."""
    keyboard = [
        [InlineKeyboardButton("➕ Nova OS", callback_data="criar_os")],
        [InlineKeyboardButton("👀 Ver OS Ativas", callback_data="ver_ativas")],
        [InlineKeyboardButton("✏️ Atualizar Status OS", callback_data="atualizar_status_os")],
        [InlineKeyboardButton("⏰ Gerenciar Alertas OS", callback_data="gerenciar_alertas_menu")],
        [InlineKeyboardButton("🗓 Agendar Lembrete Manual", callback_data="lembrete_menu")],
        [InlineKeyboardButton("📤 Exportar para PDF", callback_data="exportar_pdf")],
    ]
    return InlineKeyboardMarkup(keyboard)

def os_actions_keyboard(os_id: str) -> InlineKeyboardMarkup:
    """Teclado com ações para uma OS específica (após visualização)."""
    keyboard = [
        [
            InlineKeyboardButton("✏️ Atualizar Status", callback_data=f"update_os_{os_id}"),
            InlineKeyboardButton("❌ Excluir", callback_data=f"delete_os_{os_id}"),
        ],
        [InlineKeyboardButton("⏰ Gerenciar Alerta", callback_data=f"gerenciar_alerta_{os_id}")],
        [InlineKeyboardButton("↩️ Voltar ao Menu", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def status_options_keyboard(os_id: str) -> InlineKeyboardMarkup:
    """Teclado com as opções de status."""
    keyboard = [
        [
            InlineKeyboardButton("Pendente", callback_data=f"set_status_{os_id}_Pendente"),
            InlineKeyboardButton("Em Andamento", callback_data=f"set_status_{os_id}_Em Andamento"),
        ],
        [
            InlineKeyboardButton("Aguardando Peças", callback_data=f"set_status_{os_id}_Aguardando Peças"),
            InlineKeyboardButton("Concluída", callback_data=f"set_status_{os_id}_Concluída"),
        ],
        [InlineKeyboardButton("↩️ Voltar ao Menu", callback_data="menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def alerta_options_keyboard(os_id: str) -> InlineKeyboardMarkup:
    """Teclado de opções de alerta (incluir/remover)."""
    keyboard = [
        [InlineKeyboardButton("➕ Incluir Alerta", callback_data=f"incluir_alerta_{os_id}")],
        [InlineKeyboardButton("➖ Remover Alerta", callback_data=f"remover_alerta_{os_id}")],
        [InlineKeyboardButton("↩️ Voltar ao Menu", callback_data="menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Fluxo de Criação de OS ---

async def criar_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de criação de OS, pedindo a descrição."""
    query = update.callback_query
    await query.answer()
    
    # Usa o ID do chat para enviar mensagens futuras
    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    
    await query.edit_message_text(
        "Certo. Por favor, **digite a descrição completa** da nova Ordem de Serviço (OS):",
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data['os_data'] = {'user_id': user_id, 'chat_id': chat_id}
    return PROMPT_DESCRICAO

async def receive_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição e pede o tipo de OS."""
    context.user_data['os_data']['descricao'] = update.message.text.strip()
    
    keyboard = [
        [InlineKeyboardButton("Instalação", callback_data="tipo_Instalação")],
        [InlineKeyboardButton("Manutenção", callback_data="tipo_Manutenção")],
        [InlineKeyboardButton("Configuração", callback_data="tipo_Configuração")],
        [InlineKeyboardButton("Outro", callback_data="tipo_Outro")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Descrição recebida. Agora, selecione o *Tipo* de serviço:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_TIPO

async def receive_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o tipo e salva a OS com status 'Pendente', voltando ao menu."""
    query = update.callback_query
    await query.answer()
    
    try:
        tipo = query.data.split('_')[1]
    except IndexError:
        await query.edit_message_text("❌ Opção de tipo inválida. Voltando ao menu.", reply_markup=menu_keyboard())
        context.user_data.clear()
        return MENU
        
    context.user_data['os_data']['tipo'] = tipo
    context.user_data['os_data']['status'] = "Pendente"
    context.user_data['os_data']['data_criacao'] = firestore.SERVER_TIMESTAMP

    try:
        # Salva no Firestore
        doc_ref = db.collection("ordens_servico").add(context.user_data['os_data'])[1]
        os_id = doc_ref.id
        
        await query.edit_message_text(
            f"✅ *OS Criada com Sucesso!*\n\nID da OS: `{os_id}`\nTipo: {tipo}\nStatus: Pendente",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Erro ao salvar OS no Firebase: {e}")
        await query.edit_message_text(
            "❌ *Erro ao criar a OS*. Tente novamente.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    
    context.user_data.clear()
    return MENU

# --- Fluxo de Visualização e Atualização de OS ---

async def ver_ativas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exibe uma lista de todas as OS ativas (não concluídas)."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("👀 Buscando Ordens de Serviço Ativas...", reply_markup=menu_keyboard())
    
    ordens = get_all_os()
    
    if not ordens:
        await query.edit_message_text(
            "🎉 Nenhuma Ordem de Serviço Ativa encontrada.",
            reply_markup=menu_keyboard()
        )
        return MENU

    # Cria botões para cada OS
    keyboard = []
    for os in ordens:
        descricao_curta = os.get('descricao', 'Sem Descrição')[:50].replace('\n', ' ')
        button_text = f"🔗 {os['id']} | {os['status']} | {descricao_curta}..."
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_os_{os['id']}")])
        
    keyboard.append([InlineKeyboardButton("↩️ Voltar ao Menu", callback_data="menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🛠️ *Ordens de Serviço Ativas* (clique para ver detalhes/editar):",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return MENU

async def view_os_details(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str):
    """Exibe os detalhes de uma OS."""
    query = update.callback_query
    await query.answer()
    
    os_data = get_os_by_id(os_id)
    
    if not os_data:
        await query.edit_message_text(
            f"❌ OS com ID `{os_id}` não encontrada.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return MENU

    message = format_os_details(os_data)
    
    await query.edit_message_text(
        message,
        reply_markup=os_actions_keyboard(os_id),
        parse_mode=ParseMode.MARKDOWN
    )
    
async def atualizar_status_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de atualização de status, pedindo o ID da OS."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Certo. Por favor, **digite o ID** da Ordem de Serviço (OS) que deseja atualizar o status:",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_OS

async def prompt_update_status(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str | None = None) -> int:
    """Recebe o ID da OS e pede o novo status."""
    if os_id is None:
        # Veio do fluxo de texto (PROMPT_OS)
        if update.message:
            os_id = update.message.text.strip()
        else:
            return PROMPT_OS
        
    context.user_data['target_os_id'] = os_id
    
    os_data = get_os_by_id(os_id)
    
    if not os_data:
        msg = f"❌ OS com ID `{os_id}` não encontrada. Digite um ID válido ou /cancel."
        if update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return PROMPT_OS
        elif update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
            return MENU

    msg = (
        f"OS encontrada (ID: `{os_id}`, Status Atual: *{os_data['status']}*).\n\n"
        f"Selecione o novo Status:"
    )

    if update.message:
        await update.message.reply_text(
            msg,
            reply_markup=status_options_keyboard(os_id),
            parse_mode=ParseMode.MARKDOWN
        )
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            msg,
            reply_markup=status_options_keyboard(os_id),
            parse_mode=ParseMode.MARKDOWN
        )
        
    return PROMPT_STATUS

async def set_new_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Atualiza o status da OS no Firebase."""
    query = update.callback_query
    await query.answer()
    
    try:
        _, _, os_id, new_status = query.data.split('_')
    except ValueError:
        await query.edit_message_text("❌ Erro ao processar a mudança de status. Tente novamente.", reply_markup=menu_keyboard())
        context.user_data.clear()
        return MENU
    
    try:
        get_os_ref(os_id).update({"status": new_status, "data_atualizacao": firestore.SERVER_TIMESTAMP})
        
        await query.edit_message_text(
            f"✅ Status da OS `{os_id}` atualizado para: *{new_status}*.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Erro ao atualizar status da OS {os_id}: {e}")
        await query.edit_message_text(
            f"❌ Erro ao atualizar status da OS `{os_id}`. Tente novamente.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        
    context.user_data.clear()
    return MENU

# --- Fluxo de Eliminação de OS ---

async def delete_os_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str) -> int:
    """Exibe confirmação para exclusão de OS."""
    query = update.callback_query
    await query.answer()
    
    os_data = get_os_by_id(os_id)
    if not os_data:
        await query.edit_message_text(
            f"❌ OS com ID `{os_id}` não encontrada.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return MENU
        
    keyboard = [
        [InlineKeyboardButton("⚠️ CONFIRMAR EXCLUSÃO", callback_data=f"confirm_delete_{os_id}")],
        [InlineKeyboardButton("↩️ Voltar ao Menu (Cancelar)", callback_data="menu")]
    ]
    
    await query.edit_message_text(
        f"⚠️ *Confirmação de Exclusão*\n\n"
        f"Você tem certeza que deseja excluir a OS de ID `{os_id}`? Esta ação é irreversível.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return MENU

async def confirm_delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Exclui a OS no Firebase."""
    query = update.callback_query
    await query.answer()
    
    try:
        _, _, os_id = query.data.split('_')
    except ValueError:
        await query.edit_message_text("❌ Erro ao processar a exclusão. Tente novamente.", reply_markup=menu_keyboard())
        return MENU
    
    try:
        # 1. Excluir Alertas/Lembretes relacionados (se houver)
        alertas_query = get_alertas_ref().where('os_id', '==', os_id).stream()
        for alerta in alertas_query:
            alerta.reference.delete()
            
        # 2. Excluir a OS principal
        get_os_ref(os_id).delete()
        
        await query.edit_message_text(
            f"✅ OS `{os_id}` e alertas relacionados excluídos com sucesso.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Erro ao excluir OS {os_id}: {e}")
        await query.edit_message_text(
            f"❌ Erro ao excluir OS `{os_id}`. Tente novamente.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        
    return MENU

# --- Fluxo de Alertas da OS ---

async def gerenciar_alertas_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menu para gestão de alertas associados a OS."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Digite o *ID* da Ordem de Serviço (OS) para gerenciar alertas:",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ALERTA # Vai para o estado para receber o ID da OS

async def select_alerta_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o ID da OS e exibe opções de alerta (incluir/remover)."""
    os_id = update.message.text.strip()
    context.user_data['target_os_id'] = os_id
    
    os_data = get_os_by_id(os_id)
    
    if not os_data:
        await update.message.reply_text(
            f"❌ OS com ID `{os_id}` não encontrada. Digite um ID válido ou /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ALERTA
        
    context.user_data['os_data'] = os_data
    
    msg = format_os_details(os_data)
    
    await update.message.reply_text(
        f"{msg}\n\n*O que deseja fazer com o alerta desta OS?*",
        reply_markup=alerta_options_keyboard(os_id),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_TIPO_INCLUSAO

async def prompt_incluir_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede a data/hora para o alerta."""
    query = update.callback_query
    await query.answer()
    
    try:
        _, _, os_id = query.data.split('_')
    except ValueError:
        os_id = context.user_data.get('target_os_id', 'N/A')
        
    context.user_data['target_os_id'] = os_id
    
    await query.edit_message_text(
        f"Você está definindo um alerta para a OS `{os_id}`.\n\n"
        f"Por favor, **digite a data e hora do alerta** no formato *DD/MM/YYYY HH:MM* "
        f"ou em formato relativo (ex: `+3h`, `1d`, `amanha 09:00`):",
        parse_mode=ParseMode.MARKDOWN
    )
    # Próximo estado: receber data/hora
    # Reutiliza PROMPT_INCLUSAO para o fluxo de data -> descrição
    return PROMPT_INCLUSAO

async def receive_alerta_data_for_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data do alerta e pede a descrição/mensagem."""
    data_str = update.message.text.strip()
    alerta_dt = parse_time_input(data_str)
    
    if not alerta_dt or alerta_dt <= datetime.now():
        await update.message.reply_text(
            "❌ *Data/hora inválida ou no passado*. Por favor, digite uma data/hora futura válida (DD/MM/YYYY HH:MM ou relativo):",
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_INCLUSAO
        
    context.user_data['alerta_dt'] = alerta_dt
    
    await update.message.reply_text(
        f"Data/Hora de alerta definida para: `{alerta_dt.strftime('%d/%m/%Y %H:%M')}`.\n\n"
        f"Agora, **digite a descrição/mensagem** do alerta (ex: 'Ligar para o cliente' ou 'Revisar estoque'):",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_INCLUSAO # Permanece no mesmo estado para receber a descrição

async def receive_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição do alerta e salva tudo no Firestore."""
    
    # Esta função agora precisa diferenciar se está recebendo a data ou a descrição.
    # O fluxo é: PROMPT_INCLUSAO (data) -> PROMPT_INCLUSAO (descrição)
    
    # 1. Verifica se a data do alerta já está no contexto
    if 'alerta_dt' not in context.user_data:
        # Se não tem data, o usuário enviou a data agora. Processa como data.
        return await receive_alerta_data_for_descricao(update, context)
        
    # 2. Se a data está no contexto, o usuário enviou a descrição. Processa como descrição.
    os_id = context.user_data.get('target_os_id')
    alerta_dt = context.user_data.get('alerta_dt')
    descricao = update.message.text.strip()
    user_id = update.effective_user.id
    
    if not os_id or not alerta_dt:
        await update.message.reply_text("❌ Erro interno: Dados do alerta perdidos. Tente novamente ou /cancel.", reply_markup=menu_keyboard())
        context.user_data.clear()
        return MENU
        
    try:
        # Salva Alerta na coleção de Alertas/Lembretes
        alerta_data = {
            'os_id': os_id,
            'user_id': str(user_id),
            'chat_id': update.effective_chat.id,
            'data_alerta': alerta_dt,
            'descricao': descricao,
            'tipo': 'alerta_os',
            'ativo': True,
            'data_criacao': firestore.SERVER_TIMESTAMP,
        }
        alerta_ref = get_alertas_ref().add(alerta_data)[1]
        
        # Atualiza a OS principal com a nova referência de alerta
        get_os_ref(os_id).update({
            'alerta_data': alerta_dt,
            'alerta_descricao': descricao,
            'alerta_ref_id': alerta_ref.id
        })

        await update.message.reply_text(
            f"✅ *Alerta Agendado com Sucesso! (ID: {alerta_ref.id})*\n\n"
            f"OS ID: `{os_id}`\n"
            f"Data: `{alerta_dt.strftime('%d/%m/%Y %H:%M')}`\n"
            f"Mensagem: {descricao}",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Erro ao salvar alerta no Firebase: {e}")
        await update.message.reply_text(
            "❌ Erro ao agendar o alerta. Tente novamente.",
            reply_markup=menu_keyboard()
        )
        
    context.user_data.clear()
    return MENU

async def remover_alerta_menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede o ID da OS para remover o alerta."""
    query = update.callback_query
    await query.answer()
    
    # Se o callback veio de 'gerenciar_alerta_<os_id>', usa esse ID
    if 'gerenciar_alerta_' in query.data:
        _, _, os_id = query.data.split('_')
        context.user_data['target_os_id'] = os_id
        await query.edit_message_text(
            f"Deseja remover o alerta da OS `{os_id}`?\n\n"
            f"Se sim, confirme digitando **o ID da OS** novamente. Caso contrário, /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ID_ALERTA
    
    # Se veio do menu principal de alertas
    await query.edit_message_text(
        "Digite o *ID* da Ordem de Serviço (OS) para remover o alerta:",
        parse_mode=ParseMode.MARKDOWN
    )
        
    return PROMPT_ID_ALERTA

async def receive_alerta_prazo_or_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o ID da OS para remoção do alerta e confirma/executa."""
    os_id = update.message.text.strip()
    
    os_data = get_os_by_id(os_id)
    
    if not os_data:
        await update.message.reply_text(
            f"❌ OS com ID `{os_id}` não encontrada. Digite um ID válido ou /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_ID_ALERTA
        
    alerta_ref_id = os_data.get('alerta_ref_id')

    if not alerta_ref_id:
        await update.message.reply_text(
            f"🤷‍♂️ A OS `{os_id}` não possui um alerta ativo associado. Voltando ao menu.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.clear()
        return MENU

    try:
        # 1. Excluir o documento de alerta
        get_alertas_ref().document(alerta_ref_id).delete()
        
        # 2. Remover campos de alerta da OS principal
        get_os_ref(os_id).update({
            'alerta_data': firestore.DELETE_FIELD,
            'alerta_descricao': firestore.DELETE_FIELD,
            'alerta_ref_id': firestore.DELETE_FIELD
        })
        
        await update.message.reply_text(
            f"✅ Alerta associado à OS `{os_id}` removido com sucesso.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Erro ao remover alerta da OS {os_id}: {e}")
        await update.message.reply_text(
            f"❌ Erro ao remover alerta da OS `{os_id}`. Tente novamente.",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        
    context.user_data.clear()
    return MENU

# --- Fluxo de Lembrete Manual ---

async def lembrete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menu para lembrete manual."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🗓 Agendar Novo Lembrete", callback_data="lembrete_manual_start")],
        [InlineKeyboardButton("↩️ Voltar ao Menu", callback_data="menu")]
    ]
    
    await query.edit_message_text(
        "Você pode agendar um lembrete manual que será enviado para você neste chat.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LEMBRETE_MENU

async def prompt_lembrete_os_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pede a data/hora para o lembrete manual."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        f"Por favor, **digite a data e hora do lembrete** no formato *DD/MM/YYYY HH:MM* "
        f"ou em formato relativo (ex: `+3h`, `1d`, `amanha 09:00`):",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_LEMBRETE_DATA # Próximo estado: receber data/hora

async def prompt_lembrete_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data e pede a mensagem."""
    data_str = update.message.text.strip()
    lembrete_dt = parse_time_input(data_str)
    
    if not lembrete_dt or lembrete_dt <= datetime.now():
        await update.message.reply_text(
            "❌ *Data/hora inválida ou no passado*. Por favor, digite uma data/hora futura válida (DD/MM/YYYY HH:MM ou relativo):",
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_LEMBRETE_DATA
        
    context.user_data['lembrete_dt'] = lembrete_dt
    
    await update.message.reply_text(
        f"Data/Hora de lembrete definida para: `{lembrete_dt.strftime('%d/%m/%Y %H:%M')}`.\n\n"
        f"Agora, **digite a mensagem** do seu lembrete:",
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_LEMBRETE_MSG # Próximo estado: receber a mensagem

async def save_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a mensagem e salva o lembrete manual no Firestore."""
    
    # 1. Obter dados do contexto
    lembrete_dt = context.user_data.get('lembrete_dt')
    mensagem = update.message.text.strip()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if not lembrete_dt:
        await update.message.reply_text("❌ Erro interno: Data do lembrete perdida. Tente novamente ou /cancel.", reply_markup=menu_keyboard())
        context.user_data.clear()
        return MENU
        
    try:
        # 2. Salvar Lembrete na coleção de Alertas/Lembretes
        lembrete_data = {
            'os_id': None, # Lembrete manual não tem OS associada
            'user_id': str(user_id),
            'chat_id': chat_id, # ID do chat para onde enviar o lembrete
            'data_alerta': lembrete_dt,
            'descricao': mensagem,
            'tipo': 'lembrete_manual', # Diferencia de alertas de OS
            'ativo': True,
            'data_criacao': firestore.SERVER_TIMESTAMP,
        }
        lembrete_ref = get_alertas_ref().add(lembrete_data)[1]

        await update.message.reply_text(
            f"✅ *Lembrete Manual Agendado com Sucesso! (ID: {lembrete_ref.id})*\n\n"
            f"Data: `{lembrete_dt.strftime('%d/%m/%Y %H:%M')}`\n"
            f"Mensagem: {mensagem}",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Erro ao salvar lembrete manual no Firebase: {e}")
        await update.message.reply_text(
            "❌ Erro ao agendar o lembrete. Tente novamente.",
            reply_markup=menu_keyboard()
        )
        
    context.user_data.clear()
    return MENU

# --- Fluxo de Exportação para PDF ---

async def exportar_os_para_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gera um PDF com o status atual das OS e envia para o usuário."""
    query = update.callback_query
    await query.answer("Gerando relatório em PDF. Por favor, aguarde...")

    if not PDF_PROCESSOR_AVAILABLE:
        await query.edit_message_text(
            "❌ *Recurso indisponível*.\n\n"
            "Os módulos `PyMuPDF` (`fitz`) e `pandas` não estão instalados. "
            "Para ativar este recurso, instale as dependências (`requirements.txt`).",
            reply_markup=menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return MENU
        
    try:
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        
        # 1. Busca dados
        ordens = get_all_os()
        
        if not ordens:
            await query.edit_message_text(
                "Nenhuma OS Ativa para exportar.",
                reply_markup=menu_keyboard()
            )
            return MENU
            
        # Prepara dados para o DataFrame
        data_for_df = []
        for os in ordens:
            # Converte data_criacao do Firestore Timestamp (ou datetime) para string
            data_criacao_ts = os.get('data_criacao')
            if hasattr(data_criacao_ts, 'seconds'):
                data_criacao_str = datetime.fromtimestamp(data_criacao_ts.seconds).strftime("%d/%m/%Y %H:%M")
            elif isinstance(data_criacao_ts, datetime):
                data_criacao_str = data_criacao_ts.strftime("%d/%m/%Y %H:%M")
            else:
                data_criacao_str = "N/A"
            
            # Converte alerta_data (se existir)
            alerta_ts = os.get('alerta_data')
            alerta_str = ""
            if alerta_ts:
                if hasattr(alerta_ts, 'seconds'):
                    alerta_str = datetime.fromtimestamp(alerta_ts.seconds).strftime("%d/%m/%Y %H:%M")
                elif isinstance(alerta_ts, datetime):
                    alerta_str = alerta_ts.strftime("%d/%m/%Y %H:%M")
            
            data_for_df.append({
                "ID": os['id'],
                "Descrição": os.get('descricao', 'N/A')[:100].replace('\n', ' ') + '...',
                "Tipo": os.get('tipo', 'N/A'),
                "Status": os.get('status', 'N/A'),
                "Data Criação": data_criacao_str,
                "Alerta": alerta_str,
            })
            
        df = pd.DataFrame(data_for_df)
        
        # 2. Gera o PDF (usando PyMuPDF)
        doc = fitz.open()
        page = doc.new_page()
        
        y_cursor = 50
        x_start = 30
        line_height = 15
        
        # Título
        page.insert_text((x_start, y_cursor), "Relatório de Ordens de Serviço Ativas", fontname="helv-bold", fontsize=16)
        y_cursor += line_height * 2
        
        # Data de Geração
        page.insert_text((x_start, y_cursor), f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}", fontname="helv", fontsize=10)
        y_cursor += line_height * 2
        
        # Tabela (ajustada para um layout simples)
        
        headers = ["ID", "Tipo", "Status", "Data Criação", "Alerta"]
        # Larguras ajustadas para caber
        col_widths = [100, 80, 100, 100, 100]
        
        # Headers
        x_current = x_start
        for header, width in zip(headers, col_widths):
            page.insert_text((x_current, y_cursor), header, fontname="helv-bold", fontsize=10)
            x_current += width
        y_cursor += line_height
        
        # Linhas de dados
        for index, row in df.iterrows():
            if y_cursor > page.rect.height - 50: # Quebra de página
                page = doc.new_page()
                y_cursor = 50
                x_current = x_start
                # Reinsere headers
                for header, width in zip(headers, col_widths):
                    page.insert_text((x_current, y_cursor), header, fontname="helv-bold", fontsize=10)
                    x_current += width
                y_cursor += line_height

            x_current = x_start
            data_row = [row['ID'][:10], row['Tipo'], row['Status'], row['Data Criação'], row['Alerta']]
            for data, width in zip(data_row, col_widths):
                page.insert_text((x_current, y_cursor), str(data), fontname="helv", fontsize=9)
                x_current += width
            y_cursor += line_height

        # 3. Salva o PDF em buffer de memória
        pdf_bytes = doc.tobytes()
        doc.close()
        
        # 4. Envia o arquivo
        bio = io.BytesIO(pdf_bytes)
        bio.name = f"Relatorio_OS_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=InputFile(bio),
            caption=f"✅ Relatório de {len(ordens)} Ordens de Serviço Ativas gerado com sucesso."
        )
        
        await query.edit_message_text(
            "Relatório PDF enviado! O Menu Principal está abaixo:",
            reply_markup=menu_keyboard()
        )

    except Exception as e:
        logger.error(f"Erro ao gerar/enviar PDF: {e}")
        await query.edit_message_text(
            "❌ Erro ao processar o relatório PDF. Verifique o log do servidor.",
            reply_markup=menu_keyboard()
        )
        
    return MENU

# --- Job Queue e Lógica de Agendamento ---

async def check_alertas_and_reminders(context: ContextTypes.DEFAULT_TYPE):
    """
    Função agendada que verifica alertas/lembretes vencidos no Firestore e os envia.
    """
    logger.info("Executando verificação de alertas e lembretes agendados...")
    now = datetime.now()
    
    try:
        # Busca todos os alertas ativos (onde 'ativo' == True)
        # O filtro de data é feito localmente, pois o Firestore não suporta o operador '<' sem índice
        # e a ordem da query.
        alertas_stream = get_alertas_ref().where('ativo', '==', True).stream()
        
        for alerta_doc in alertas_stream:
            alerta = alerta_doc.to_dict()
            data_alerta_ts = alerta.get('data_alerta')
            
            if not data_alerta_ts:
                alerta_doc.reference.update({'ativo': False})
                continue

            # Converte Firestore Timestamp para objeto datetime
            if hasattr(data_alerta_ts, 'seconds'):
                data_alerta_dt = datetime.fromtimestamp(data_alerta_ts.seconds)
            elif isinstance(data_alerta_ts, datetime):
                data_alerta_dt = data_alerta_ts
            else:
                logger.warning(f"Tipo de data inesperado para alerta {alerta_doc.id}: {type(data_alerta_ts)}")
                alerta_doc.reference.update({'ativo': False})
                continue
            
            # Checa se o alerta/lembrete já passou
            if data_alerta_dt <= now:
                chat_id = alerta.get('chat_id')
                descricao = alerta.get('descricao')
                os_id = alerta.get('os_id')
                tipo = alerta.get('tipo', 'alerta_os')

                if tipo == 'alerta_os':
                    mensagem = f"🔔 *ALERTA DE OS VENCIDO!* (ID: `{os_id}`)\n\n"
                    mensagem += f"Descrição do Alerta: *{descricao}*\n"
                    mensagem += f"Data Agendada: `{data_alerta_dt.strftime('%d/%m/%Y %H:%M')}`"
                    os_data = get_os_by_id(os_id)
                    if os_data:
                        mensagem += f"\nStatus Atual da OS: *{os_data['status']}*"
                        
                    # Remove campos de alerta da OS (limpeza)
                    try:
                        get_os_ref(os_id).update({
                            'alerta_data': firestore.DELETE_FIELD,
                            'alerta_descricao': firestore.DELETE_FIELD,
                            'alerta_ref_id': firestore.DELETE_FIELD
                        })
                    except Exception as e:
                        logger.error(f"Erro ao limpar campos de alerta na OS {os_id}: {e}")

                else: # Lembrete manual
                    mensagem = f"🗓 *LEMBRETE MANUAL!* (ID: `{alerta_doc.id}`)\n\n"
                    mensagem += f"Mensagem: *{descricao}*\n"
                    mensagem += f"Data Agendada: `{data_alerta_dt.strftime('%d/%m/%Y %H:%M')}`"
                    
                # Envia a mensagem
                try:
                    if chat_id:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=mensagem,
                            parse_mode=ParseMode.MARKDOWN
                        )
                except Exception as e:
                    logger.error(f"Falha ao enviar mensagem de alerta para chat {chat_id}: {e}")

                # Desativa o alerta no Firestore
                try:
                    alerta_doc.reference.update({'ativo': False})
                    logger.info(f"Alerta/Lembrete {alerta_doc.id} desativado.")
                except Exception as e:
                    logger.error(f"Erro ao desativar alerta {alerta_doc.id}: {e}")

    except Exception as e:
        logger.error(f"Erro na Job Queue: {e}")

# --- Webhook Keep-Alive ---

async def keep_alive(context: ContextTypes.DEFAULT_TYPE):
    """Envia uma requisição HTTP para a própria URL do webhook para mantê-lo ativo."""
    if WEBHOOK_URL and TOKEN:
        full_url = WEBHOOK_URL + WEBHOOK_PATH
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url) as response:
                    logger.info(f"Keep-alive enviado para {full_url}. Status: {response.status}")
        except Exception as e:
            logger.error(f"Erro no Keep-Alive: {e}")

# --- Main ---

def run_bot() -> None:
    """Inicia o bot e configura o Webhook ou Long Polling."""
    
    if not TOKEN:
        logger.error("O Token do Telegram não foi carregado. Finalizando.")
        return
        
    # 1. Cria a Aplicação
    application = Application.builder().token(TOKEN).build()
    
    # 2. Configura a JobQueue
    job_queue: JobQueue = application.job_queue

    # Agenda a verificação de alertas para rodar a cada 60 segundos
    job_queue.run_repeating(check_alertas_and_reminders, interval=60, first=0, name="alert_checker")
    
    # Agenda o keep-alive para rodar a cada 5 minutos
    job_queue.run_repeating(keep_alive, interval=300, first=300, name="keep_alive")


    # 3. Configura o ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        
        states={
            MENU: [
                CallbackQueryHandler(criar_os, pattern='^criar_os$'),
                CallbackQueryHandler(ver_ativas, pattern='^ver_ativas$'),
                CallbackQueryHandler(atualizar_status_os, pattern='^atualizar_status_os$'),
                CallbackQueryHandler(gerenciar_alertas_menu, pattern='^gerenciar_alertas_menu$'),
                CallbackQueryHandler(lembrete_menu, pattern='^lembrete_menu$'),
                CallbackQueryHandler(exportar_os_para_pdf, pattern='^exportar_pdf$'),
            ],
            
            # Fluxo de Criação de OS
            PROMPT_DESCRICAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_descricao),
            ],
            PROMPT_TIPO: [
                CallbackQueryHandler(receive_tipo, pattern='^tipo_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            
            # Fluxo de Atualização de Status
            PROMPT_OS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_update_status),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_STATUS: [
                CallbackQueryHandler(set_new_status, pattern='^set_status_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],

            # Fluxo de Gestão de Alertas
            PROMPT_ALERTA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_alerta_action),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_TIPO_INCLUSAO: [
                CallbackQueryHandler(prompt_incluir_alerta, pattern='^incluir_alerta_'),
                CallbackQueryHandler(remover_alerta_menu_start, pattern='^remover_alerta_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_INCLUSAO: [
                # Recebe a data OU a descrição
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_descricao), 
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_ID_ALERTA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_prazo_or_id),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            
            # Fluxo de Lembrete Manual
            LEMBRETE_MENU: [
                CallbackQueryHandler(callback_handler, pattern='^lembrete_manual_start$'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_LEMBRETE_DATA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_msg),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_LEMBRETE_MSG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_lembrete),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(menu_callback, pattern='^menu$'),
            CallbackQueryHandler(confirm_delete_os, pattern='^confirm_delete_'),
            MessageHandler(filters.COMMAND, fallback_command),
        ],
    )

    # Adiciona o ConversationHandler e o start
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start)) 
    
    # 4. Configuração do Webhook
    if WEBHOOK_URL:
        try:
            # Define a URL do webhook no Telegram
            logger.info(f"A iniciar Webhook em http://0.0.0.0:{PORT}{WEBHOOK_PATH}")
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TOKEN, 
                webhook_url=WEBHOOK_URL + WEBHOOK_PATH, 
            )
            logger.info(f"Servidor Webhook iniciado e escutando na porta {PORT}.")
            logger.info(f"Webhook URL configurada no Telegram: {WEBHOOK_URL + WEBHOOK_PATH}")
        except Exception as e:
            logger.error(f"Falha ao iniciar o Webhook: {e}")
            logger.info("Tentando modo Long Polling (desativar para produção em infraestrutura Webhook).")
            # Fallback para Long Polling (para debug ou ambientes sem suporte a webhook)
            application.run_polling(allowed_updates=Update.ALL_TYPES)
    else:
        # Se WEBHOOK_URL não estiver configurado, usa Long Polling
        logger.info("WEBHOOK_URL não definida. Iniciando em modo Long Polling.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_bot()
