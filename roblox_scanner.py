import requests
import concurrent.futures
import re

START_ID = 51912925
END_ID = 51922925
PATTERNS = [re.compile(r'1234'), re.compile(r'123'), re.compile(r'12')]

def check_user(user_id):
    url = f"https://users.roblox.com/v1/users/{user_id}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            username = data.get("name", "")
            
            matched = None
            for p in PATTERNS:
                if p.search(username):
                    matched = p.pattern
                    break
            
            if matched:
                print(f"[FOUND] ID: {user_id} | Username: {username} | Pattern: {matched}")
    except Exception:
        pass

def main():
    print(f"Starting scan for IDs from {START_ID} to {END_ID}...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(check_user, uid): uid for uid in range(START_ID, END_ID + 1)}
        for future in concurrent.futures.as_completed(futures):
            uid = futures[future]
            if uid % 2000 == 0:
                print(f"Progress: Checked up to ID {uid}")
    print("Scan complete!")

if __name__ == "__main__":
    main()
