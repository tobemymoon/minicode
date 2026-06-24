import sys 
import re 
from collections import defaultdict 

def normalize_space(s):
    return " ".join(s.strip().split())

def extract_words(s):
    return re.findall(r"[A-Za-z0-9]+",s.lower())

def solve():
    input = sys.stdin.readline

    N, L, R, M, K = map(int, input().split())

    blacklist = set()
    if K > 0:
        blacklist = set(input().strip().split())

    seen = set()
    ans = [] 
    for i in range(N):
        raw = input().rstrip("\n")


        doc = normalize_space(raw)

        if not (L <= len(doc) <= R):
            continue 

        words = extract_words(doc)
        bad = False 

        for w in words:
            if w in blacklist:
                bad = True
                break 

        if bad:
            continue 

        cnt = defaultdict(int)
        for i in range(len(words) - 2):
            gram = (words[i], words[i+1], words[i+2])
            cnt[gram] += 1
            if cnt[gram] > M:
                bad = True
                break 
        
        if bad:
            continue 

        key = tuple(words)
        if key in seen:
            continue 

        seen.add(key)
        ans.append(doc) 

    sys.stdout.write("\n".join(ans))

if __name__ == "__main__":
    solve()
