import os
import logging
import asyncio
import time
import discord
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from keep_alive import keep_alive

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv('USER_TOKEN')
API_URL = os.getenv('API_URL')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
TOPIC_COMPLAINTS = os.getenv('TELEGRAM_TOPIC_COMPLAINTS')
TOPIC_MEMBERS = os.getenv('TELEGRAM_TOPIC_MEMBERS')
TOPIC_TICKETS = os.getenv('TELEGRAM_TOPIC_TICKETS')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('discord_selfbot')

def escape_markdown(text):
    """Helper to escape Markdown v1 characters."""
    if not text:
        return ""
    # Escape characters that have special meaning in Markdown V1
    for char in ['_', '*', '`', '[']:
        text = str(text).replace(char, f'\\{char}')
    return text

def send_telegram_alert(text: str, thread_id: str = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
        
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f'Failed to send telegram alert: {e}')

def send_telegram_join_alert(member: discord.Member):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    # Calculate account age
    now = datetime.now(timezone.utc)
    account_age_days = (now - member.created_at).days
    
    # Format dates
    joined_date = member.joined_at.strftime("%d/%m/%Y %H:%M") if member.joined_at else "Unknown"
    created_date = member.created_at.strftime("%d/%m/%Y %H:%M")
    
    # Avatar URL
    if hasattr(member, 'display_avatar') and member.display_avatar:
        avatar_url = member.display_avatar.url
    elif hasattr(member, 'avatar') and member.avatar:
        avatar_url = member.avatar.url
    else:
        avatar_url = "No Avatar"
    
    # Mutual servers count
    mutual_guilds = getattr(member, 'mutual_guilds', [])
    mutual_count = len(mutual_guilds) if isinstance(mutual_guilds, list) else 0

    text = (
        f"🛑 *{escape_markdown(member.guild.name)}* 🛑\n\n"
        f"👤 *Display Name*: {escape_markdown(member.display_name)}\n"
        f"💬 *Username*: {escape_markdown(member.name)}\n"
        f"⌛ *Account Age*: {account_age_days} days\n\n"
        f"📅 *Joined*: {joined_date}\n"
        f"🎂 *Account Created*: {created_date}\n\n"
        f"🖼️ [👁️ Avatar]({avatar_url})\n"
        f"🤝 {mutual_count} mutual servers"
    )
    
    
    # Send to Telegram Topic
    send_telegram_alert(text, thread_id=TOPIC_MEMBERS)

def send_to_api(payload: dict, retries: int = 3):
    """Send message payload to Django API with exponential backoff."""
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                API_URL,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=10,
            )
            if response.status_code in (200, 201):
                logger.info(f'📨 API SUCCESS: {payload["author_name"]} -> {payload["channel_name"]}')
                return
            else:
                logger.warning(f'⚠️ API ERROR {response.status_code}: {response.text[:100]}')
        except Exception as e:
            logger.warning(f'🔄 RETRY {attempt}/{retries} due to error: {e}')
        
        if attempt < retries:
            time.sleep(2 ** attempt)
    logger.error(f'❌ FINAL FAILURE: Could not send message {payload["discord_id"]}')

API_RULES_URL = os.getenv('API_RULES_URL')

class IntelSelfBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_rules = []

    async def update_rules_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, requests.get, API_RULES_URL)
                if response.status_code == 200:
                    self.active_rules = response.json()
                    logger.info(f"🔄 Updated active alert rules (count: {len(self.active_rules)})")
            except Exception as e:
                logger.error(f"Failed to fetch alert rules: {e}")
            await asyncio.sleep(60)

    async def on_ready(self):
        logger.info(f'✅ Connected as: {self.user} (ID: {self.user.id})')
        logger.info(f'📡 Monitoring {len(self.guilds)} servers')
        self.loop.create_task(self.update_rules_loop())

    async def on_member_join(self, member: discord.Member):
        logger.info(f"New member joined: {member.display_name} in {member.guild.name}")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, send_telegram_join_alert, member)

    async def on_guild_channel_create(self, channel):
        """Detect new support ticket channels."""
        if isinstance(channel, discord.TextChannel):
            name = channel.name.lower()
            if 'ticket' in name or 'support' in name:
                logger.info(f"Ticket detected: {channel.name} in {channel.guild.name}")
                
                creator_info = "Unknown"
                try:
                    # Give the bot/server a moment to set permissions
                    await asyncio.sleep(2)
                    
                    # Find a member who has specific permission overwrites in this channel
                    # Ticket bots usually add the user as a specific member overwrite
                    for member_id, overwrite in channel.overwrites.items():
                        if isinstance(member_id, discord.Member) and not member_id.bot:
                            creator_info = f"{escape_markdown(member_id.display_name)} (@{escape_markdown(member_id.name)})"
                            break
                    
                    # Fallback: If no member overwrite, check audit logs (might still be the bot)
                    if creator_info == "Unknown":
                        async for entry in channel.guild.audit_logs(action=discord.AuditLogAction.channel_create, limit=5):
                            if entry.target.id == channel.id:
                                creator = entry.user
                                creator_info = f"{escape_markdown(creator.display_name)} (@{escape_markdown(creator.name)})"
                                break
                except Exception as e:
                    logger.warning(f"Could not fetch ticket creator info: {e}")

                text = (
                    f"🎫 *New Support Ticket*\n\n"
                    f"👤 *Created By*: {creator_info}\n"
                    f"🏠 *Server*: {escape_markdown(channel.guild.name)}\n"
                    f"📂 *Channel*: #{escape_markdown(channel.name)}\n"
                    f"🔗 [Jump to Channel](https://discord.com/channels/{channel.guild.id}/{channel.id})"
                )
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, send_telegram_alert, text, TOPIC_TICKETS)

    async def on_message(self, message: discord.Message):
        # Filter intelligence: Ignore bot messages and ignore DMs (no guild)
        if message.author.bot or not message.guild:
            return

        content = message.content or ''
        content_lower = content.lower()
        matched = False

        # Evaluate rules locally
        for rule in self.active_rules:
            keyword_raw = rule.get('keyword', '').strip()
            is_regex = rule.get('is_regex', False)

            if is_regex:
                import re
                try:
                    if re.search(keyword_raw, content, re.IGNORECASE):
                        matched = True
                        break
                except Exception:
                    pass
            else:
                if keyword_raw.lower() in ('all', '*'):
                    matched = True
                    break
                elif ',' in keyword_raw:
                    keywords = [kw.strip().lower() for kw in keyword_raw.split(',') if kw.strip()]
                    if any(kw in content_lower for kw in keywords):
                        matched = True
                        break
                else:
                    if keyword_raw.lower() in content_lower:
                        matched = True
                        break

        if not matched:
            return

        payload = {
            'discord_id': str(message.id),
            'content': content,
            'author_name': str(message.author.display_name),
            'author_username': str(message.author.name),
            'author_id': str(message.author.id),
            'channel_id': str(message.channel.id),
            'channel_name': str(message.channel.name),
            'server_id': str(message.guild.id),
            'server_name': str(message.guild.name),
            'created_at': message.created_at.isoformat(),
        }

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, send_to_api, payload)

if __name__ == '__main__':
    if not DISCORD_TOKEN:
        logger.error('❌ DISCORD_TOKEN missing in .env file!')
        exit(1)

    logger.info('🚀 Launching Selfbot Intelligence...')

    # KEEP ALIVE - START FLASK APP
    keep_alive()

    MAX_RETRIES = 5
    retry_count = 0
    
    while retry_count < MAX_RETRIES:
        try:
            bot = IntelSelfBot()
            bot.run(DISCORD_TOKEN)
            break
        except discord.LoginFailure:
            logger.error('❌ Invalid Token! Please check your DISCORD_TOKEN.')
            break
        except Exception as e:
            retry_count += 1
            logger.error(f'❌ Runtime Error Attempt {retry_count}/{MAX_RETRIES}: {e}')
            if retry_count < MAX_RETRIES:
                time.sleep(5 * retry_count)
            else:
                exit(1)
