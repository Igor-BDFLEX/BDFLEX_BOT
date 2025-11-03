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
from dotenv import load_dotenv

# --- Configuração ---

# Carrega variáveis de ambiente (TOKEN, WEBHOOK_URL, etc.)
load_dotenv()

# Habilita o logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Define níveis de log mais altos para bibliotecas que usam muito log
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Variáveis de ambiente
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "SUA_URL_WEBHOOK_AQUI")
PORT = int(os.environ.get('PORT', '8443')) # Padrão para Render/Heroku
WEBHOOK_PATH = f"/{TOKEN}"

# Estados para o ConversationHandler
MENU, PROMPT_OS, PROMPT_DESCRICAO, PROMPT_TIPO, PROMPT_STATUS, PROMPT_ATUALIZACAO, PROMPT_ALERTA, PROMPT_INCLUSAO, PROMPT_ID_ALERTA, PROMPT_TIPO_INCLUSAO, LEMBRETE_MENU, PROMPT_ID_LEMBRETE, PROMPT_LEMBRETE_DATA, PROMPT_LEMBRETE_MSG = range(14)

# --- Firebase Init ---

# Credenciais do Firebase (assumindo que o JSON da Service Account está na variável de ambiente FIREBASE_CREDENTIALS_JSON)
# Se estiver usando um arquivo app.json no deploy da Render, descomente e ajuste:
# path_to_credentials = os.path.join(os.getcwd(), 'app.json')
# cred = credentials.Certificate(path_to_credentials)

# Se estiver usando uma variável de ambiente (melhor prática em ambientes de produção)
try:
    firebase_json_str = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if firebase_json_str:
        firebase_json = json.loads(firebase_json_str)
        cred = credentials.Certificate(firebase_json)
    else:
        # Fallback para o arquivo local se existir (apenas para desenvolvimento local)
        cred = credentials.Certificate("app.json") 
except Exception as e:
    logger.error(f"Erro ao carregar credenciais do Firebase: {e}")
    exit(1)


if not firebase_admin._apps:
    firebase_app = initialize_app(cred)
db = firestore.client()

# --- Funções de Ajuda ---

def get_user_id(update: Update) -> str:
    """Obtém o ID de usuário do objeto Update."""
    return str(update.effective_user.id)

def get_os_ref(user_id: str):
    """Retorna a referência da coleção de OS do usuário no Firestore."""
    return db.collection("users").document(user_id).collection("ordens_servico")

def get_alerta_ref(user_id: str):
    """Retorna a referência da coleção de alertas do usuário no Firestore."""
    return db.collection("users").document(user_id).collection("alertas")

def format_os_message(os_data: dict, include_id: bool = False) -> str:
    """Formata os dados de uma OS em uma string legível."""
    os_id = os_data.get('id', 'N/A')
    descricao = os_data.get('descricao', 'N/A')
    tipo = os_data.get('tipo', 'N/A')
    status = os_data.get('status', 'N/A')
    data_criacao = os_data.get('data_criacao', datetime.now().isoformat())
    
    # Tentativa de formatar a data
    try:
        data_formatada = datetime.fromisoformat(data_criacao).strftime('%d/%m/%Y %H:%M')
    except:
        data_formatada = data_criacao

    # Se houver alertas
    alertas_pendentes = len(os_data.get('alertas', []))
    alerta_str = f" ({alertas_pendentes} Alerta{'s' if alertas_pendentes != 1 else ''})" if alertas_pendentes > 0 else ""

    message = f"""
*OS #{os_id}*{alerta_str}
- *Descrição:* {descricao}
- *Tipo:* {tipo}
- *Status:* {status}
- *Criada em:* {data_formatada}
    """
    if include_id:
        message += f"\n- *ID do Documento (Firestore):* `{os_data.get('doc_id', 'N/A')}`"
        
    return message.strip()

