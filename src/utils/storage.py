"""
Утилиты для постоянного хранения данных в JSON, используемые ботом.

- Расположение файлов (относительно корня проекта):
  - src/data/users.json

Модуль предоставляет:
- JsonStorage: низкоуровневую безопасную работу с JSON (чтение/запись) с кэшированием в памяти
- UserStorage: высокоуровневые помощники для работы с профилями пользователей и статистикой

Хранилище подходит для однопроцессного бота (наш случай).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, date
from typing import Any, Dict, Optional, Tuple

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
USERS_PATH = os.path.join(DATA_DIR, "users.json")


class JsonStorage:
    """Простое файловое хранилище JSON с блокировкой на уровне процесса и кэшем."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None
        self._ensure_parent_dir()
        self._ensure_file_exists()

    def _ensure_parent_dir(self) -> None:
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)

    def _ensure_file_exists(self) -> None:
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)

    def read(self) -> Dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except json.JSONDecodeError:
                # Восстановление после повреждения файла
                self._cache = {}
            return self._cache

    def write(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._cache = data
            temp_path = f"{self.file_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.file_path)

    def update(self, updater) -> Dict[str, Any]:
        with self._lock:
            data = self.read().copy()
            updater(data)
            self.write(data)
            return data


class UserStorage:
    """Высокоуровневое хранилище пользователей и статистики в users.json.

    Пример структуры:
    {
      "users": {
        "123456": {
          "id": 123456,
          "username": "john",
          "registered_at": "2025-09-23T10:00:00Z",
          "push_time": "10:00",
          "push_enabled": true,
          "last_card": "Шут",
          "last_card_date": "2025-09-23",
          "last_activity_date": "2025-09-23",
          "draw_count": 12
        }
      }
    }
    """

    DEFAULT_PUSH_TIME = "10:00"  # По умолчанию (часовой пояс Europe/Moscow)

    def __init__(self, storage: Optional[JsonStorage] = None) -> None:
        self.storage = storage or JsonStorage(USERS_PATH)

    # Базовые помощники
    def _now_iso(self) -> str:
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    def _today_str(self) -> str:
        return date.today().isoformat()

    def get_all(self) -> Dict[str, Any]:
        data = self.storage.read()
        if "users" not in data:
            data = self.storage.update(lambda d: d.setdefault("users", {}))
        return data

    def get_users(self) -> Dict[str, Dict[str, Any]]:
        return self.get_all().get("users", {})

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self.get_users().get(str(user_id))

    def ensure_user(self, user_id: int, username: Optional[str]) -> Dict[str, Any]:
        def _updater(d: Dict[str, Any]):
            users = d.setdefault("users", {})
            user = users.get(str(user_id))
            if not user:
                users[str(user_id)] = {
                    "id": user_id,
                    "username": username,
                    "registered_at": self._now_iso(),
                    "push_time": self.DEFAULT_PUSH_TIME,
                    "push_enabled": True,
                    "last_card": None,
                    "last_card_date": None,
                    "last_activity_date": self._today_str(),
                    "draw_count": 0,
                }
            else:
                # Обновляем имя и активность при любом контакте
                user["username"] = username
                user["last_activity_date"] = self._today_str()
        self.storage.update(_updater)
        return self.get_user(user_id) or {}

    def set_push_time(self, user_id: int, time_str: str) -> None:
        def _updater(d: Dict[str, Any]):
            d.setdefault("users", {}).setdefault(str(user_id), {})["push_time"] = time_str
        self.storage.update(_updater)

    def set_push_enabled(self, user_id: int, enabled: bool) -> None:
        def _updater(d: Dict[str, Any]):
            d.setdefault("users", {}).setdefault(str(user_id), {})["push_enabled"] = enabled
        self.storage.update(_updater)

    def set_last_card(self, user_id: int, card_title: str) -> None:
        today = self._today_str()
        def _updater(d: Dict[str, Any]):
            user = d.setdefault("users", {}).setdefault(str(user_id), {})
            user["last_card"] = card_title
            user["last_card_date"] = today
            user["last_activity_date"] = today
            user["draw_count"] = int(user.get("draw_count", 0)) + 1
        self.storage.update(_updater)

    # Статистика
    def get_stats(self) -> Tuple[int, int, int]:
        users = self.get_users()
        total_users = len(users)
        today = self._today_str()
        active_today = sum(1 for u in users.values() if u.get("last_activity_date") == today)
        total_draws = sum(int(u.get("draw_count", 0)) for u in users.values())
        return total_users, active_today, total_draws 