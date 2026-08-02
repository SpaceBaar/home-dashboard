import re
import difflib

raws = [
    """Stock: Tata Power
Score: 8/10
Reason: Juniper Green Energy sign PPA.

Company: Paytm
Score: 3/10
Reason: Unclear sentiment.

Symbol: LIC
Score: 5
Reason: Earnings report.

Stock: Apple
Score: 2/10
Reason: Supply crunch.
It also affects Mac sales.

Stock: Voda Idea
Score: 2
Justification: Notice from DoT.""",
]

batch_data = {'TATAPOWER': [], 'PAYTM': [], 'LICI': [], 'AAPL': [], 'IDEA': []}
symbols = list(batch_data.keys())

for raw in raws:
    parsed = {}
    current_sym = None
    current_score = None
    
    for line in raw.split('\n'):
        line = line.strip()
        if not line: continue
        
        m_stock = re.match(r'^(?:Stock|Symbol|Company)[\s:]+([A-Za-z0-9_&\s]+)', line, re.IGNORECASE)
        if m_stock:
            name = m_stock.group(1).upper().strip()
            name_nospace = name.replace(" ", "")
            best_sym = None
            for sym in symbols:
                if sym == name_nospace or sym in name_nospace or name_nospace in sym:
                    best_sym = sym
                    break
            if not best_sym:
                matches = difflib.get_close_matches(name_nospace, symbols, n=1, cutoff=0.3)
                if matches: best_sym = matches[0]
            
            if best_sym:
                current_sym = best_sym
                current_score = None
            continue
            
        m_score = re.search(r'Score[\s:]+(\d+)', line, re.IGNORECASE)
        if m_score and current_sym:
            current_score = m_score.group(1)
            if current_sym not in parsed:
                parsed[current_sym] = [current_score, ""]
            continue
            
        m_reason = re.match(r'^(?:Reason|Justification)[\s:]+(.+)', line, re.IGNORECASE)
        if m_reason and current_sym and current_score:
            parsed[current_sym][1] = m_reason.group(1).strip()
            # Don't reset current_sym, in case reason spans multiple lines
            continue
            
        # If we are inside a reason block, append lines
        if current_sym and current_score and current_sym in parsed:
            # Only append if it doesn't look like a new stock starting (safety net)
            if not re.match(r'^(?:Stock|Symbol|Company|Score)[\s:]+', line, re.IGNORECASE):
                if parsed[current_sym][1]:
                    parsed[current_sym][1] += " " + line
                else:
                    parsed[current_sym][1] = line

    print(f"Final Parsed: {parsed}")
