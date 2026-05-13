import sys

path = "migration_checkout.py"
with open(path, "rb") as f:
    data = f.read()

old = b'    payload = json.dumps(data, indent=2).replace("\\n", "\\r\\n") + "\\r\\n"'
new = b'    payload = json.dumps(data, indent=2).replace("\\n", "\\r\\n")'

if old not in data:
    print("ERROR: target bytes not found")
    sys.exit(1)
count = data.count(old)
if count != 1:
    print(f"ERROR: target appears {count} times, expected exactly 1")
    sys.exit(1)

new_data = data.replace(old, new)
with open(path, "wb") as f:
    f.write(new_data)
print("patched, bytes:", len(data), "->", len(new_data))
