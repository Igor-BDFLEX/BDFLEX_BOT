# bot_webhook.py - Bot para Gestão de Ordens de Serviço (OS) via Telegram (WEBHOOK MODE)

# --- Imports e Setup ---

import logging
import json
import time
import os
import re # Para manipulação de texto
import uuid # Para IDs únicos

# Firebase
import firebase_admin
from firebase_admin import credentials, firestore, initialize_app

# Python Telegram Bot
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
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

# Estados para o ConversationHandler
MENU, PROMPT_OS, PROMPT_DESCRICAO, PROMPT_TIPO, PROMPT_STATUS, PROMPT_ATUALIZACAO, PROMPT_ALERTA, PROMPT_INCLUSAO, PROMPT_ID_ALERTA, PROMPT_TIPO_INCLUSAO = range(10)

# --- Firebase Init ---

def initialize_firebase():
    """
    Inicializa o Firebase usando a credencial JSON da variável de ambiente.
    Implementa um tratamento robusto para problemas de formatação do JSON.
    """
    try:
        # Tenta carregar as credenciais da variável de ambiente
        firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
        
        if not firebase_credentials_json:
            logger.error("A variável de ambiente 'FIREBASE_CREDENTIALS_JSON' não está configurada.")
            return None

        # --- Etapa 1: Carregamento do JSON ---
        cred_dict = {}
        try:
            # Tenta carregar o JSON de forma direta (mais limpa)
            cred_dict = json.loads(firebase_credentials_json)
            logger.info("Tentativa 1: JSON carregado diretamente.")
        except json.JSONDecodeError as e:
            # Se falhar, tenta limpar a string (útil para quebras de linha ou caracteres de escape)
            logger.warning(f"Tentativa 1 falhou ao carregar o JSON: {e}. Tentando limpeza...")
            # Remove quebras de linha e tabs que podem ter sido inseridas no Render
            cleaned_json_string = firebase_credentials_json.replace('\n', '').replace('\t', '')
            
            try:
                # Tenta carregar novamente após limpeza básica
                cred_dict = json.loads(cleaned_json_string)
                logger.info("Tentativa 2: JSON carregado após limpeza.")
            except json.JSONDecodeError as e_clean:
                # Se ainda falhar, algo fundamental está errado com a string.
                raise ValueError(f"Falha ao carregar JSON após todas as tentativas de limpeza: {e_clean}")


        # --- Etapa 2: Validação Agressiva da Credencial (Onde o erro está ocorrendo) ---
        
        # Se o json.loads funcionar, mas o Firebase falhar, o problema é no conteúdo do dicionário.
        if cred_dict.get("type") != "service_account":
            logger.error(f"O campo 'type' no JSON carregado é '{cred_dict.get('type')}', mas deveria ser 'service_account'.")
            raise ValueError("O JSON da credencial é inválido. Falta ou está incorreto o campo 'type'.")

        # O certificado é criado a partir do dicionário Python (cred_dict)
        cred = credentials.Certificate(cred_dict)
        
        # Inicializa o app do Firebase
        firebase_admin.initialize_app(cred)
        logger.info("Firebase inicializado com sucesso a partir da variável de ambiente.")
        return firestore.client()

    except Exception as e:
        logger.error(f"Erro na inicialização do Firebase: {e}")
        # Loga a dica mais específica
        if "Certificate must contain a \"type\" field set to \"service_account\"" in str(e) or "O JSON da credencial é inválido" in str(e):
             logger.error("DICA FINAL: Este erro é SEMPRE causado por caracteres invisíveis, aspas extras, ou quebras de linha corrompendo a estrutura da credencial na variável 'FIREBASE_CREDENTIALS_JSON' no Render.")
             logger.error("A RECOMENDAÇÃO é gerar uma NOVA chave de conta de serviço no Firebase e colá-la cuidadosamente no Render.")
        return None

# Variável global para o cliente Firestore
db = initialize_firebase()

