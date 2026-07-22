import time

print("=" * 50)
print("✅ TEST BOT STARTED!")
print("=" * 50)

counter = 0
while True:
    counter += 1
    time.sleep(10)
    print(f"🔄 Heartbeat {counter} - Still running!")