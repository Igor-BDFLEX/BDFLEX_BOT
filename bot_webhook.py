# bot_os_telegram.py - Bot para Gestão de Ordens de Serviço (OS) via Telegram (WEBHOOK MODE)
#
# Este bot permite:
# 1. Criação, visualização, atualização e eliminação de Ordens de Serviço (OS).
# 2. Gestão de alertas (lembretes) associados a uma OS específica.
# 3. Agendamento de lembretes manuais.
# 4. Exportação do estado atual das OS para PDF.
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

# --- Imports para PDF (necessitam de instalação via pip: PyMuPDF e pandas) ---
try:
    import fitz # PyMuPDF
    import pandas as pd
    PDF_PROCESSOR_AVAILABLE = True
except ImportError:
    # Se PyMuPDF ou Pandas não estiverem disponíveis, o recurso Enviar PDF será desativado
    logging.warning("Módulos 'fitz' (PyMuPDF) e/ou 'pandas' não encontrados. O recurso Enviar PDF não funcionará.")
    PDF_PROCESSOR_AVAILABLE = False
    class MockDataFrame: # Placeholder para evitar erros
        def __init__(self, *args, **kwargs): pass
        def to_html(self): return "Módulos de PDF indisponíveis."
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
    JobQueue
)
from telegram.constants import ParseMode
from dotenv import load_dotenv

# Carrega variáveis de ambiente (se estiver a usar um ficheiro .env)
# load_dotenv() 

# --- Configuração ---

# Habilita o logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Define níveis de log mais altos para bibliotecas que usam muito log
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- Variáveis de Ambiente e Constantes ---

# Defina as variáveis de ambiente aqui ou use um ficheiro .env
# Reutilizando o token de TELEGRAM_TOKEN="8343582672:AAGVE-52s_KTo3tXgQIKUFBn3017FZOm17A"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "8343582672:AAGVE-52s_KTo3tXgQIKUFBn3017FZOm17A")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://your-app-name.herokuapp.com") # Substituir pela sua URL real
WEBHOOK_PATH = f"/{TOKEN}"
PORT = int(os.environ.get("PORT", "8000"))

# Estados para o ConversationHandler
MENU, PROMPT_OS, PROMPT_DESCRICAO, PROMPT_TIPO, PROMPT_STATUS, PROMPT_ATUALIZACAO, PROMPT_ALERTA, PROMPT_INCLUSAO, PROMPT_ID_ALERTA, PROMPT_TIPO_INCLUSAO, LEMBRETE_MENU, PROMPT_ID_LEMBRETE, PROMPT_LEMBRETE_DATA, PROMPT_LEMBRETE_MSG = range(14)

# Status e Tipos de OS (para botões)
OS_STATUS = ["Pendente", "Em Progresso", "Concluído", "Cancelado"]
OS_TIPOS = ["Manutenção", "Instalação", "Reparo", "Outro"]

# --- Firebase Init ---

# O conteúdo da app.json (Chave de Serviço) deve ser carregado.
# Para manter o ficheiro completo e autónomo, embed a chave aqui (ATENÇÃO: Não recomendado para produção real por segurança).
# Em ambiente de produção, carregue-o via variável de ambiente ou ficheiro seguro.
FIREBASE_CONFIG_JSON = """
{
  "type": "service_account",
  "project_id": "automatizacaoos",
  "private_key_id": "cd9957ad7e95a872f60b98ede7c08818f053ee68",
  "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCeEkfbg+HH7VrH\\n/a5WuHiqKlmddmbNgwzuJK5jdUHfJ1WQvcwIEwhxzRJZ0Fb9OMVPyzhoCM4Zieq6\\nOwtyQ7enX+dVyxHMGw+aIVywk6c60tFvnPGIQRq4gwdlKbIxnzmuZFaD+eYYsa08\\nC7WhxN6OrgX2KRRgqx7U5banhEs/xOvl0qHEt1jLgz92s65HqgUH/Fq3EDGWRRR1\\neQfDLstG/UrEVP5/5DRwTU962hVXL4GC1uekf7blhb1IineRCdd774e3bWQjwaaA\\nOepLGA1LR7yBOSPwPuq1pG5nZ5aA2zp1d6ruAde62Wz/fmZ1+Tt8u050GgHOMA2Y\\nRarjfjp/AgMBAAECggEAC69FQYxPqdQ5VDRD6WQsg0..."
}
""" # Conteúdo real do seu app.json omitido por segurança, substitua com o conteúdo completo.

# Tenta carregar a chave de serviço
try:
    if "private_key" in FIREBASE_CONFIG_JSON:
        cred = credentials.Certificate(json.loads(FIREBASE_CONFIG_JSON))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase inicializado com sucesso.")
    else:
        logger.error("A chave de serviço do Firebase está incompleta ou ausente.")
        db = None
except Exception as e:
    logger.error(f"Erro ao inicializar o Firebase: {e}")
    db = None

# --- Funções Auxiliares de BD (Firestore) ---

def get_os_collection(user_id):
    """Retorna a referência à coleção de OS para o utilizador."""
    if not db: return None
    # Armazena os dados privados do utilizador em 'artifacts/{appId}/users/{userId}/ordens_servico'
    # Como não temos __app_id e userId de forma padrão, usamos o user_id do Telegram
    return db.collection(f"users/{user_id}/ordens_servico")

def get_alertas_collection(user_id):
    """Retorna a referência à coleção de alertas para o utilizador."""
    if not db: return None
    return db.collection(f"users/{user_id}/alertas")