# --- Funções de Menu e Navegação ---

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra o menu principal."""
    keyboard = [
        [InlineKeyboardButton("Criar Nova OS", callback_data="criar_os")],
        [InlineKeyboardButton("Ver OS Existentes", callback_data="ver_os")],
        [InlineKeyboardButton("Atualizar/Excluir OS", callback_data="atualizar_os")],
        [InlineKeyboardButton("Gerenciar Lembretes Manuais", callback_data="lembrete_menu")],
        [InlineKeyboardButton("Exportar para PDF", callback_data="exportar_pdf")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    chat_id = update.effective_chat.id
    message_id = context.user_data.get("menu_message_id")
    
    welcome_message = "*🤖 Menu Principal - Gestão de OS*\nO que gostaria de fazer?"

    if update.callback_query:
        await update.callback_query.answer()
        # Se for um callback, edita a mensagem existente
        try:
            await update.callback_query.edit_message_text(
                welcome_message, 
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            # Captura exceção se a mensagem não foi modificada
            pass
    elif message_id:
        # Se for um comando inicial e a mensagem já existe, tenta editar
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=welcome_message,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
             # Se a edição falhar (ex: mensagem muito antiga), envia uma nova
            message = await context.bot.send_message(
                chat_id,
                welcome_message,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data["menu_message_id"] = message.message_id
    else:
        # Envia a mensagem pela primeira vez
        message = await context.bot.send_message(
            chat_id,
            welcome_message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["menu_message_id"] = message.message_id
        
    return MENU

# --- Handlers Básicos ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia a conversa com o bot."""
    return await show_main_menu(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a conversa e volta ao menu principal."""
    user_id = get_user_id(update)
    logger.info(f"Usuário {user_id} cancelou a conversa.")
    await update.message.reply_text(
        "Operação cancelada. Retornando ao menu principal.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu", callback_data="menu")]])
    )
    # Limpa dados temporários do usuário, se houver
    context.user_data.pop('current_os', None)
    context.user_data.pop('os_doc_id', None)
    return await show_main_menu(update, context)

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde a comandos não reconhecidos."""
    await update.message.reply_text(
        "Comando não reconhecido. Use /start para iniciar o bot ou clique em Menu.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Principal", callback_data="menu")]])
    )

# --- Lógica de OS (Criação, Atualização, Visualização) ---

async def prompt_os_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a descrição da nova OS."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "*Criar Nova OS*\n\nPor favor, envie a **descrição** detalhada da Ordem de Serviço:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )
    return PROMPT_DESCRICAO

