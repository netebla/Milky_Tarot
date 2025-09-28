import csv
import random
import asyncio
from llm.client import ask_llm

CSV_FILE = "src/data/cards.csv"

def get_random_cards(n=3):
    cards = []
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")  # только названия
        for row in reader:
            cards.append(row[0].strip())
    return random.sample(cards, n)

async def main():
    cards = get_random_cards()
    prompt = f"Сделай трактовку расклада три карты: {', '.join(cards)}"
    print("Промпт:", prompt)
    response = await ask_llm(prompt)
    print("\nОтвет LLM:\n", response)

if __name__ == "__main__":
    asyncio.run(main())