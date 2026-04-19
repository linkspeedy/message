import os
import logging
import asyncio
import time
import discord
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv('USER_TOKEN')
API_URL = os.getenv('API_URL')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('discord_selfbot')

def send_telegram_join_alert(member: discord.Member):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = (
        f"👋 *New Member Joined*\n"
        f"👤 User: {member.display_name}\n"
        f"🏠 Server: {member.guild.name}\n"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f'Failed to send telegram join alert: {e}')

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

API_RULES_URL = os.getenv('API_RULES_URL', 'http://127.0.0.1:8000/api/messages/rules/')

class IntelSelfBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_rules = []

    async def update_rules_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                # Run sync requests in an executor
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, requests.get, API_RULES_URL)
                if response.status_code == 200:
                    self.active_rules = response.json()
                    logger.info(f"🔄 Updated active alert rules (count: {len(self.active_rules)})")
            except Exception as e:
                logger.error(f"Failed to fetch alert rules: {e}")
            await asyncio.sleep(60) # check every 60 seconds

    async def on_ready(self):
        logger.info(f'✅ Connected as: {self.user} (ID: {self.user.id})')
        logger.info(f'📡 Monitoring {len(self.guilds)} servers')
        self.loop.create_task(self.update_rules_loop())

    async def on_member_join(self, member: discord.Member):
        logger.info(f"New member joined: {member.display_name} in {member.guild.name}")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, send_telegram_join_alert, member)

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

        # Only send to API if there is a match
        if not matched:
            return

        # Prepare Intelligence Payload
        payload = {
            'discord_id': str(message.id),
            'content': content,
            'author_name': str(message.author.display_name),
            'author_id': str(message.author.id),
            'channel_id': str(message.channel.id),
            'channel_name': str(message.channel.name),
            'server_id': str(message.guild.id),
            'server_name': str(message.guild.name),
            'created_at': message.created_at.isoformat(),
        }

        # Handle API forwarding in a background thread to prevent bot lag
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, send_to_api, payload)

if __name__ == '__main__':
    if not DISCORD_TOKEN:
        logger.error('❌ DISCORD_TOKEN missing in .env file!')
        exit(1)

    logger.info('🚀 Launching Selfbot Intelligence...')

    MAX_RETRIES = 5
    retry_count = 0
    
    while retry_count < MAX_RETRIES:
        try:
            # discord.py-self doesn't use standard intents, selfbots use implicit scopes
            bot = IntelSelfBot()

            # discord.py-self implicitly knows it's a user token
            bot.run(DISCORD_TOKEN)
            break
        except discord.LoginFailure:
            logger.error('❌ Invalid Token! Please check your DISCORD_TOKEN.')
            break  # Stop retrying on invalid token
        except Exception as e:
            retry_count += 1
            logger.error(f'❌ Runtime Error Attempt {retry_count}/{MAX_RETRIES}: {e}')
            if retry_count < MAX_RETRIES:
                time.sleep(5 * retry_count)
            else:
                exit(1)