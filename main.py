import sys
from pathlib import Path

# Add 'agent' directory to Python path
agent_dir = Path(__file__).resolve().parent / "agent"
if str(agent_dir) not in sys.path:
    sys.path.insert(0, str(agent_dir))

# pyrefly: ignore [missing-import]
from backend.main import app  # noqa: F401