# --- Funções do Database (Apenas exemplos, implementar lógica real conforme necessário) ---

def create_os_document(chat_id, user_id, os_data):
    """Cria um novo documento de OS no Firestore."""
    if not db: return False
    
    # Gerar um ID de OS único
    os_id = str(uuid.uuid4()).split('-')[0].upper() # Ex: 1A2B3C
    os_data['os_id'] = os_id
    os_data['chat_id'] = chat_id
    os_data['user_id'] = user_id
    os_data['created_at'] = firestore.SERVER_TIMESTAMP
    os_data['updated_at'] = firestore.SERVER_TIMESTAMP
    
    # Caminho do documento: /artifacts/{appId}/users/{userId}/ordens_servico/{os_id}
    appId = os.environ.get('RENDER_EXTERNAL_URL', 'default-app-id').split('//')[-1].split('.')[0]
    doc_path = f"artifacts/{appId}/users/{user_id}/ordens_servico/{os_id}"
    
    try:
        db.document(doc_path).set(os_data)
        return os_id
    except Exception as e:
        logger.error(f"Erro ao criar OS no Firestore: {e}")
        return None

def get_os_documents(user_id):
    """Busca todas as OS ativas do usuário."""
    if not db: return []
    try:
        appId = os.environ.get('RENDER_EXTERNAL_URL', 'default-app-id').split('//')[-1].split('.')[0]
        collection_path = f"artifacts/{appId}/users/{user_id}/ordens_servico"
        
        docs = db.collection(collection_path).order_by('created_at', direction=firestore.Query.DESCENDING).get()
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"Erro ao buscar OSs: {e}")
        return []

# --- Funções de Alerta/Agendamento (MUITO IMPORTANTE para Webhook) ---
# A função de agendamento (monitoramento de alertas) deve ser executada FORA do contexto do webhook.
# O Render executa o comando 'python3 bot_webhook.py'. O código deve iniciar o servidor web
# E, idealmente, um processo separado para o agendamento (cron job ou similar no próprio Render, mas aqui simulamos com um processo embutido, O QUE PODE SER INEFICIENTE).

# No modo Webhook no Render, o bot apenas responde a requisições HTTP do Telegram.
# Funções de agendamento (checar alertas periodicamente) devem ser executadas como um serviço separado (Render Cron Job).
# O código a seguir foca apenas na manipulação de mensagens. O agendamento seria implementado
# em um segundo serviço separado no Render, chamando uma rota da API.

# --- Funções Auxiliares do Telegram ---