async def receive_os_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição e solicita o tipo."""
    context.user_data['descricao'] = update.message.text
    
    keyboard = [
        [InlineKeyboardButton("Técnica", callback_data="tipo_Tecnica")],
        [InlineKeyboardButton("Administrativa", callback_data="tipo_Administrativa")],
        [InlineKeyboardButton("Suporte", callback_data="tipo_Suporte")],
        [InlineKeyboardButton("Voltar ao Menu", callback_data="menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"Descrição salva: `{context.user_data['descricao']}`\n\nAgora, selecione o **tipo** de OS:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_TIPO

async def prompt_os_tipo_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o tipo (via callback) e solicita o status inicial."""
    query = update.callback_query
    await query.answer()
    
    os_tipo = query.data.split('_')[1]
    context.user_data['tipo'] = os_tipo
    
    keyboard = [
        [InlineKeyboardButton("Pendente", callback_data="status_Pendente")],
        [InlineKeyboardButton("Em Andamento", callback_data="status_Em Andamento")],
        [InlineKeyboardButton("Concluída", callback_data="status_Concluída")],
        [InlineKeyboardButton("Voltar ao Menu", callback_data="menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"Tipo selecionado: `{os_tipo}`\n\nPor fim, selecione o **status inicial** da OS:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_STATUS

async def save_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o status (via callback), salva a OS no Firestore e volta ao menu."""
    query = update.callback_query
    await query.answer()
    
    os_status = query.data.split('_')[1]
    context.user_data['status'] = os_status
    
    user_id = get_user_id(update)
    os_ref = get_os_ref(user_id)
    
    # Gera um ID sequencial ou UUID simples
    os_id = str(uuid.uuid4()).split('-')[0].upper()
    
    os_data = {
        'id': os_id,
        'descricao': context.user_data['descricao'],
        'tipo': context.user_data['tipo'],
        'status': context.user_data['status'],
        'data_criacao': datetime.now().isoformat(),
        'alertas': [], # Lista de alertas/lembretes
        'historico': [{'data': datetime.now().isoformat(), 'evento': 'OS Criada', 'status': os_status}]
    }
    
    try:
        doc_ref = await os_ref.add(os_data)
        os_data['doc_id'] = doc_ref.id # Adiciona o doc_id para referência futura
        
        await query.edit_message_text(
            f"✅ *OS #{os_id} criada com sucesso!* \n\n{format_os_message(os_data)}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Erro ao salvar OS no Firestore: {e}")
        await query.edit_message_text(
            "❌ *Erro ao criar OS.*\nPor favor, tente novamente.",
            parse_mode=ParseMode.MARKDOWN
        )

    # Retorna ao menu
    return await show_main_menu(update, context)

async def view_os_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Lista todas as OS do usuário."""
    query = update.callback_query
    await query.answer()

    user_id = get_user_id(update)
    os_ref = get_os_ref(user_id)
    
    try:
        docs = await os_ref.order_by("data_criacao", direction=firestore.Query.DESCENDING).get()
        
        if not docs:
            message = "⚠️ *Nenhuma Ordem de Serviço encontrada.*"
            keyboard = [[InlineKeyboardButton("Criar Nova OS", callback_data="criar_os")]]
        else:
            message = "*Lista de Ordens de Serviço:*\n\n"
            keyboard = []
            
            for doc in docs:
                os_data = doc.to_dict()
                os_data['doc_id'] = doc.id
                
                status_emoji = "🟢" if os_data.get('status') == "Concluída" else "🟡"
                alertas_count = len(os_data.get('alertas', []))
                alerta_emoji = "🔔" if alertas_count > 0 else ""

                message += f"{status_emoji} {alerta_emoji} *OS #{os_data['id']}* ({os_data['status']}) - {os_data['descricao'][:40]}...\n"
                
                # Botão para ver detalhes/atualizar
                keyboard.append([
                    InlineKeyboardButton(
                        f"Ver/Atualizar OS #{os_data['id']}", 
                        callback_data=f"detalhe_{doc.id}"
                    )
                ])

        keyboard.append([InlineKeyboardButton("Voltar ao Menu", callback_data="menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Erro ao listar OS: {e}")
        await query.edit_message_text(
            "❌ Ocorreu um erro ao buscar as OS. Tente novamente mais tarde.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )
        
    return MENU # Mantém no estado de menu para que os callbacks funcionem

# --- Detalhes e Atualização de OS ---

async def view_os_details(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_id: str) -> int:
    """Mostra os detalhes de uma OS e as opções de atualização."""
    query = update.callback_query
    
    user_id = get_user_id(update)
    os_doc_ref = get_os_ref(user_id).document(doc_id)
    
    try:
        doc = await os_doc_ref.get()
        if not doc.exists:
            await query.edit_message_text(
                "❌ OS não encontrada.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
            )
            return MENU
            
        os_data = doc.to_dict()
        os_data['doc_id'] = doc.id
        context.user_data['current_os'] = os_data # Salva a OS atual
        context.user_data['os_doc_id'] = doc_id # Salva o ID do documento

        message = format_os_message(os_data)
        
        # Botões de Ação
        keyboard = [
            [
                InlineKeyboardButton("Alterar Status", callback_data=f"mudar_status_{doc_id}"),
                InlineKeyboardButton("Gerenciar Alertas", callback_data=f"alerta_menu_{doc_id}"),
            ],
            [
                InlineKeyboardButton("Excluir OS", callback_data=f"excluir_os_confirma_{doc_id}"),
            ],
            [
                InlineKeyboardButton("Voltar à Lista", callback_data="ver_os"),
                InlineKeyboardButton("Voltar ao Menu", callback_data="menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🔎 *Detalhes da OS #{os_data['id']}*\n\n{message}",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Erro ao buscar detalhes da OS: {e}")
        await query.edit_message_text(
            "❌ Ocorreu um erro ao carregar os detalhes.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )
        
    return PROMPT_OS # Estado de visualização/atualização

async def prompt_change_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a mudança de status."""
    query = update.callback_query
    await query.answer()
    
    doc_id = query.data.split('_')[-1]
    context.user_data['os_doc_id'] = doc_id
    
    keyboard = [
        [InlineKeyboardButton("Pendente", callback_data=f"update_status_Pendente_{doc_id}")],
        [InlineKeyboardButton("Em Andamento", callback_data=f"update_status_Em Andamento_{doc_id}")],
        [InlineKeyboardButton("Concluída", callback_data=f"update_status_Concluída_{doc_id}")],
        [InlineKeyboardButton("Cancelar e Voltar", callback_data=f"detalhe_{doc_id}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "Selecione o novo status para esta OS:",
        reply_markup=reply_markup
    )
    
    return PROMPT_ATUALIZACAO

async def update_os_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Atualiza o status da OS no Firestore."""
    query = update.callback_query
    await query.answer()
    
    # Exemplo: update_status_Em Andamento_IDDOC
    parts = query.data.split('_')
    new_status = parts[2]
    doc_id = parts[3]
    
    user_id = get_user_id(update)
    os_doc_ref = get_os_ref(user_id).document(doc_id)

    try:
        # Atualiza o status e adiciona ao histórico
        await os_doc_ref.update({
            'status': new_status,
            'historico': firestore.ArrayUnion([{
                'data': datetime.now().isoformat(),
                'evento': 'Status Atualizado',
                'status': new_status
            }])
        })
        
        # Volta para a tela de detalhes
        await query.edit_message_text(
            f"✅ *Status da OS atualizado para* `{new_status}` *com sucesso!*",
            parse_mode=ParseMode.MARKDOWN
        )
        # Recarrega e mostra os detalhes atualizados
        await asyncio.sleep(0.5) # Pequeno delay para garantir que o Firestore processou
        return await view_os_details(update, context, doc_id)

    except Exception as e:
        logger.error(f"Erro ao atualizar status da OS {doc_id}: {e}")
        await query.edit_message_text(
            "❌ *Erro ao atualizar o status.*",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar aos Detalhes", callback_data=f"detalhe_{doc_id}")]])
        )
        return PROMPT_OS

async def delete_os(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirma e exclui a OS."""
    query = update.callback_query
    await query.answer()

    # Exemplo: excluir_os_confirma_IDDOC
    doc_id = query.data.split('_')[-1]
    
    user_id = get_user_id(update)
    os_doc_ref = get_os_ref(user_id).document(doc_id)
    
    if query.data.startswith("excluir_os_confirma"):
        # Solicita confirmação
        keyboard = [
            [InlineKeyboardButton("SIM, Excluir Definitivamente", callback_data=f"excluir_os_{doc_id}")],
            [InlineKeyboardButton("NÃO, Manter OS", callback_data=f"detalhe_{doc_id}")]
        ]
        await query.edit_message_text(
            "⚠️ *Tem certeza que deseja excluir esta OS e todos os seus alertas?* Esta ação é irreversível.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return PROMPT_OS # Continua no estado de atualização
    
    elif query.data.startswith("excluir_os_"):
        # Executa a exclusão
        try:
            await os_doc_ref.delete()
            
            # Remove dados temporários
            if context.user_data.get('os_doc_id') == doc_id:
                context.user_data.pop('current_os', None)
                context.user_data.pop('os_doc_id', None)
            
            await query.edit_message_text(
                "✅ *OS excluída com sucesso.*",
                parse_mode=ParseMode.MARKDOWN
            )
            # Volta ao menu principal
            return await show_main_menu(update, context)
            
        except Exception as e:
            logger.error(f"Erro ao excluir OS {doc_id}: {e}")
            await query.edit_message_text(
                "❌ *Erro ao excluir a OS.*",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
            )
            return MENU

# --- Lógica de Alertas (Anexados a uma OS) ---

async def alerta_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra o menu de gestão de alertas para uma OS específica."""
    query = update.callback_query
    await query.answer()
    
    doc_id = query.data.split('_')[-1]
    
    user_id = get_user_id(update)
    os_doc_ref = get_os_ref(user_id).document(doc_id)
    
    try:
        doc = await os_doc_ref.get()
        if not doc.exists:
            await query.edit_message_text("❌ OS não encontrada.")
            return MENU
            
        os_data = doc.to_dict()
        context.user_data['current_os'] = os_data
        context.user_data['os_doc_id'] = doc_id
        
        alertas = os_data.get('alertas', [])
        
        # Formata a lista de alertas
        alertas_list = ""
        if alertas:
            alertas_list = "\n*Alertas Ativos:*\n"
            for i, alerta in enumerate(alertas):
                alerta_data = datetime.fromisoformat(alerta['data']).strftime('%d/%m/%Y %H:%M')
                alertas_list += f"*{i+1}.* {alerta['descricao']} (Prazo: {alerta_data})\n"
        else:
            alertas_list = "\n⚠️ *Nenhum alerta ativo para esta OS.*"
            
        message = f"🔔 *Gerenciamento de Alertas* (OS #{os_data['id']})\n\n"
        message += alertas_list
        
        keyboard = [
            [InlineKeyboardButton("Criar Novo Alerta", callback_data="criar_alerta")],
            [InlineKeyboardButton("Remover Alerta", callback_data="remover_alerta_menu")] if alertas else [],
            [InlineKeyboardButton("Voltar aos Detalhes", callback_data=f"detalhe_{doc_id}")]
        ]
        
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Erro ao abrir menu de alerta: {e}")
        await query.edit_message_text(
            "❌ Erro ao carregar menu de alertas.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]])
        )
        return MENU
        
    return PROMPT_ALERTA

async def prompt_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita a descrição do novo alerta."""
    query = update.callback_query
    if query:
        await query.answer()
        # Não edita, apenas envia a instrução
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="*Criar Novo Alerta*\n\nPor favor, envie a **descrição** ou o texto do lembrete:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"alerta_menu_{context.user_data.get('os_doc_id')}")]])
        )
    return PROMPT_INCLUSAO

async def receive_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição e solicita o prazo."""
    context.user_data['alerta_descricao'] = update.message.text
    
    await update.message.reply_text(
        "Descrição salva. Agora, por favor, envie o **prazo/data limite** para este alerta (Ex: `dd/mm/aaaa HH:MM` ou `+1d` para 1 dia):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"alerta_menu_{context.user_data.get('os_doc_id')}")]])
    )
    return PROMPT_ID_ALERTA

def parse_relative_time(text: str) -> datetime | None:
    """Converte expressões de tempo relativo (+5h, +2d) em datetime."""
    match = re.match(r"^\+(\d+)([hdm])$", text.lower())
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        
        now = datetime.now()
        if unit == 'h':
            return now + timedelta(hours=value)
        elif unit == 'd':
            return now + timedelta(days=value)
        elif unit == 'm': # m para minutos, se for o caso
             return now + timedelta(minutes=value)
    return None

async def receive_alerta_prazo_or_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe o prazo/ID para remoção e salva ou solicita nova entrada."""
    text = update.message.text
    doc_id = context.user_data.get('os_doc_id')
    
    # 1. Tenta interpretar como prazo
    prazo = parse_relative_time(text)
    if not prazo:
        # Tenta interpretar como data e hora completa (dd/mm/aaaa HH:MM)
        try:
            prazo = datetime.strptime(text, '%d/%m/%Y %H:%M')
        except ValueError:
            # Se a descrição do alerta estiver no contexto, é um prazo inválido
            if 'alerta_descricao' in context.user_data:
                await update.message.reply_text(
                    "❌ Formato de prazo inválido. Tente `dd/mm/aaaa HH:MM` ou `+2d` para 2 dias. Tente novamente:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"alerta_menu_{doc_id}")]])
                )
                return PROMPT_ID_ALERTA
            # Se NÃO estiver no contexto, o usuário está tentando remover um alerta
            
    # 2. Se for um prazo VÁLIDO e a descrição existir, salva o alerta
    if prazo and 'alerta_descricao' in context.user_data:
        alerta_data = {
            'descricao': context.user_data['alerta_descricao'],
            'data': prazo.isoformat(),
            'criado_em': datetime.now().isoformat(),
            'chat_id': update.effective_chat.id,
            'os_doc_id': doc_id,
            'user_id': get_user_id(update),
            'id_alerta': str(uuid.uuid4()) # ID único para a job/alerta
        }
        
        user_id = get_user_id(update)
        os_doc_ref = get_os_ref(user_id).document(doc_id)
        
        try:
            # Adiciona o alerta à lista de alertas da OS
            await os_doc_ref.update({'alertas': firestore.ArrayUnion([alerta_data])})
            
            # Limpa os dados temporários do alerta
            context.user_data.pop('alerta_descricao', None)
            
            await update.message.reply_text(
                f"✅ *Alerta agendado com sucesso!* Será enviado em: `{prazo.strftime('%d/%m/%Y %H:%M')}`",
                parse_mode=ParseMode.MARKDOWN
            )
            # Retorna ao menu de alertas
            return await view_os_details(update, context, doc_id)

        except Exception as e:
            logger.error(f"Erro ao salvar alerta: {e}")
            await update.message.reply_text("❌ Erro ao salvar o alerta.")
            return await view_os_details(update, context, doc_id)

    # 3. Tenta interpretar como índice para REMOÇÃO
    if 'alerta_descricao' not in context.user_data:
        try:
            index_to_remove = int(text) - 1
            await remove_alerta_by_index(update, context, index_to_remove)
            return await view_os_details(update, context, doc_id)
        except ValueError:
            await update.message.reply_text(
                "❌ Entrada inválida. Por favor, envie o *número* do alerta que deseja remover ou clique em 'Cancelar'.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"alerta_menu_{doc_id}")]])
            )
            return PROMPT_ID_ALERTA
            
    return PROMPT_ID_ALERTA # Se cair aqui, a lógica anterior falhou, repete a solicitação de prazo/ID

async def prompt_remove_alerta_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Solicita o ID do alerta a ser removido (índice da lista)."""
    query = update.callback_query
    await query.answer()

    os_data = context.user_data.get('current_os', {})
    doc_id = context.user_data.get('os_doc_id')
    alertas = os_data.get('alertas', [])

    if not alertas:
        await query.edit_message_text(
            "⚠️ Não há alertas para remover.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar aos Detalhes", callback_data=f"detalhe_{doc_id}")]])
        )
        return PROMPT_OS

    # Formata a lista de alertas numerada
    alertas_list = "*Alertas Ativos:*\n"
    for i, alerta in enumerate(alertas):
        alerta_data = datetime.fromisoformat(alerta['data']).strftime('%d/%m/%Y %H:%M')
        alertas_list += f"*{i+1}.* {alerta['descricao']} (Prazo: {alerta_data})\n"

    message = f"🔔 *Remover Alerta* (OS #{os_data['id']})\n\n"
    message += alertas_list
    message += "\nPor favor, envie o **número** do alerta (1, 2, 3...) que deseja remover:"

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=message,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"alerta_menu_{doc_id}")]])
    )
    
    return PROMPT_ID_ALERTA

async def remove_alerta_by_index(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int) -> None:
    """Remove um alerta da OS pelo índice."""
    user_id = get_user_id(update)
    doc_id = context.user_data.get('os_doc_id')
    
    if not doc_id:
        await update.message.reply_text("❌ Erro: ID da OS não encontrado.")
        return

    os_doc_ref = get_os_ref(user_id).document(doc_id)
    
    try:
        doc = await os_doc_ref.get()
        if not doc.exists:
            await update.message.reply_text("❌ OS não encontrada para remoção de alerta.")
            return

        os_data = doc.to_dict()
        alertas = os_data.get('alertas', [])
        
        if 0 <= index < len(alertas):
            alerta_removido = alertas.pop(index)
            
            # Atualiza o documento com a nova lista de alertas
            await os_doc_ref.update({'alertas': alertas})

            await update.message.reply_text(
                f"✅ Alerta *'{alerta_removido['descricao']}'* removido com sucesso.",
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data['current_os']['alertas'] = alertas # Atualiza o contexto
        else:
            await update.message.reply_text(f"❌ Índice {index + 1} inválido. Não foi possível remover o alerta.")

    except Exception as e:
        logger.error(f"Erro ao remover alerta: {e}")
        await update.message.reply_text("❌ Erro interno ao remover o alerta.")


# --- Lógica de Lembretes Manuais ---

async def lembrete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra o menu para gerenciar lembretes manuais (não anexados a OS)."""
    query = update.callback_query
    await query.answer()

    message = "⏰ *Gerenciamento de Lembretes Pessoais*\n\n"
    message += "Aqui você pode agendar lembretes rápidos não relacionados a nenhuma Ordem de Serviço."
    
    keyboard = [
        [InlineKeyboardButton("Agendar Novo Lembrete", callback_data="lembrete_manual_start")],
        [InlineKeyboardButton("Voltar ao Menu", callback_data="menu")]
    ]
    
    await query.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return LEMBRETE_MENU

async def prompt_lembrete_data_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo do lembrete, solicitando a data/prazo."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "*Agendar Novo Lembrete*\n\nPor favor, envie o **prazo/data limite** para o lembrete (Ex: `dd/mm/aaaa HH:MM` ou `+5h` para 5 horas):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar e Voltar", callback_data="lembrete_menu")]])
    )
    
    return PROMPT_ID_LEMBRETE

async def prompt_lembrete_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a data e solicita a mensagem."""
    text = update.message.text
    
    prazo = parse_relative_time(text)
    if not prazo:
        try:
            prazo = datetime.strptime(text, '%d/%m/%Y %H:%M')
        except ValueError:
            await update.message.reply_text(
                "❌ Formato de prazo inválido. Tente `dd/mm/aaaa HH:MM` ou `+2d` para 2 dias. Tente novamente:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar e Voltar", callback_data="lembrete_menu")]])
            )
            return PROMPT_ID_LEMBRETE
    
    context.user_data['lembrete_prazo'] = prazo
    
    await update.message.reply_text(
        f"Data salva: `{prazo.strftime('%d/%m/%Y %H:%M')}`\n\nAgora, por favor, envie a **mensagem** que você deseja ser lembrado:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar e Voltar", callback_data="lembrete_menu")]])
    )
    return PROMPT_LEMBRETE_MSG

