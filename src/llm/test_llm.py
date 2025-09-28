import csv
import random
from llm.client import ask_llm

CSV_FILE = "src/data/cards_advice.csv"

def get_random_cards(n=3):
    cards = []
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            cards.append(row[0].strip())
    return random.sample(cards, n)

async def get_three_card_reading():
    cards = get_random_cards()
    prompt = f"Сделай трактовку расклада три карты: {', '.join(cards)}"
    return await ask_llm(prompt)