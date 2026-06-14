"""Put the app/ directory on sys.path so tests can `import core`, `import bot`,
`import market_brief`, etc. regardless of where pytest is invoked from."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