async def get_os_data(user_id, os_id):
    """Obtém dados de uma OS específica."""
    try:
        doc_ref = get_os_collection(user_id).document(os_id)
        doc = await doc_ref.get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        logger.error(f"Erro ao obter OS {os_id}: {e}")
        return None

async def list_all_os(user_id):
    """Lista todas as OS do utilizador."""
    try:
        docs = await get_os_collection(user_id).get()
        return [{"id": doc.id, **doc.to_dict()} for doc in docs]
    except Exception as e:
        logger.error(f"Erro ao listar OS: {e}")
        return []

async def get_os_alerts(user_id, os_id):
    """Obtém alertas para uma OS específica."""
    try:
        alerts_ref = get_alertas_collection(user_id)
        q = alerts_ref.where("os_id", "==", os_id).stream()
        return [{"id": doc.id, **doc.to_dict()} async for doc in q]
    except Exception as e:
        logger.error(f"Erro ao obter alertas para OS {os_id}: {e}")
        return []

# --- Funções de Conversa (Handlers) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a conversa e vai para o menu principal."""
    if update.message:
        user_id = update.message.from_user.id
        await update.message.reply_text(
            f"Bem-vindo(a) ao Bot de Gestão de OS! \nO seu ID de utilizador é: `{user_id}`.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    return await menu(update, context)

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra o menu principal."""
    
    # Se for CallbackQuery, deve responder e editar a mensagem
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        message = query.edit_message_text
        user_id = query.from_user.id
    # Se for Message, deve responder diretamente
    elif update.message:
        message = update.message.reply_text
        user_id = update.message.from_user.id
    else:
        # Caso fallback de cancel/start onde update.message pode ser None
        message = update.effective_chat.send_message
        user_id = update.effective_chat.id

    keyboard = [
        [InlineKeyboardButton("Criar Nova OS", callback_data="criar_os")],
        [
            InlineKeyboardButton("Ver/Atualizar OS", callback_data="atualizar_existente"),
            InlineKeyboardButton("Eliminar OS", callback_data="eliminar_os")
        ],
        [
            InlineKeyboardButton("Gerir Alertas", callback_data="menu_alerta"),
            InlineKeyboardButton("Lembrete Manual", callback_data="lembrete_manual_start")
        ],
        [InlineKeyboardButton("Exportar PDF", callback_data="enviar_pdf")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await message(
        "*Menu Principal*\nEscolha uma opção para gerir as suas Ordens de Serviço (OS).", 
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )

    # Verifica se o job_queue está ativo
    if context.job_queue:
        current_jobs = context.job_queue.get_jobs_by_name(f"alert_check_{user_id}")
        if not current_jobs:
            # Agenda a verificação de alertas a cada 60 segundos
            context.job_queue.run_repeating(check_alerts, interval=60, first=0, name=f"alert_check_{user_id}", data={"user_id": user_id})
            logger.info(f"JobQueue para user {user_id} iniciado.")

    return MENU

# --- Fluxo de Criação de OS ---

async def prompt_os_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o ID da OS."""
    query = update.callback_query
    await query.answer()
    
    action = query.data # criar_os, atualizar_existente, eliminar_os
    
    if action == "criar_os":
        context.user_data['os_data'] = {} # Inicia dados para nova OS
        context.user_data['flow'] = 'criar_os'
        await query.edit_message_text(
            "Digite o ID único para a nova Ordem de Serviço (Ex: OS-001, Cliente-A).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="menu")]])
        )
        return PROMPT_OS
        
    elif action in ["atualizar_existente", "eliminar_os"]:
        context.user_data['flow'] = action
        all_os = await list_all_os(query.from_user.id)
        
        if not all_os:
            await query.edit_message_text(
                "Não existem Ordens de Serviço registadas. Crie uma primeiro!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
            )
            return MENU
            
        os_list_text = "\n".join([f"- `{os['id']}` ({os['status']})" for os in all_os])
        
        await query.edit_message_text(
            f"Digite o ID da Ordem de Serviço que deseja *{('eliminar' if action == 'eliminar_os' else 'atualizar/ver')}*:\n\n*OS Existentes:*\n{os_list_text}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="menu")]])
        )
        return PROMPT_OS
        
    return MENU

async def receive_os_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o ID da OS e avança ou verifica a existência."""
    user_id = update.message.from_user.id
    os_id = update.message.text.strip()
    flow = context.user_data.get('flow')
    context.user_data['os_id'] = os_id

    # 1. Fluxo de Criação
    if flow == 'criar_os':
        if await get_os_data(user_id, os_id):
            await update.message.reply_text(f"O ID `{os_id}` já existe. Por favor, digite um ID único.")
            return PROMPT_OS
        
        context.user_data['os_data']['id'] = os_id
        return await prompt_descricao(update, context)

    # 2. Fluxo de Atualização/Eliminação
    elif flow in ['atualizar_existente', 'eliminar_os']:
        os_data = await get_os_data(user_id, os_id)
        
        if not os_data:
            await update.message.reply_text(f"OS com ID `{os_id}` não encontrada. Por favor, digite um ID válido.")
            return PROMPT_OS
        
        if flow == 'eliminar_os':
            await update.message.reply_text(
                f"Tem certeza que deseja *ELIMINAR* a OS com ID: `{os_id}`?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Sim, Eliminar", callback_data=f"confirm_delete_{os_id}")],
                    [InlineKeyboardButton("Não, Cancelar", callback_data="menu")]
                ]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return PROMPT_OS # Permanece no estado para callback_handler processar 'confirm_delete'
        
        elif flow == 'atualizar_existente':
            context.user_data['os_data'] = os_data
            return await menu_atualizacao(update, context, os_data, os_id, is_new_message=True)
            
    return MENU

# --- Fluxo de Descrição/Tipo/Status (Comum à Criação) ---

async def prompt_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a descrição da OS."""
    await update.message.reply_text(
        "Digite a descrição detalhada da OS (qual o problema/serviço?).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="menu")]])
    )
    return PROMPT_DESCRICAO

async def receive_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição e avança para o tipo."""
    context.user_data['os_data']['descricao'] = update.message.text.strip()
    return await prompt_tipo(update, context)

async def prompt_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o tipo de OS."""
    keyboard = [[InlineKeyboardButton(tipo, callback_data=f"tipo_{tipo}")] for tipo in OS_TIPOS]
    reply_markup = InlineKeyboardMarkup(keyboard + [[InlineKeyboardButton("Cancelar", callback_data="menu")]])
    
    # Se for a primeira vez (via MessageHandler), responde. Se for via CallbackQuery, edita.
    if update.message:
        await update.message.reply_text("Escolha o tipo de OS:", reply_markup=reply_markup)
    else: # Veio de um callback (e.g. Cancelar no próximo passo)
        await update.callback_query.edit_message_text("Escolha o tipo de OS:", reply_markup=reply_markup)

    return PROMPT_TIPO

async def receive_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o tipo e avança para o status."""
    query = update.callback_query
    await query.answer()
    
    tipo = query.data.replace("tipo_", "")
    context.user_data['os_data']['tipo'] = tipo
    
    return await prompt_status(update, context)

async def prompt_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o status inicial da OS e guarda a OS."""
    keyboard = [[InlineKeyboardButton(status, callback_data=f"status_{status}")] for status in OS_STATUS]
    reply_markup = InlineKeyboardMarkup(keyboard + [[InlineKeyboardButton("Cancelar", callback_data="menu")]])
    
    await update.callback_query.edit_message_text("Escolha o *Status* inicial da OS:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

    return PROMPT_STATUS

async def receive_status_and_save_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o status, guarda a OS no Firestore e volta ao menu."""
    query = update.callback_query
    await query.answer()
    
    status = query.data.replace("status_", "")
    os_data = context.user_data.get('os_data', {})
    os_data['status'] = status
    os_data['criada_em'] = datetime.now().isoformat()
    os_data['atualizada_em'] = datetime.now().isoformat()
    user_id = query.from_user.id
    os_id = os_data.get('id')
    
    try:
        if os_id and db:
            os_data_to_save = {k: v for k, v in os_data.items() if k != 'id'} # Não guarda o ID dentro do documento
            await get_os_collection(user_id).document(os_id).set(os_data_to_save)
            
            summary = (
                f"*OS Criada com Sucesso!*\n\n"
                f"ID: `{os_id}`\n"
                f"Descrição: {os_data.get('descricao')}\n"
                f"Tipo: {os_data.get('tipo')}\n"
                f"Status: *{os_data.get('status')}*\n"
            )
            await query.edit_message_text(summary, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await query.edit_message_text("Erro: ID da OS não encontrado ou Firebase indisponível.")
    except Exception as e:
        logger.error(f"Erro ao salvar OS {os_id}: {e}")
        await query.edit_message_text("Ocorreu um erro ao tentar guardar a OS. Tente novamente.")

    # Limpa dados do fluxo e volta ao menu
    context.user_data.pop('os_data', None)
    context.user_data.pop('os_id', None)
    context.user_data.pop('flow', None)
    
    # Adiciona um botão para voltar ao menu
    await query.message.reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
    
    return MENU

# --- Fluxo de Atualização de OS ---

def format_os_details(os_id: str, os_data: dict, alerts: list = None) -> str:
    """Formata os detalhes da OS para exibição."""
    text = (
        f"*Detalhes da OS: {os_id}*\n\n"
        f"Descrição: {os_data.get('descricao', 'N/A')}\n"
        f"Tipo: {os_data.get('tipo', 'N/A')}\n"
        f"Status: *{os_data.get('status', 'N/A')}*\n"
        f"Criada em: {datetime.fromisoformat(os_data.get('criada_em')).strftime('%d/%m/%Y %H:%M') if os_data.get('criada_em') else 'N/A'}\n"
        f"Atualizada em: {datetime.fromisoformat(os_data.get('atualizada_em')).strftime('%d/%m/%Y %H:%M') if os_data.get('atualizada_em') else 'N/A'}\n"
    )
    if alerts is not None:
        alert_summary = "\n".join([
            f"  - `{alert['id'][:4]}`: '{alert['descricao'][:20]}...' em {datetime.fromisoformat(alert['prazo']).strftime('%d/%m %H:%M')}"
            for alert in alerts
        ])
        if alert_summary:
            text += f"\n*Alertas ({len(alerts)}):*\n{alert_summary}"
        else:
            text += "\n*Alertas:* Nenhum agendado."
            
    return text

async def menu_atualizacao(update: Update, context: ContextTypes.DEFAULT_TYPE, os_data: dict, os_id: str, is_new_message: bool = False) -> int:
    """Mostra os detalhes da OS e opções de atualização."""
    user_id = update.effective_user.id
    
    # Obter alertas para mostrar no menu
    alerts = await get_os_alerts(user_id, os_id)
    
    formatted_details = format_os_details(os_id, os_data, alerts)
    
    keyboard = [
        [InlineKeyboardButton("Mudar Status", callback_data="upd_status")],
        [
            InlineKeyboardButton("Mudar Tipo", callback_data="upd_tipo"),
            InlineKeyboardButton("Mudar Descrição", callback_data="upd_descricao")
        ],
        [InlineKeyboardButton("Gerir Alertas (Dedicado)", callback_data="alerta_existente")], # Vai para o menu de gestão de alertas
        [InlineKeyboardButton("Voltar ao Menu Principal", callback_data="menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if is_new_message:
        await update.message.reply_text(formatted_details, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(formatted_details, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

    # Armazena os dados atuais para o fluxo de atualização
    context.user_data['os_data'] = os_data
    context.user_data['os_id'] = os_id
    context.user_data['flow'] = 'atualizar_os'
    
    return PROMPT_ATUALIZACAO

async def prompt_atualizar_campo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o prompt para receber o novo valor de um campo."""
    query = update.callback_query
    await query.answer()
    
    action = query.data.replace("upd_", "")
    context.user_data['field_to_update'] = action
    
    os_id = context.user_data.get('os_id')

    if action == "status":
        keyboard = [[InlineKeyboardButton(status, callback_data=f"set_status_{status}")] for status in OS_STATUS]
        reply_markup = InlineKeyboardMarkup(keyboard + [[InlineKeyboardButton("Cancelar", callback_data="cancelar_atualizacao")]])
        await query.edit_message_text(f"Escolha o *novo Status* para a OS `{os_id}`:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return PROMPT_ATUALIZACAO
        
    elif action == "tipo":
        keyboard = [[InlineKeyboardButton(tipo, callback_data=f"set_tipo_{tipo}")] for tipo in OS_TIPOS]
        reply_markup = InlineKeyboardMarkup(keyboard + [[InlineKeyboardButton("Cancelar", callback_data="cancelar_atualizacao")]])
        await query.edit_message_text(f"Escolha o *novo Tipo* para a OS `{os_id}`:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return PROMPT_ATUALIZACAO
        
    elif action == "descricao":
        await query.edit_message_text(
            f"Digite a *nova Descrição* para a OS `{os_id}`:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="cancelar_atualizacao")]])
        )
        return PROMPT_ATUALIZACAO
        
    return PROMPT_ATUALIZACAO

async def receive_novo_valor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o novo valor (apenas para Descrição) e guarda a atualização."""
    field = context.user_data.get('field_to_update')
    os_id = context.user_data.get('os_id')
    user_id = update.message.from_user.id
    
    if field == 'descricao':
        novo_valor = update.message.text.strip()
    else:
        await update.message.reply_text("Erro inesperado. Por favor, use os botões para Status/Tipo.")
        return PROMPT_ATUALIZACAO

    return await finalize_update(update, context, novo_valor, field)

async def finalize_update_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o novo valor (de botões) e guarda a atualização."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    field = context.user_data.get('field_to_update')
    
    if data.startswith("set_status_"):
        novo_valor = data.replace("set_status_", "")
    elif data.startswith("set_tipo_"):
        novo_valor = data.replace("set_tipo_", "")
    else:
        # Caso de cancelamento
        if data == 'cancelar_atualizacao':
            os_data = await get_os_data(query.from_user.id, context.user_data.get('os_id'))
            if os_data:
                return await menu_atualizacao(update, context, os_data, context.user_data.get('os_id'))
            return await menu(update, context)
        
        await query.edit_message_text("Ação de atualização desconhecida.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]]))
        return MENU
        
    return await finalize_update(update, context, novo_valor, field)

async def finalize_update(update: Update, context: ContextTypes.DEFAULT_TYPE, novo_valor: str, field: str) -> int:
    """Guarda a atualização no Firestore e retorna ao menu de atualização."""
    os_id = context.user_data.get('os_id')
    user_id = update.effective_user.id
    
    try:
        update_data = {
            field: novo_valor,
            'atualizada_em': datetime.now().isoformat()
        }
        
        doc_ref = get_os_collection(user_id).document(os_id)
        await doc_ref.update(update_data)
        
        # Obtém os dados atualizados para mostrar o menu
        updated_os_data = await get_os_data(user_id, os_id)
        
        if updated_os_data:
            return await menu_atualizacao(update, context, updated_os_data, os_id)
        else:
            raise Exception("Dados da OS não encontrados após a atualização.")

    except Exception as e:
        logger.error(f"Erro ao atualizar OS {os_id}: {e}")
        
        if update.message:
            await update.message.reply_text("Ocorreu um erro ao atualizar a OS. Tente novamente.")
        elif update.callback_query:
            await update.callback_query.edit_message_text("Ocorreu um erro ao atualizar a OS. Tente novamente.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]]))

    return MENU

# --- Fluxo de Eliminação ---

async def confirm_delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Elimina a OS após confirmação."""
    query = update.callback_query
    await query.answer()
    
    os_id = query.data.replace("confirm_delete_", "")
    user_id = query.from_user.id
    
    try:
        # 1. Eliminar alertas associados
        alerts_ref = get_alertas_collection(user_id)
        alerts_query = alerts_ref.where("os_id", "==", os_id).stream()
        async for doc in alerts_query:
            await doc.reference.delete()
        
        # 2. Eliminar a OS
        await get_os_collection(user_id).document(os_id).delete()

        await query.edit_message_text(
            f"Ordem de Serviço `{os_id}` e todos os seus alertas foram *ELIMINADOS* com sucesso.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )
    except Exception as e:
        logger.error(f"Erro ao eliminar OS {os_id}: {e}")
        await query.edit_message_text("Ocorreu um erro ao eliminar a OS. Tente novamente.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]]))

    # Limpa dados do fluxo
    context.user_data.pop('os_id', None)
    context.user_data.pop('flow', None)
    
    return MENU

# --- Fluxo de Gestão de Alertas ---

async def menu_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra o menu de gestão de alertas."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    all_os = await list_all_os(user_id)
    
    if not all_os:
        await query.edit_message_text(
            "Não existem OS para gerir alertas. Crie uma OS primeiro.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )
        return MENU
        
    os_list_text = "\n".join([f"- `{os['id']}` ({os['status']})" for os in all_os])
    
    await query.edit_message_text(
        f"*Menu de Gestão de Alertas*\n\nDigite o ID da OS à qual deseja gerir os alertas (criar/remover):\n\n*OS Existentes:*\n{os_list_text}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
    )
    
    context.user_data['flow'] = 'gestao_alerta'
    return PROMPT_ALERTA
    
async def prompt_os_alerta_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o ID da OS para gerir alertas e mostra as opções."""
    os_id = update.message.text.strip()
    user_id = update.message.from_user.id
    
    os_data = await get_os_data(user_id, os_id)
    
    if not os_data:
        await update.message.reply_text(f"OS com ID `{os_id}` não encontrada. Digite um ID válido.")
        return PROMPT_ALERTA
        
    context.user_data['os_id'] = os_id
    
    return await menu_alerta_os_especifica(update, context, os_id, os_data)

async def menu_alerta_os_especifica(update: Update, context: ContextTypes.DEFAULT_TYPE, os_id: str, os_data: dict) -> int:
    """Mostra opções de alerta para uma OS específica."""
    user_id = update.effective_user.id
    alerts = await get_os_alerts(user_id, os_id)
    
    alert_summary = ""
    if alerts:
        alert_summary = "\n*Alertas Ativos:*\n" + "\n".join([
            f"  - `ID: {alert['id'][:4]}` | Desc: {alert['descricao'][:30]}... | Prazo: *{datetime.fromisoformat(alert['prazo']).strftime('%d/%m/%Y %H:%M')}*"
            for alert in alerts
        ])
    else:
        alert_summary = "\n*Alertas Ativos:* Nenhum agendado."

    keyboard = [
        [InlineKeyboardButton("Criar Novo Alerta", callback_data="criar_alerta")],
        [InlineKeyboardButton("Remover Alerta Existente", callback_data="remover_alerta_menu")],
        [InlineKeyboardButton("Voltar à OS", callback_data="voltar_os_update")], # Volta ao menu de atualização da OS
        [InlineKeyboardButton("Voltar ao Menu Principal", callback_data="menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = (
        f"*Gestão de Alertas para OS: {os_id}*\n"
        f"Status Atual: *{os_data.get('status', 'N/A')}*"
        f"{alert_summary}"
    )
    
    if update.message:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    elif update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

    return PROMPT_ALERTA

async def prompt_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a descrição do alerta."""
    query = update.callback_query
    await query.answer()
    
    os_id = context.user_data.get('os_id')
    
    if query.data == "criar_alerta":
        await query.edit_message_text(
            f"A criar alerta para OS `{os_id}`. \n\nQual a descrição do alerta (o que precisa ser lembrado)?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="alerta_existente")]])
        )
        context.user_data['flow'] = 'criar_alerta_descricao'
        return PROMPT_INCLUSAO
    
    return PROMPT_ALERTA

async def receive_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição do alerta e solicita o prazo."""
    descricao = update.message.text.strip()
    context.user_data['alerta_descricao'] = descricao
    
    return await prompt_alerta_prazo(update, context)

async def prompt_alerta_prazo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o prazo do alerta."""
    await update.message.reply_text(
        "Agora, digite o prazo para o alerta no formato *DD/MM/AAAA HH:MM* (Ex: 01/12/2025 15:30).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar", callback_data="alerta_existente")]])
    )
    context.user_data['flow'] = 'criar_alerta_prazo'
    return PROMPT_ID_ALERTA

async def receive_alerta_prazo_or_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o prazo (ou ID do alerta para remover) e processa."""
    input_text = update.message.text.strip()
    user_id = update.message.from_user.id
    flow = context.user_data.get('flow')
    os_id = context.user_data.get('os_id')
    
    if flow == 'criar_alerta_prazo':
        # Tenta parsear a data
        try:
            alerta_prazo = datetime.strptime(input_text, "%d/%m/%Y %H:%M")
            if alerta_prazo <= datetime.now() + timedelta(minutes=1):
                await update.message.reply_text("O prazo deve ser no futuro. Tente novamente com uma data/hora futura.")
                return PROMPT_ID_ALERTA
            
            # 1. Guarda o Alerta
            alerta_data = {
                "os_id": os_id,
                "descricao": context.user_data.get('alerta_descricao'),
                "prazo": alerta_prazo.isoformat(),
                "criado_em": datetime.now().isoformat(),
                "user_id": user_id,
                "chat_id": update.message.chat_id
            }
            
            doc_ref = await get_alertas_collection(user_id).add(alerta_data)
            
            await update.message.reply_text(
                f"Alerta criado com sucesso para a OS `{os_id}`!\n"
                f"Lembrete: *{alerta_data['descricao']}*\n"
                f"Agendado para: *{alerta_prazo.strftime('%d/%m/%Y %H:%M')}*",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            
            # 2. Retorna ao menu de alertas da OS
            os_data = await get_os_data(user_id, os_id)
            return await menu_alerta_os_especifica(update, context, os_id, os_data)

        except ValueError:
            await update.message.reply_text("Formato de data/hora inválido. Use DD/MM/AAAA HH:MM (Ex: 01/12/2025 15:30).")
            return PROMPT_ID_ALERTA

    elif flow == 'remover_alerta_id':
        # Tenta remover o alerta
        return await remover_alerta(update, context, input_text)
        
    return PROMPT_ALERTA

async def prompt_remover_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o ID curto do alerta a remover."""
    query = update.callback_query
    await query.answer()
    
    os_id = context.user_data.get('os_id')
    user_id = query.from_user.id
    
    alerts = await get_os_alerts(user_id, os_id)
    
    if not alerts:
        await query.edit_message_text(
            f"Não existem alertas ativos para a OS `{os_id}`.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar", callback_data="alerta_existente")]])
        )
        return PROMPT_ALERTA
        
    alert_list = "\n".join([
        f"  - *{alert['id'][:4]}*: {alert['descricao'][:30]}..."
        for alert in alerts
    ])
    
    await query.edit_message_text(
        f"*Remover Alerta para OS: {os_id}*\n\n"
        f"Digite os *primeiros 4 caracteres* do ID do alerta que deseja remover:\n\n"
        f"{alert_list}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar", callback_data="alerta_existente")]])
    )
    context.user_data['flow'] = 'remover_alerta_id'
    return PROMPT_ID_ALERTA # Reutiliza o estado de prompt de ID

