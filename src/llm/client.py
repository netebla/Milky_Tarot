import os
import aiohttp

OPENAI_API_KEY = "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"

async def ask_llm(prompt: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": prompt}],
            },
        ) as resp:
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

