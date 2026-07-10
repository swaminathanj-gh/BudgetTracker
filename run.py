"""Convenience entry point so you can run `python3 run.py` from the project
root instead of `python3 -m app.main` (main.py uses relative imports within
the app package, so it can't be run directly as a script)."""
from app.main import main

if __name__ == "__main__":
    main()