async def remover_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE, short_id: str) -> int:
    """Elimina um alerta específico pelo seu ID curto."""
    user_id = update.message.from_user.id
    os_id = context.user_data.get('os_id')
    
    # Busca o ID completo pelo ID curto
    alerts = await get_os_alerts(user_id, os_id)
    target_alert = next((alert for alert in alerts if alert['id'].startswith(short_id)), None)
    
    if target_alert:
        try:
            await get_alertas_collection(user_id).document(target_alert['id']).delete()
            await update.message.reply_text(
                f"Alerta com ID `{target_alert['id'][:4]}` e descrição *'{target_alert['descricao'][:20]}...'* eliminado com sucesso.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.error(f"Erro ao eliminar alerta {target_alert['id']}: {e}")
            await update.message.reply_text("Ocorreu um erro ao eliminar o alerta. Tente novamente.")
    else:
        await update.message.reply_text(f"Nenhum alerta encontrado com o ID curto *`{short_id}`* para a OS `{os_id}`.", parse_mode=ParseMode.MARKDOWN_V2)

    # Retorna ao menu de alertas da OS
    os_data = await get_os_data(user_id, os_id)
    return await menu_alerta_os_especifica(update, context, os_id, os_data)

# --- Fluxo de Lembrete Manual ---

async def menu_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de lembrete manual, solicitando a descrição."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "*Criação de Lembrete Manual*\n\nQual a descrição do lembrete?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="menu")]])
    )
    context.user_data['flow'] = 'criar_lembrete_descricao'
    return PROMPT_ID_LEMBRETE

