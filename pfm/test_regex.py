import re

raws = [
    """Ratings:
1. [Tata Power] Score: 8/10 - Reason: Juniper Green Energy sign PPA.
2. [Paytm] Score: 3/10 - Reason: Unclear sentiment.
3. [LIC] Score: 5/10 - Reason: Earnings report.
4. [Apple] Score: 2/10 - Reason: Supply crunch.
5. [Voda Idea] Score: 2/10 - Reason: Notice from DoT.""",

    """[TATAPOWER] - Score 8/10: Juniper Green Energy
[PAYTM] - 3/10, Unclear sentiment
[LICI] Score: 5/10 - Earnings report
[AAPL] 2/10. Supply crunch
[IDEA] Score:2/10 Reason: Notice""",
]

batch_data = {'TATAPOWER': [], 'PAYTM': [], 'LICI': [], 'AAPL': [], 'IDEA': []}

for raw in raws:
    print(f"\n--- Testing RAW ---\n{raw}\n-------------------")
    
    parsed = {}
    symbols_pattern = "|".join(map(re.escape, batch_data.keys()))
    patterns = [
        re.compile(rf'\[?({symbols_pattern})\]?[\s:\-\u2013]*(?:Score:\s*)?(\d+)(?:/10)?\s*[-\u2013]\s*(?:Reason:\s*)?([^\n]+)', re.IGNORECASE),
        re.compile(rf'\[?({symbols_pattern})\]?[\s:\-\u2013]*(?:Score:\s*)?(\d+)(?:/10)?\s*Reason:\s*([^\n]+)', re.IGNORECASE),
    ]
    for pattern in patterns:
        for m in pattern.finditer(raw):
            sym = m.group(1).upper()
            if sym in batch_data and sym not in parsed:
                parsed[sym] = (m.group(2), m.group(3).strip())
        if len(parsed) == len(batch_data):
            break

    print(f"Strict Regex found: {parsed}")

    if len(parsed) < len(batch_data):
        scores_found = []
        # Fallback: MUST have either "Score: X", "X/10", or "Score X"
        fallback_pattern = re.compile(r'(?:Score:?\s*(\d+)|(\d+)/10)(?:/10)?[\s\-\u2013,.:]*(?:Reason:?\s*)?([^\n]+)', re.IGNORECASE)
        for line in raw.split('\n'):
            line = line.strip()
            if not line: continue
            
            # Skip lines that don't look like they have a rating
            m = fallback_pattern.search(line)
            if m:
                score = m.group(1) or m.group(2)
                reason = m.group(3).strip()
                # Clean up reason if it has leading dashes or '/10 -'
                reason = re.sub(r'^(?:/10)?[\s\-\u2013,.:]*(?:Reason:\s*)?', '', reason, flags=re.IGNORECASE)
                scores_found.append((score, reason))
                    
        print(f"Fallback extracted: {scores_found}")
        
        if len(scores_found) == len(batch_data):
            parsed = {}
            for i, sym in enumerate(batch_data.keys()):
                parsed[sym] = scores_found[i]
                
    print(f"Final Parsed: {parsed}")