async def prompt_lembrete_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a mensagem e salva o lembrete."""
    context.user_data['lembrete_msg'] = update.message.text
    
    await save_lembrete(update, context) # Chama a função de salvamento
    
    return LEMBRETE_MENU

async def save_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva o lembrete manual no Firestore."""
    
    prazo = context.user_data.get('lembrete_prazo')
    mensagem = context.user_data.get('lembrete_msg')
    
    if not prazo or not mensagem:
        await update.message.reply_text("❌ Erro: Dados do lembrete incompletos.")
        return await lembrete_menu_return_logic(update, context)

    alerta_data = {
        'id_alerta': str(uuid.uuid4()),
        'descricao': mensagem,
        'data': prazo.isoformat(),
        'criado_em': datetime.now().isoformat(),
        'chat_id': update.effective_chat.id,
        'user_id': get_user_id(update),
        'tipo': 'manual' # Tipo para diferenciar de alertas de OS
    }

    user_id = get_user_id(update)
    alerta_ref = get_alerta_ref(user_id) # Usa a coleção de alertas pessoais/manuais
    
    try:
        await alerta_ref.add(alerta_data)
        
        await update.message.reply_text(
            f"✅ *Lembrete pessoal agendado!* Mensagem: `{mensagem}`. Data: `{prazo.strftime('%d/%m/%Y %H:%M')}`",
            parse_mode=ParseMode.MARKDOWN
        )
        # Limpa dados temporários
        context.user_data.pop('lembrete_prazo', None)
        context.user_data.pop('lembrete_msg', None)
        
    except Exception as e:
        logger.error(f"Erro ao salvar lembrete manual: {e}")
        await update.message.reply_text("❌ Erro ao salvar o lembrete pessoal.")

    return await lembrete_menu_return_logic(update, context)


