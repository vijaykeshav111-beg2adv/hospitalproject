# Run this ONE time from the project folder:
#     python fix_email_domain.py
# It changes  vijayvargiiyahospital  ->  vijayvargiyahospital  in all project text files.

from pathlib import Path

OLD = "vijayvargiiyahospital"
NEW = "vijayvargiyahospital"

SKIP_FOLDERS = {".git", ".venv", "venv", "__pycache__", "node_modules", "storage"}
FILE_TYPES = {".py", ".html", ".md", ".sql", ".txt", ".json", ".example", ".env", ".yml", ".yaml", ".toml"}

changed_files = 0
changed_places = 0

for path in Path(".").rglob("*"):
    if not path.is_file():
        continue

    # never change this script itself
    if path.name == Path(__file__).name:
        continue

    # skip big/unwanted folders
    if any(part in SKIP_FOLDERS for part in path.parts):
        continue

    # only look at text files
    if path.suffix.lower() not in FILE_TYPES and path.name not in {".env", ".env.example"}:
        continue

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue

    count = text.count(OLD)
    if count == 0:
        continue

    path.write_text(text.replace(OLD, NEW), encoding="utf-8")
    changed_files += 1
    changed_places += count
    print(f"fixed {count:3d}  {path}")

print()
print(f"Done. {changed_places} places fixed in {changed_files} files.")