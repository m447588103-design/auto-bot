import os
import asyncio
from dotenv import load_dotenv
import discord
from agent import process_agent_command

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user} (Autonomous Agent Online)')

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content.startswith("!agent"):
        command = message.content[len("!agent"):].strip()
        await message.channel.send(f"🤖 Processing command: `{command}`...")
        
        response = await process_agent_command(command)
        await message.channel.send(response)

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_BOT_TOKEN not found in environment variables.")
    else:
        client.run(TOKEN)
