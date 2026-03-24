import sys
import logging

def test_imports():
    print("Attempting to import src.main...")
    try:
        from src.main import bot
        print("Successfully imported src.main.bot")
    except ImportError as e:
        print(f"Failed to import src.main: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred during import: {e}")
        sys.exit(1)

    print("Attempting to import src.config...")
    try:
        from src.config import CONFIG
        print("Successfully imported src.config.CONFIG")
    except Exception as e:
        print(f"Failed to import src.config: {e}")
        sys.exit(1)

    print("All imports successful.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_imports()
