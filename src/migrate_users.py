import json
from utils.db import SessionLocal, User

# читаем старую JSON базу
with open("src/data/users.json", "r", encoding="utf-8") as f:
    data = json.load(f)

session = SessionLocal()

for user_id, u in data.get("users", {}).items():
    user = User(
        id=int(user_id),
        username=u.get("username"),
        registered_at=u.get("registered_at"),
        push_time=u.get("push_time"),
        push_enabled=u.get("push_enabled"),
        last_card=u.get("last_card"),
        last_card_date=u.get("last_card_date"),
        last_activity_date=u.get("last_activity_date"),
        draw_count=u.get("draw_count", 0),
    )
    session.merge(user)  # merge чтобы не дублировать
session.commit()
session.close()
print("Миграция завершена")
