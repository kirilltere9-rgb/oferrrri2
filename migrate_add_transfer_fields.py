"""
Миграция: добавление полей accepted_at, transfer_confirmed, transfer_confirmed_at в таблицу gift_offers
"""
import sqlite3
from pathlib import Path

def migrate():
    db_path = Path("data/offers.db")
    
    if not db_path.exists():
        print("База данных не найдена. Пропускаем миграцию.")
        return
    
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    
    try:
        # Проверяем, существуют ли уже поля
        cur.execute("PRAGMA table_info(gift_offers)")
        columns = [row[1] for row in cur.fetchall()]
        
        if "accepted_at" not in columns:
            print("Добавляем поле accepted_at...")
            cur.execute("ALTER TABLE gift_offers ADD COLUMN accepted_at TIMESTAMP")
        
        if "transfer_confirmed" not in columns:
            print("Добавляем поле transfer_confirmed...")
            cur.execute("ALTER TABLE gift_offers ADD COLUMN transfer_confirmed INTEGER DEFAULT 0")
        
        if "transfer_confirmed_at" not in columns:
            print("Добавляем поле transfer_confirmed_at...")
            cur.execute("ALTER TABLE gift_offers ADD COLUMN transfer_confirmed_at TIMESTAMP")
        
        con.commit()
        print("✅ Миграция успешно выполнена!")
        
    except Exception as e:
        print(f"❌ Ошибка при миграции: {e}")
        con.rollback()
    finally:
        con.close()

if __name__ == "__main__":
    migrate()
