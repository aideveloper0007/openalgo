import json
import os
from pathlib import Path
from datetime import datetime
import glob

config_file = Path("strategies/strategy_configs.json")
if config_file.exists():
    with open(config_file, "r") as f:
        configs = json.load(f)
else:
    configs = {}

bjp_dir = Path("strategies/bjp_portfolio")
strategy_files = [f for f in bjp_dir.glob("*.py") if f.name != "bjp_core.py" and not f.name.startswith("__")]

for script in strategy_files:
    # Use stem as ID
    strategy_id = f"bjp_{script.stem}"
    
    # If not already registered, add it
    if strategy_id not in configs:
        exchange = "BSE" if "sensex" in script.stem else "NSE"
        name = "BJP " + script.stem.replace("_", " ").title()
        
        configs[strategy_id] = {
            "name": name,
            "file_path": str(script),
            "file_name": script.name,
            "exchange": exchange,
            "is_running": False,
            "is_scheduled": False,
            "created_at": datetime.now().astimezone().isoformat(),
            "user_id": "admin", # Default or fallback
            "schedule_start": "09:15",
            "schedule_stop": "15:30",
            "schedule_days": ["mon", "tue", "wed", "thu", "fri"]
        }
        print(f"Registered {name} ({strategy_id})")

with open(config_file, "w") as f:
    json.dump(configs, f, indent=2)

print("Registration complete. You can now start/schedule them in the OpenAlgo Strategy Host.")
