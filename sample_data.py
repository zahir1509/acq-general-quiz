"""
Demo data: 20 teams and 20 questions, enough to rehearse a whole quiz.

    python3 server.py --seed
"""

TEAMS = [
    "Biryani Boys", "Quiz Khalifa", "The Puzzlers", "Ctrl Alt Defeat", "Sher Khan",
    "Chai Pe Charcha", "Bounce Back", "Gully Boys", "The Mavericks", "Filter Coffee",
    "Aaj Kal Parso", "Wikipedia Warriors", "Rapid Fire", "Jugaad Inc", "Masala Dosa",
    "Nazar Na Lage", "The Underdogs", "Padhai Later", "Panic Buttons", "Last Bencherss",
]

QUESTIONS = [
    ("Which Indian city is known as the Pink City?", "Jaipur", ["Jaipur City"]),
    ("Who wrote the novel 'Midnight's Children'?", "Salman Rushdie", ["Rushdie", "Ahmed Salman Rushdie"]),
    ("In cricket, what does the acronym LBW stand for?", "Leg Before Wicket", []),
    ("Which river is the longest in India by length within the country?", "Ganga",
     ["Ganges", "Ganga River", "River Ganges"]),
    ("Name the scientist who discovered the Raman Effect.", "C V Raman",
     ["Chandrasekhara Venkata Raman", "Sir C V Raman"]),
    ("Which company's logo is a half-eaten apple?", "Apple", ["Apple Inc", "Apple Computer"]),
    ("Identify the dance form: originating in Kerala, performed with elaborate green face "
     "paint and headgear, and traditionally by male dancers.", "Kathakali", []),
    ("What is the currency of Japan?", "Yen", ["Japanese Yen", "JPY"]),
    ("Which planet rotates on its side, with an axial tilt of about 98 degrees?", "Uranus", []),
    ("Who was the first woman to become the Prime Minister of India?", "Indira Gandhi",
     ["Indira Priyadarshini Gandhi", "Mrs Indira Gandhi"]),
    ("In the periodic table, which element has the symbol 'W'?", "Tungsten", ["Wolfram"]),
    ("Connect: Vidhan Soudha, Cubbon Park, Chinnaswamy Stadium, Majestic.", "Bengaluru",
     ["Bangalore"]),
    ("Which two colours appear on the flag of Japan?", "Red and White", ["White and Red"]),
    ("Sachin Tendulkar scored his 100th international century against which country?",
     "Bangladesh", []),
    ("Which Indian state has the longest coastline?", "Gujarat", []),
    ("'Ctrl + Z' does what on most computers?", "Undo", ["Undo the last action"]),
    ("Which company makes the Pixel range of smartphones?", "Google", ["Alphabet", "Google LLC"]),
    ("Name the three Khans of Bollywood born in 1965.", "Shah Rukh Khan, Salman Khan, Aamir Khan",
     ["SRK, Salman, Aamir"]),
    ("In which year did India win its first Cricket World Cup?", "1983", []),
    ("Connect: Dalal Street, Sensex, Bombay Stock Exchange, Mumbai.", "The Indian stock market",
     ["Stock market", "BSE", "Share market"]),
]


def seed(eng):
    eng.set_cfg({"quiz_name": "ACQ General (demo)"})
    eng.generate_teams(len(TEAMS), TEAMS, replace=True)
    for i, (text, answer, alts) in enumerate(QUESTIONS, 1):
        eng.add_question({"number": i, "text": text, "official_answer": answer,
                          "alt_answers": alts})