def build_menu(buttons, n_cols, header_buttons=None, footer_buttons=None):
    """Função auxiliar para construir um InlineKeyboardMarkup."""
    menu = [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header_buttons:
        menu.insert(0, header_buttons)
    if footer_buttons:
        menu.append(footer_buttons)
    return InlineKeyboardMarkup(menu)

# --- Comandos do Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Envia a mensagem de boas-vindas e o menu principal."""
    if not db:
        await update.message.reply_text("❌ Falha na conexão com o Banco de Dados. O bot não pode ser iniciado. Por favor, verifique os logs no Render.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("➕ Criar Nova OS", callback_data='criar_os')],
        [InlineKeyboardButton("📜 Minhas OSs Ativas", callback_data='minhas_oss')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Olá! Eu sou seu assistente de Ordens de Serviço (OS). Escolha uma opção para começar:",
        reply_markup=reply_markup,
    )
    return MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a conversa e volta ao menu principal."""
    await update.message.reply_text("Operação cancelada. Voltando ao menu principal.")
    return await start(update, context) # Retorna para a função start para exibir o menu

async def fallback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trata comandos não reconhecidos."""
    await update.message.reply_text("Comando não reconhecido. Use /start para recomeçar.")

# --- Funções de Callback e Estados (Lógica Central) ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Trata todos os cliques de botões Inline."""
    query = update.callback_query
    await query.answer() # Fecha o pop-up de carregamento

    data = query.data
    
    # 1. MENU Principal
    if data == 'menu':
        keyboard = [
            [InlineKeyboardButton("➕ Criar Nova OS", callback_data='criar_os')],
            [InlineKeyboardButton("📜 Minhas OSs Ativas", callback_data='minhas_oss')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Menu Principal. Escolha uma opção:",
            reply_markup=reply_markup,
        )
        return MENU
    
    elif data == 'criar_os':
        context.user_data['current_os'] = {}
        await query.edit_message_text("Ótimo! Digite uma breve **descrição** para a nova OS (ex: 'Configuração de rede no Escritório A'):", parse_mode=ParseMode.MARKDOWN)
        return PROMPT_DESCRICAO

    elif data == 'minhas_oss':
        user_id = str(query.from_user.id)
        oss = get_os_documents(user_id)
        
        if not oss:
            text = "Você não tem nenhuma Ordem de Serviço registrada no momento."
            keyboard = [[InlineKeyboardButton("🔙 Menu Principal", callback_data='menu')]]
        else:
            text = "🔍 **Suas Ordens de Serviço Ativas:**\n\n"
            keyboard_buttons = []
            for os_item in oss:
                os_id = os_item.get('os_id', 'N/A')
                desc = os_item.get('descricao', 'Sem descrição')[:30] + '...'
                text += f"**ID:** `{os_id}` | {desc}\n"
                keyboard_buttons.append(InlineKeyboardButton(f"OS {os_id}", callback_data=f"ver_os_{os_id}"))
            
            # Divide os botões em linhas de 2
            keyboard = build_menu(keyboard_buttons, n_cols=2)
            keyboard.append([InlineKeyboardButton("🔙 Menu Principal", callback_data='menu')])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return PROMPT_OS
    
    # 2. Visualizar OS específica
    elif data.startswith('ver_os_'):
        os_id = data.split('_')[2]
        # (Implementar lógica de busca da OS pelo ID e exibir detalhes)
        # Para simplificar, vou apenas mostrar um menu de ação
        context.user_data['current_os_id'] = os_id
        
        text = f"Detalhes da OS **{os_id}** (Status: Pendente). O que deseja fazer?"
        keyboard = [
            [InlineKeyboardButton("✏️ Atualizar Status", callback_data=f'atualizar_os_{os_id}')],
            [InlineKeyboardButton("🔔 Gerenciar Alertas", callback_data=f'gerenciar_alerta_{os_id}')],
            [InlineKeyboardButton("🗑️ Excluir OS", callback_data=f'excluir_os_{os_id}')],
            [InlineKeyboardButton("🔙 Minhas OSs", callback_data='minhas_oss')],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return PROMPT_ATUALIZACAO

    # ... Adicione mais lógica de tratamento de callbacks aqui ...
    
    return context.user_data.get('state', MENU) # Retorna para o estado atual se não houver mudança

async def receive_os_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a descrição da nova OS e pede o tipo."""
    descricao = update.message.text
    context.user_data['current_os']['descricao'] = descricao
    
    text = f"Descrição salva: *{descricao}*. Agora, qual o **tipo** desta OS?"
    keyboard = [
        [InlineKeyboardButton("⚙️ Manutenção", callback_data='tipo_manutencao'), InlineKeyboardButton("💻 Suporte", callback_data='tipo_suporte')],
        [InlineKeyboardButton("☁️ Infraestrutura", callback_data='tipo_infra'), InlineKeyboardButton("📝 Outros", callback_data='tipo_outros')],
        [InlineKeyboardButton("🔙 Cancelar", callback_data='menu')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return PROMPT_TIPO

async def finalize_os(update: Update, context: ContextTypes.DEFAULT_TYPE, tipo: str) -> int:
    """Finaliza a criação da OS e salva no Firebase."""
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    os_data = context.user_data['current_os']
    os_data['tipo'] = tipo
    os_data['status'] = 'Pendente' # Status inicial padrão
    
    os_id = create_os_document(chat_id, user_id, os_data)

    if os_id:
        text = f"✅ OS **{os_id}** criada com sucesso!\n\n**Descrição:** {os_data['descricao']}\n**Tipo:** {os_data['tipo']}\n\nVocê pode gerenciá-la no menu 'Minhas OSs Ativas'."
    else:
        text = "❌ Erro ao salvar a OS no Firebase. Por favor, tente novamente mais tarde."

    keyboard = [[InlineKeyboardButton("🔙 Menu Principal", callback_data='menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Verifica se a mensagem veio de um callback query ou de uma message
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    context.user_data.pop('current_os', None)
    return MENU

# Lógica para tratamento do TIPO da OS
async def handle_os_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data.startswith('tipo_'):
        os_type = query.data.split('_')[1].capitalize()
        return await finalize_os(update, context, os_type)

    return PROMPT_TIPO

# Placeholder functions for remaining states to ensure the ConversationHandler is complete
async def update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Lógica de atualização de status será implementada aqui.")
    return PROMPT_STATUS

async def alert_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Lógica de gerenciamento de alertas será implementada aqui.")
    return PROMPT_ALERTA

async def receive_alerta_descricao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Lógica para receber a descrição do alerta será implementada aqui.")
    return PROMPT_ID_ALERTA

async def receive_alerta_prazo_or_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Lógica para receber o prazo ou o ID do alerta será implementada aqui.")
    return PROMPT_ID_ALERTA

# --- Função Principal ---

def main() -> None:
    """Inicia o bot no modo Webhook."""
    
    # Verifica a conexão com o Firebase novamente
    if db is None:
        logger.error("O bot não pode iniciar sem a conexão com o Firebase.")
        return

    # 1. Obtenção das variáveis do Render
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    if not TOKEN or not WEBHOOK_URL:
        logger.error("O bot não pode iniciar: TELEGRAM_BOT_TOKEN ou WEBHOOK_URL não configurados.")
        return

    # 2. Configura a Aplicação
    application = Application.builder().token(TOKEN).build()

    # Define a porta, que é fornecida pelo Render (geralmente é 10000)
    PORT = int(os.environ.get("PORT", "8080"))

    # 3. Define o ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [
                CallbackQueryHandler(callback_handler, pattern='^criar_os$|^minhas_oss$'),
            ],
            PROMPT_DESCRICAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_os_descricao),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_TIPO: [
                CallbackQueryHandler(handle_os_tipo, pattern='^tipo_'),
                CallbackQueryHandler(callback_handler, pattern='^menu$'),
            ],
            PROMPT_OS: [
                CallbackQueryHandler(callback_handler, pattern='^ver_os_|^menu$'),
            ],
            PROMPT_ATUALIZACAO: [
                CallbackQueryHandler(callback_handler, pattern='^atualizar_os_|^gerenciar_alerta_|^excluir_os_|^minhas_oss$|^menu$'),
            ],
            PROMPT_STATUS: [
                CallbackQueryHandler(update_handler, pattern='^menu$|atualizar_existente'),
            ],
            PROMPT_ALERTA: [
                CallbackQueryHandler(callback_handler, pattern='^menu$|alerta_existente|criar_alerta|remover_alerta_menu'),
            ],
            PROMPT_INCLUSAO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_descricao),
                CallbackQueryHandler(callback_handler, pattern='^alerta_existente$'),
            ],
            PROMPT_ID_ALERTA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alerta_prazo_or_id),
                CallbackQueryHandler(callback_handler, pattern='^alerta_existente$'),
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
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=WEBHOOK_URL + '/' + TOKEN,
        )
        logger.info(f"Servidor Webhook iniciado e escutando na porta {PORT}.")
        logger.info(f"Webhook URL configurada no Telegram: {WEBHOOK_URL + '/' + TOKEN}")
    except Exception as e:
        logger.error(f"Falha ao iniciar o Webhook: {e}")

if __name__ == "__main__":
    main()
