import os
import google.generativeai as genai
from dotenv import load_dotenv
from automation import run_browser_task

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Initialize Gemini Model
model = genai.GenerativeModel("gemini-2.5-flash")

async def process_agent_command(prompt: str) -> str:
    try:
        # If the prompt involves browser tasks
        if "browse" in prompt.lower() or "scrape" in prompt.lower() or "search" in prompt.lower():
            result = await run_browser_task(prompt)
            return f"🌐 Browser Automation Result:\n{result}"
        
        # General AI reasoning and code generation
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"❌ Error executing command: {str(e)}"