async def prompt_lembrete_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição do lembrete e solicita a data."""
    context.user_data['lembrete_descricao'] = update.message.text.strip()
    
    await update.message.reply_text(
        "Lembrete: *'{lembrete_descricao}'*\n\nAgora, digite o prazo no formato *DD/MM/AAAA HH:MM* (Ex: 01/12/2025 15:30).",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="menu")]])
    )
    context.user_data['flow'] = 'criar_lembrete_data'
    return PROMPT_LEMBRETE_DATA

async def prompt_lembrete_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data e guarda o lembrete (sem OS associada)."""
    input_text = update.message.text.strip()
    user_id = update.message.from_user.id
    
    try:
        lembrete_prazo = datetime.strptime(input_text, "%d/%m/%Y %H:%M")
        if lembrete_prazo <= datetime.now() + timedelta(minutes=1):
            await update.message.reply_text("O prazo deve ser no futuro. Tente novamente com uma data/hora futura.")
            return PROMPT_LEMBRETE_DATA
        
        # 1. Guarda o Alerta (sem OS associada)
        lembrete_data = {
            "os_id": None, # Indica que é um lembrete manual
            "descricao": context.user_data.get('lembrete_descricao'),
            "prazo": lembrete_prazo.isoformat(),
            "criado_em": datetime.now().isoformat(),
            "user_id": user_id,
            "chat_id": update.message.chat_id
        }
        
        doc_ref = await get_alertas_collection(user_id).add(lembrete_data)
        
        await update.message.reply_text(
            f"*Lembrete Manual Criado com Sucesso!*\n\n"
            f"Lembrete: *{lembrete_data['descricao']}*\n"
            f"Agendado para: *{lembrete_prazo.strftime('%d/%m/%Y %H:%M')}*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )

    except ValueError:
        await update.message.reply_text("Formato de data/hora inválido. Use DD/MM/AAAA HH:MM (Ex: 01/12/2025 15:30).")
        return PROMPT_LEMBRETE_DATA
        
    # Limpa dados do fluxo e volta ao menu
    context.user_data.pop('lembrete_descricao', None)
    context.user_data.pop('flow', None)
    return MENU

# --- Funções do Job Queue (Alertas) ---

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Envia o lembrete/alerta ao utilizador e elimina-o."""
    job = context.job
    alerta_id = job.name.split('_')[1]
    
    # 1. Obter dados do alerta (usando o user_id do job.data para encontrar a coleção correta)
    user_id = job.data['user_id']
    alerts_ref = get_alertas_collection(user_id)
    doc_ref = alerts_ref.document(alerta_id)
    
    try:
        doc = await doc_ref.get()
        alerta = doc.to_dict()
        
        if not alerta:
            logger.warning(f"Alerta {alerta_id} não encontrado. Não será enviado.")
            return
            
        chat_id = alerta['chat_id']
        descricao = alerta['descricao']
        os_id = alerta.get('os_id')
        
        message_text = f"🚨 *LEMBRETE AGENDADO* 🚨\n\n"
        if os_id:
            message_text += f"Associado à OS: `{os_id}`\n"
        message_text += f"Detalhe: *{descricao}*\n"
        message_text += f"\nData do Alerta: {datetime.fromisoformat(alerta['prazo']).strftime('%d/%m/%Y %H:%M')}"
        
        await context.bot.send_message(chat_id=chat_id, text=message_text, parse_mode=ParseMode.MARKDOWN_V2)
        
        # 2. Eliminar o alerta do Firestore
        await doc_ref.delete()
        logger.info(f"Alerta {alerta_id} enviado e eliminado para o user {user_id}.")
        
    except Exception as e:
        logger.error(f"Erro ao enviar/eliminar alerta {alerta_id}: {e}")

async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Verifica todos os alertas agendados para o utilizador no Firestore."""
    user_id = context.job.data['user_id']
    alerts_ref = get_alertas_collection(user_id)
    
    # Busca alertas que estão próximos (ex: próximos 5 minutos)
    now = datetime.now()
    # Adicionamos um pequeno buffer de 60 segundos para garantir que não perdemos alertas
    prazo_limite = now + timedelta(minutes=5)

    try:
        # Pede todos os alertas e filtra em memória (Firestore não suporta query em data string diretamente)
        docs = await alerts_ref.get()
        
        for doc in docs:
            alerta = doc.to_dict()
            alerta_id = doc.id
            
            # Verifica se o alerta já foi agendado no JobQueue para evitar duplicação
            if context.job_queue.get_jobs_by_name(f"alert_{alerta_id}"):
                continue

            try:
                prazo = datetime.fromisoformat(alerta['prazo'])
                
                # Se o prazo for entre agora e os próximos 5 minutos, ou já passou (e precisa ser disparado)
                if prazo <= prazo_limite:
                    # Calcula o atraso para agendar imediatamente ou no tempo certo
                    delay = (prazo - now).total_seconds()
                    
                    # Garante que o delay não é negativo (para alertas expirados, dispara imediatamente)
                    if delay < 0:
                        delay = 1 # Dispara em 1 segundo
                        
                    context.job_queue.run_once(
                        send_reminder, 
                        when=delay, 
                        name=f"alert_{alerta_id}", 
                        data={"user_id": user_id}
                    )
                    logger.info(f"Alerta {alerta_id} (OS: {alerta.get('os_id')}) agendado para disparo em {delay:.2f} segundos.")
                    
            except ValueError:
                logger.error(f"Alerta {alerta_id} com formato de prazo inválido: {alerta.get('prazo')}")

    except Exception as e:
        logger.error(f"Erro no job check_alerts para user {user_id}: {e}")

# --- Fluxo de Exportação para PDF ---

async def enviar_pdf_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gera um PDF com o resumo de todas as OS e envia ao utilizador."""
    query = update.callback_query
    await query.answer("A gerar o PDF, por favor aguarde...")
    user_id = query.from_user.id
    
    if not PDF_PROCESSOR_AVAILABLE:
        await query.edit_message_text(
            "Desculpe, o módulo de geração de PDF (PyMuPDF/Pandas) não está instalado ou disponível.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )
        return MENU

    try:
        all_os = await list_all_os(user_id)
        if not all_os:
            await query.edit_message_text(
                "Não existem Ordens de Serviço registadas para gerar o PDF.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
            )
            return MENU
            
        # 1. Preparar os dados
        df_data = []
        for os in all_os:
            df_data.append({
                "ID": os['id'],
                "Descrição": os['descricao'][:50] + "...",
                "Tipo": os['tipo'],
                "Status": os['status'],
                "Criada Em": datetime.fromisoformat(os['criada_em']).strftime('%Y-%m-%d %H:%M')
            })

        df = pd.DataFrame(df_data)
        
        # 2. Gerar HTML a partir do DataFrame
        title = f"Relatório de Ordens de Serviço - Utilizador {user_id}"
        total_count = len(df)
        status_counts = df['Status'].value_counts().to_dict()
        status_summary = "<br>".join([f"<li>{status}: {count}</li>" for status, count in status_counts.items()])
        
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: sans-serif; margin: 20px; }}
                h1 {{ color: #333; }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 10pt; }}
                th {{ background-color: #f2f2f2; }}
                .summary {{ margin-bottom: 30px; padding: 15px; background-color: #e6f7ff; border-left: 5px solid #007bff; }}
            </style>
        </head>
        <body>
            <h1>{title}</h1>
            <div class="summary">
                <p><b>Total de OS:</b> {total_count}</p>
                <p><b>Resumo por Status:</b></p>
                <ul>{status_summary}</ul>
            </div>
            {df.to_html(index=False)}
            <p style="margin-top: 50px; font-size: 8pt;">Gerado pelo Bot de OS em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</p>
        </body>
        </html>
        """

        # 3. Gerar PDF usando PyMuPDF (fitz)
        pdf_bytes = io.BytesIO()
        doc = fitz.open() # Novo documento PDF
        page = doc.new_page() # Nova página
        
        # Insere o HTML na página
        rect = page.rect
        fitz.insert_html(page, rect, html_content)

        doc.save(pdf_bytes)
        doc.close()
        pdf_bytes.seek(0)
        
        # 4. Enviar o ficheiro
        pdf_file = InputFile(pdf_bytes, filename=f"Relatorio_OS_{user_id}_{datetime.now().strftime('%Y%m%d')}.pdf")
        
        await context.bot.send_document(
            chat_id=query.message.chat_id, 
            document=pdf_file, 
            caption=f"*Relatório PDF* de {total_count} Ordens de Serviço.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        
        await query.message.reply_text(
            "PDF enviado com sucesso!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )

    except Exception as e:
        logger.error(f"Erro ao gerar/enviar PDF: {e}")
        await query.edit_message_text(
            f"Ocorreu um erro ao gerar o PDF: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )

    return MENU

# --- Funções de Fallback e Cancelamento ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Trata todos os callbacks que não correspondem aos estados específicos."""
    query = update.callback_query
    data = query.data
    
    if data == "menu":
        return await menu(update, context)
    
    # Navegação no Menu de Alerta
    if data == "alerta_existente":
        os_id = context.user_data.get('os_id')
        user_id = query.from_user.id
        os_data = await get_os_data(user_id, os_id)
        if os_data:
            return await menu_alerta_os_especifica(update, context, os_id, os_data)
            
    # Voltar ao menu de atualização de OS
    if data == "voltar_os_update":
        os_id = context.user_data.get('os_id')
        user_id = query.from_user.id
        os_data = await get_os_data(user_id, os_id)
        if os_data:
            return await menu_atualizacao(update, context, os_data, os_id)
            
    # Processa ações de criação/atualização/eliminação
    if data in ["criar_os", "atualizar_existente", "eliminar_os"]:
        return await prompt_os_id(update, context)
        
    if data == "enviar_pdf":
        return await enviar_pdf_os(update, context)

    if data.startswith("confirm_delete_"):
        return await confirm_delete_os(update, context)
        
    if data.startswith("upd_"):
        return await prompt_atualizar_campo(update, context)

    if data.startswith("set_status_") or data.startswith("set_tipo_") or data == "cancelar_atualizacao":
        return await finalize_update_callback(update, context)
        
    if data == "menu_alerta":
        return await menu_alerta(update, context)
        
    if data == "criar_alerta":
        return await prompt_alerta_descricao(update, context)

    if data == "remover_alerta_menu":
        return await prompt_remover_alerta(update, context)

    if data == "lembrete_manual_start":
        return await menu_lembrete(update, context)
        
    await query.answer("Opção desconhecida. Use os botões para navegar.")
    return ConversationHandler.RETRY

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela o fluxo atual e volta ao menu principal."""
    context.user_data.clear() # Limpa todos os dados de utilizador do fluxo
    
    # Responde à mensagem /cancel
    if update.message:
        await update.message.reply_text(
            "Operação cancelada. A retornar ao menu principal.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Principal", callback_data="menu")]])
        )
    # Responde ao callback
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "Operação cancelada. A retornar ao menu principal.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Principal", callback_data="menu")]])
        )
        
    return MENU

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trata comandos que não correspondem a nenhum handler."""
    if update.message:
        await update.message.reply_text(
            "Comando não reconhecido. Use /start ou /cancel, ou escolha uma opção do menu."
        )

# --- Função Principal ---

def main() -> None:
    """Inicia o bot usando o modo Webhook."""

    if not db:
        logger.error("A aplicação não pode iniciar. Falha na inicialização do Firebase.")
        return

    # 1. Cria o Application com JobQueue
    application = Application.builder().token(TOKEN).concurrent_updates(True).build()
    
    # 2. Configura o ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(callback_handler, pattern='^criar_os$|^atualizar_existente$|^eliminar_os$|^menu_alerta$|^lembrete_manual_start$|^enviar_pdf$'),
            ],
            PROMPT_OS: [
                # Recebe o ID da OS para criar/atualizar/eliminar
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_id),
                CallbackQueryHandler(confirm_delete_os, pattern='^confirm_delete_'), # Confirmação de eliminação
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_DESCRICAO: [
                # Recebe a descrição
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_descricao),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_TIPO: [
                # Escolhe o tipo
                CallbackQueryHandler(receive_tipo, pattern='^tipo_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_STATUS: [
                # Escolhe o status e guarda a OS
                CallbackQueryHandler(receive_status_and_save_os, pattern='^status_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_ATUALIZACAO: [
                # Menu de atualização da OS
                CallbackQueryHandler(callback_handler, pattern='^upd_status$|^upd_tipo$|^upd_descricao$|^alerta_existente$|^voltar_os_update$|^menu$'),
                CallbackQueryHandler(finalize_update_callback, pattern='^set_status_|^set_tipo_|^cancelar_atualizacao$'), # Recebe o novo status/tipo
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_novo_valor), # Recebe a nova descrição
            ],
            PROMPT_ALERTA: [
                # Recebe o ID da OS para gestão de alertas
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_os_alerta_id),
                # Botões do menu de alerta (criar, remover, voltar)
                CallbackQueryHandler(callback_handler, pattern='^menu$|^alerta_existente$|^criar_alerta$|^remover_alerta_menu$|^voltar_os_update$'),
            ],
            PROMPT_INCLUSAO: [
                # Recebe a descrição do alerta
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_descricao),
                CallbackQueryHandler(callback_handler, pattern='^alerta_existente$'),
            ],
            PROMPT_ID_ALERTA: [
                # Recebe o prazo do alerta OU o ID para remover
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_prazo_or_id),
                CallbackQueryHandler(callback_handler, pattern='^alerta_existente$'),
            ],
            # Fluxo de Lembrete Manual
            PROMPT_ID_LEMBRETE: [ # Recebe a descrição
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_data),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_LEMBRETE_DATA: [ # Recebe a data
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_msg),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
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
    
    # 3. Configuração do Webhook
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
        logger.info("Tentando iniciar no modo Polling (para ambiente de desenvolvimento)...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