async def lembrete_menu_return_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lógica para retornar ao menu de lembretes, considerando se veio de um comando ou callback."""
    if update.callback_query:
        # Se veio de um callback, edita o menu
        return await lembrete_menu(update, context)
    else:
        # Se veio de um MessageHandler (após enviar a mensagem do lembrete), reenvia o menu
        return await show_main_menu(update, context)

# --- Lógica de Exportação para PDF ---

async def exportar_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exporta todas as OS para um PDF e envia ao usuário."""
    query = update.callback_query
    await query.answer("Gerando PDF... Aguarde um momento.")
    
    user_id = get_user_id(update)
    os_ref = get_os_ref(user_id)
    chat_id = query.message.chat_id

    if not PDF_PROCESSOR_AVAILABLE:
        await context.bot.send_message(
            chat_id,
            "❌ *Recurso de Exportar PDF indisponível.*\nO servidor não possui as bibliotecas 'PyMuPDF' e 'pandas' instaladas.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        # 1. Busca os dados
        docs = await os_ref.order_by("data_criacao", direction=firestore.Query.DESCENDING).get()
        if not docs:
            await context.bot.send_message(chat_id, "⚠️ Nenhuma OS para exportar.")
            return

        # 2. Converte para DataFrame do Pandas
        data_list = []
        for doc in docs:
            d = doc.to_dict()
            d['doc_id'] = doc.id
            # Simplifica a data de criação
            try:
                d['data_criacao'] = datetime.fromisoformat(d['data_criacao']).strftime('%d/%m/%Y %H:%M')
            except:
                pass
            data_list.append({
                'ID': d['id'],
                'Descrição': d['descricao'],
                'Tipo': d['tipo'],
                'Status': d['status'],
                'Alertas': len(d.get('alertas', [])),
                'Criação': d['data_criacao']
            })
        
        df = pd.DataFrame(data_list)
        
        # 3. Cria um arquivo temporário em memória para o PDF
        pdf_buffer = io.BytesIO()
        doc = fitz.open() # Novo documento PDF
        
        # Cria uma página com o conteúdo do DataFrame
        page = doc.new_page()
        
        # (Lógica simplificada para converter DataFrame para texto e colocar no PDF)
        # O fitz não tem um método direto para renderizar dataframes.
        # Aqui, vamos apenas exportar o texto e tentar formatar minimamente.
        
        text = f"Relatório de Ordens de Serviço - {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        text += df.to_string(index=False, justify='left')

        # Insere o texto na página (precisa de uma fonte compatível com fitz)
        rect = page.rect
        fitz.TextWriter(rect, doc=doc).write_text(rect.tl, text)

        # Salva o PDF no buffer
        doc.save(pdf_buffer)
        doc.close()
        pdf_buffer.seek(0)
        
        # 4. Envia o arquivo ao usuário
        await context.bot.send_document(
            chat_id,
            document=InputFile(pdf_buffer, filename=f"relatorio_os_{datetime.now().strftime('%Y%m%d')}.pdf"),
            caption="✅ *Exportação concluída!* Aqui está o seu relatório.",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Erro na exportação para PDF: {e}")
        await context.bot.send_message(
            chat_id,
            "❌ *Erro ao gerar o PDF.* Verifique os logs do servidor.",
            parse_mode=ParseMode.MARKDOWN
        )

# --- Callbacks Handler (Onde estava o erro) ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processa todos os callbacks de botões inline."""
    query = update.callback_query
    
    # IMPORTANTE: Responde ao callback ANTES de fazer operações longas
    await query.answer()

    data = query.data
    
    if data == "menu":
        # CORREÇÃO DA LINHA 383 APLICADA AQUI
        # Edita a mensagem para o Menu Principal (botão "Voltar ao Menu")
        # É a linha que substitui o INCORRETO: await query.message.reply_markup = InlineKeyboardMarkup(...)
        try:
            # 1. Remove os botões da mensagem anterior, deixando apenas o texto
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            # Não faz nada se a edição falhar (mensagem já editada ou muito antiga)
            pass
        
        # 2. Chama a função que envia ou edita o menu principal
        return await show_main_menu(update, context)

    # --- Fluxo de OS ---
    elif data == "criar_os":
        return await prompt_os_descricao(update, context)
    elif data == "ver_os":
        return await view_os_list(update, context)
    elif data == "atualizar_os":
        # Reutiliza a função de lista para que o usuário possa escolher qual atualizar
        return await view_os_list(update, context)
    
    # --- Detalhes/Ações da OS ---
    elif data.startswith("detalhe_"):
        doc_id = data.split('_')[1]
        return await view_os_details(update, context, doc_id)
    elif data.startswith("tipo_"):
        return await prompt_os_tipo_status(update, context)
    elif data.startswith("status_"):
        return await save_os(update, context)
    elif data.startswith("mudar_status_"):
        return await prompt_change_status(update, context)
    elif data.startswith("update_status_"):
        return await update_os_status(update, context)
    elif data.startswith("excluir_os_"): # Pode ser confirmação ou exclusão final
        return await delete_os(update, context)

    # --- Fluxo de Alertas de OS ---
    elif data.startswith("alerta_menu_"):
        return await alerta_menu(update, context)
    elif data == "criar_alerta":
        return await prompt_alerta_descricao(update, context)
    elif data == "remover_alerta_menu":
        return await prompt_remove_alerta_menu(update, context)

    # --- Fluxo de Lembretes Manuais ---
    elif data == "lembrete_menu":
        return await lembrete_menu(update, context)
    elif data == "lembrete_manual_start":
        return await prompt_lembrete_data_start(update, context)
    
    # --- Fluxo de Exportação ---
    elif data == "exportar_pdf":
        return await exportar_pdf(update, context)

    # Caso não seja reconhecido, volta ao menu principal
    return await show_main_menu(update, context)

# --- Lógica do Bot ---

async def keep_alive():
    """Tarefa periódica para manter o serviço ativo se não houver tráfego (opcional)."""
    if not WEBHOOK_URL.startswith("SUA_URL_WEBHOOK_AQUI"):
        try:
            # Pinga a URL do webhook (evita que o serviço entre em "sleep" no Render/Heroku)
            async with aiohttp.ClientSession() as session:
                async with session.get(WEBHOOK_URL) as response:
                    logger.info(f"Keep-alive ping OK. Status: {response.status}")
        except Exception as e:
            logger.error(f"Erro no keep-alive: {e}")
    else:
        logger.warning("Keep-alive desativado: WEBHOOK_URL não configurada.")
        
def main() -> None:
    """Inicia o bot usando o modo Webhook."""
    if not TOKEN:
        logger.error("TOKEN do Telegram não encontrado. Verifique suas variáveis de ambiente.")
        return

    # 1. Cria a Application
    application = Application.builder().token(TOKEN).build()
    
    # 2. Agenda a tarefa de keep-alive (a cada 20 minutos)
    # application.job_queue.run_repeating(keep_alive, interval=1200, first=0) # Descomente se usar JobQueue

    # 3. Define o ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        
        states={
            MENU: [
                # Todos os callbacks que saem do menu
                CallbackQueryHandler(callback_handler, pattern='^criar_os$|^ver_os$|^atualizar_os$|^lembrete_menu$|^exportar_pdf$'),
            ],
            PROMPT_DESCRICAO: [
                # Recebe a descrição da OS
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_descricao),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_TIPO: [
                # Recebe o tipo (via callback)
                CallbackQueryHandler(callback_handler, pattern='^tipo_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_STATUS: [
                # Recebe o status (via callback)
                CallbackQueryHandler(callback_handler, pattern='^status_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_OS: [
                # Estado geral para detalhes/atualização de OS
                CallbackQueryHandler(callback_handler, pattern='^detalhe_|^mudar_status_|^excluir_os_'),
                CallbackQueryHandler(callback_handler, pattern='^alerta_menu_'), # Permite entrar no menu de alertas
                CallbackQueryHandler(callback_handler, pattern='^menu$|^ver_os$'),
            ],
            PROMPT_ATUALIZACAO: [
                # Recebe atualização de status (via callback)
                CallbackQueryHandler(callback_handler, pattern='^update_status_|^detalhe_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_ALERTA: [
                # Menu de gestão de alertas para uma OS
                CallbackQueryHandler(callback_handler, pattern='^menu$|^criar_alerta$|^remover_alerta_menu$|^detalhe_'),
            ],
            PROMPT_INCLUSAO: [
                # Recebe a descrição do alerta (texto)
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_descricao),
                CallbackQueryHandler(callback_handler, pattern='^alerta_menu_'),
            ],
            PROMPT_ID_ALERTA: [
                # Recebe o prazo do alerta OU o ID/Índice para remover
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_prazo_or_id),
                CallbackQueryHandler(callback_handler, pattern='^alerta_menu_'),
            ],
            # Fluxo de Lembrete Manual
            LEMBRETE_MENU: [
                CallbackQueryHandler(callback_handler, pattern='^lembrete_manual_start$|^menu$'),
            ],
            PROMPT_ID_LEMBRETE: [
                # Recebe a data do lembrete (texto)
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_data),
                CallbackQueryHandler(callback_handler, pattern='^lembrete_menu$|^menu$'),
            ],
            PROMPT_LEMBRETE_MSG: [
                # Recebe a mensagem do lembrete (texto)
                MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_lembrete_msg),
                CallbackQueryHandler(callback_handler, pattern='^lembrete_menu$|^menu$'),
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
        logger.info("Tentando modo polling como fallback...")
        # Fallback para Polling em caso de falha (útil em desenvolvimento local)
        try:
            logger.info("Bot rodando em modo polling...")
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e_polling:
            logger.error(f"Falha ao iniciar em modo polling: {e_polling}")


if __name__ == "__main__":
    main()
